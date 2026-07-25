"""Policy ceilings and the audit trail.

Two properties carry most of the weight here:

* **Policy narrows, never grants.** There is no configuration that hands someone a
  capability their own permissions didn't include.
* **The audit log is evidence, not a log.** A hash chain means editing or removing a past
  record is detectable, and detectable *specifically* — edited, removed, or dropped are
  reported as different problems because they mean different things.
"""

import json

import pytest

from skillforge.adapters.fake_scoped import BoundScopedClient, FakeScalekitActions
from skillforge.adapters.llm import ScriptedGenerator
from skillforge.core.audit import GENESIS, AuditLog, derive_states
from skillforge.core.forge import Forge
from skillforge.core.intent import RuleIntentDetector
from skillforge.core.manifest import CapabilityManifest, Effect
from skillforge.core.policy import STRICT, Policy
from skillforge.core.router import Confidence, Decision, Router

from tests.test_forge import GOOD_SOURCE, FULL_PRIMITIVES, payload
from tests.test_router import ASSIGN, ESCALATE, ROSTER, assign_payload


def manifest(**over) -> CapabilityManifest:
    base = {
        "skill": "escalate_and_rebalance", "apps": ["linear"],
        "primitives_used": list(FULL_PRIMITIVES), "effects": "write",
        "reversible": True, "inverse": "restore_snapshot",
        "forged_by": "priya@co", "trust": "quarantined",
        "description": "Escalate an issue and re-triage.",
    }
    return CapabilityManifest.from_dict({**base, **over})


# --- policy: the empty case ---------------------------------------------------


def test_an_empty_policy_permits_everything():
    """Policy is opt-in. An unconfigured one must not become a deny-all that looks
    like a broken forge."""
    open_policy = Policy()
    assert open_policy.evaluate(manifest())
    assert open_policy.evaluate(manifest(effects="destructive", reversible=False,
                                         inverse=None))
    assert open_policy.permits_primitive("linear.delete_project")
    assert open_policy.filter_primitives([
        {"definition": {"name": "linear.delete_project"}}]) != []


def test_an_empty_allowlist_is_not_the_same_as_no_allowlist():
    """`None` means unconfigured; an empty set means "nothing permitted"."""
    assert Policy(allow_apps=None).permits_primitive("linear.get_issue")
    assert not Policy(allow_apps=frozenset()).permits_primitive("linear.get_issue")


# --- policy: each ceiling ----------------------------------------------------


def test_denies_an_effect_class():
    p = Policy(label="no-destruction", deny_effects=frozenset({Effect.DESTRUCTIVE}))
    assert p.evaluate(manifest())
    verdict = p.evaluate(manifest(effects="destructive", reversible=False, inverse=None))
    assert not verdict
    assert "forbids destructive skills" in verdict.reason


def test_denies_an_app_and_enforces_an_allowlist():
    assert not Policy(deny_apps=frozenset({"linear"})).evaluate(manifest())
    verdict = Policy(label="linear-only",
                     allow_apps=frozenset({"linear"})).evaluate(manifest(apps=["linear"]))
    assert verdict
    with pytest.raises(Exception):
        # A manifest can't even name a primitive outside its declared apps, so an off-app
        # manifest is unconstructable — the ceiling is belt to that braces.
        manifest(apps=["workday"])


def clinical(**over) -> CapabilityManifest:
    """A skill spanning two services, which is the shape this product actually forges."""
    base = {
        "skill": "schedule_followup_and_log_request",
        "apps": ["calendar", "hubspot"],
        "primitives_used": ["calendar.create_event", "hubspot.create_note"],
        "effects": "write", "reversible": True, "inverse": "cancel_followup",
        "forged_by": "priya.rao@clinic.test", "trust": "quarantined",
        "description": "Book a follow-up and log the request on the record.",
    }
    return CapabilityManifest.from_dict({**base, **over})


def test_denying_one_app_refuses_a_skill_that_spans_it():
    """A ceiling that held for one of a skill's two services would not be a ceiling.
    Denying `hubspot` must stop a calendar-and-hubspot skill outright, not partially."""
    verdict = Policy(label="no-crm",
                     deny_apps=frozenset({"hubspot"})).evaluate(clinical())
    assert not verdict
    assert "hubspot" in verdict.reason
    assert "calendar" not in verdict.reason, "named a service it does not object to"


def test_an_allowlist_names_every_app_outside_it():
    """All the reasons, not the first — a caller fixing one violation only to hit the
    next learns nothing per round trip."""
    verdict = Policy(label="calendar-only",
                     allow_apps=frozenset({"calendar"})).evaluate(clinical())
    assert not verdict
    assert "hubspot" in verdict.reason


def test_a_multi_app_skill_passes_when_every_app_is_permitted():
    assert Policy(label="clinic",
                  allow_apps=frozenset({"calendar", "hubspot", "gmail"})).evaluate(clinical())


def test_a_skill_is_not_judged_on_services_it_never_reaches():
    """The manifest's `apps` is derived from the primitives declared, so a doctor with
    Gmail connected who forges a calendar-only skill is not refused by a policy denying
    Gmail. A ceiling firing on a service the skill cannot reach is a false refusal, and
    false refusals are how a governance layer gets switched off."""
    calendar_only = clinical(apps=["calendar"],
                             primitives_used=["calendar.create_event"],
                             skill="book_followup")
    assert Policy(deny_apps=frozenset({"gmail"})).evaluate(calendar_only)


def test_denies_specific_primitives():
    p = Policy(label="no-deletes",
               deny_primitives=frozenset({"linear.delete_project"}))
    assert not p.permits_primitive("linear.delete_project")
    assert p.permits_primitive("linear.get_issue")

    verdict = p.evaluate(manifest(skill="nuke", effects="destructive",
                                  primitives_used=["linear.delete_project"],
                                  reversible=False, inverse=None))
    assert not verdict
    assert "linear.delete_project" in verdict.reason


def test_caps_composition_size():
    p = Policy(label="small", max_primitives=3)
    verdict = p.evaluate(manifest())          # six primitives
    assert not verdict
    assert "caps a skill at 3 primitives" in verdict.reason
    assert "declares 6" in verdict.reason


def test_can_require_writes_to_be_undoable():
    p = Policy(label="undoable", require_reversible=frozenset({Effect.WRITE}))
    assert p.evaluate(manifest(reversible=True, inverse="restore_snapshot"))
    verdict = p.evaluate(manifest(reversible=False, inverse=None))
    assert not verdict
    assert "declare an inverse" in verdict.reason


def test_collects_every_violation_not_just_the_first():
    """A caller fixing one violation only to hit the next learns nothing per round trip."""
    p = Policy(label="tight", max_primitives=2,
               deny_primitives=frozenset({"linear.create_comment"}),
               require_reversible=frozenset({Effect.WRITE}))
    verdict = p.evaluate(manifest(reversible=False, inverse=None))

    assert not verdict
    assert len(verdict.reasons) == 3, verdict.reasons


def test_the_worked_strict_example_is_coherent():
    assert STRICT.evaluate(manifest())                       # a normal write skill passes
    assert not STRICT.evaluate(manifest(effects="destructive", reversible=False,
                                        inverse=None))
    assert not STRICT.permits_primitive("linear.delete_project")
    assert STRICT.needs_confirmation(manifest(effects="destructive", reversible=False,
                                              inverse=None))


# --- policy in the forge: filtering before generation ------------------------


@pytest.fixture
def forge_for(library):
    def _make(payloads, **kw):
        gen = ScriptedGenerator(payloads)
        return Forge(generator=gen, library=library, **kw), gen
    return _make


@pytest.fixture
def simulator():
    def _make():
        return BoundScopedClient(FakeScalekitActions(), "priya@co")
    return _make


def test_a_banned_primitive_is_never_shown_to_the_generator(forge_for, simulator,
                                                            client_for):
    """Governance by construction: the cheapest refusal is the one that never happens."""
    banned = Policy(label="no-comments",
                    deny_primitives=frozenset({"linear.create_comment"}))
    forge, generator = forge_for([payload(source=GOOD_SOURCE)], policy=banned)

    forge.forge(intent=ESCALATE, speaker="priya@co", client=client_for("priya@co"),
                simulator_factory=simulator,
                kwargs={"issue_id": "LIN-402", "escalate_to": "sam@co"})

    shown = {t["definition"]["name"] for t in generator.requests[0].tools}
    assert "linear.create_comment" not in shown
    assert "linear.get_issue" in shown
    assert "linear.create_comment" not in generator.requests[0].prompt()


def test_policy_rejection_becomes_regeneration_feedback(forge_for, simulator,
                                                        client_for):
    """A policy refusal is an input like any other failure, not a dead end."""
    forge, generator = forge_for(
        [payload(source=GOOD_SOURCE), payload(source=GOOD_SOURCE)],
        policy=Policy(label="small", max_primitives=3),
    )
    outcome = forge.forge(intent=ESCALATE, speaker="priya@co",
                          client=client_for("priya@co"), simulator_factory=simulator,
                          kwargs={"issue_id": "LIN-402", "escalate_to": "sam@co"})

    assert not outcome.ok
    assert "caps a skill at 3 primitives" in outcome.attempts[0].reason
    assert "caps a skill at 3" in generator.requests[1].feedback


# --- policy in the router: re-checked at execution ---------------------------


@pytest.fixture
def wired(library, tmp_path):
    """A router with an audit log, and a policy that can be swapped mid-session."""
    def _make(payloads=None, *, policy=None, confirm=lambda p: True):
        actions = FakeScalekitActions()
        audit = AuditLog(tmp_path / "audit.jsonl")
        forge = Forge(
            generator=ScriptedGenerator(payloads if payloads is not None
                                        else [assign_payload()]),
            library=library, policy=policy,
        )
        router = Router(
            forge=forge, library=library,
            client_factory=lambda who: BoundScopedClient(actions, who),
            simulator_factory=lambda: BoundScopedClient(FakeScalekitActions(), "priya@co"),
            detector=RuleIntentDetector(), roster=ROSTER,
            confirm=confirm, audit=audit,
        )
        return router, actions, audit
    return _make


def test_the_router_inherits_the_forges_policy_by_default(wired):
    """One place to configure, so the two layers cannot disagree about what's permitted."""
    router, _, _ = wired(policy=STRICT)
    assert router.policy is STRICT


def test_a_policy_tightened_after_forging_blocks_the_reused_skill(wired):
    """A reused skill was gated under whatever policy was in force when it was forged.
    If policy has since tightened, execution must respect the new ceiling."""
    router, actions, _ = wired()
    first = router.handle(ASSIGN, speaker="priya@co")
    assert first.decision is Decision.ACTED
    calls_before = len(actions.log)

    # An admin tightens the ceiling between turns.
    router.policy = Policy(label="frozen", max_primitives=1)

    second = router.handle(ASSIGN, speaker="dana@co")
    assert second.decision is Decision.DENIED
    assert "caps a skill at 1 primitives" in second.reason
    assert len(actions.log) == calls_before, "policy denial still executed"


def test_policy_can_demand_confirmation_the_trust_ladder_would_not(wired):
    router, actions, _ = wired(
        policy=Policy(label="paranoid", always_confirm=frozenset({Effect.WRITE})),
        confirm=None,          # nobody available to approve
    )
    outcome = router.handle(ASSIGN, speaker="priya@co")

    assert outcome.decision is Decision.NEEDS_CONFIRMATION
    assert actions.log == []


# --- the audit log -----------------------------------------------------------


def test_records_an_action_with_who_what_and_observed_state(wired):
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="priya@co")

    records = audit.records()
    assert len(records) == 1
    r = records[0]
    assert r.seq == 1
    assert r.outcome == "acted"
    assert r.actor == "priya@co"
    assert r.skill == "assign_and_prioritize"
    assert r.utterance == ASSIGN
    assert r.args["issue_id"] == "LIN-402"
    assert r.identity_confidence == "high"
    # The manifest *as executed*, so a later edit to the skill can't rewrite history.
    assert r.manifest["forged_by"] == "priya@co"
    assert r.calls, "no call log recorded"


def test_records_a_denial_with_the_primitive_that_stopped_it(wired):
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="priya@co")
    router.handle(ASSIGN, speaker="sam@co")

    denials = audit.denials()
    assert len(denials) == 1
    assert denials[0].actor == "sam@co"
    assert denials[0].blocked_at == "linear.update_issue"


def test_records_an_identity_denial_with_the_confidence_tier(wired):
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="ghost@nowhere", confidence=Confidence.UNKNOWN)

    r = audit.records()[0]
    assert r.outcome == "denied"
    assert r.identity_confidence == "unknown"
    assert r.skill is None


def test_chatter_is_not_recorded(wired):
    """Logging every line of a call would bury the entries a reviewer wants."""
    router, _, audit = wired()
    router.handle("The SSO login bug is worse than we thought.", speaker="priya@co")
    router.handle("Forge, escalate it to Dana", speaker="priya@co")   # clarify

    assert audit.records() == []


def test_captures_before_and_after_state_from_the_read_back_convention(wired):
    router, _, audit = wired([payload(source=GOOD_SOURCE)])
    router.handle(ESCALATE, speaker="priya@co")

    r = audit.records()[0]
    assert r.before is not None and r.after is not None
    assert r.before["priority"] == "Medium"      # what it overwrote
    assert r.after["priority"] == "Urgent"       # what it observed afterwards
    assert r.reversible, "a write with a before-state and an inverse should be reversible"


def test_a_skill_that_skips_the_read_back_has_no_recoverable_before_state():
    """The concrete reason read-before-write is a hard rule, not a style preference."""
    assert derive_states([]) == (None, None)
    assert derive_states([{"primitive": "linear.update_issue", "input": {}, "ok": True,
                           "result": {}}]) == (None, None)
    # Different targets — not a before/after pair for the same thing.
    assert derive_states([
        {"primitive": "linear.get_issue", "input": {"issue_id": "A"}, "ok": True,
         "result": {"priority": "Low"}},
        {"primitive": "linear.get_issue", "input": {"issue_id": "B"}, "ok": True,
         "result": {"priority": "High"}},
    ]) == (None, None)


# --- the hash chain: evidence rather than a log ------------------------------


def test_the_chain_starts_at_genesis_and_links_forward(wired):
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="priya@co")
    router.handle(ASSIGN, speaker="sam@co")

    first, second = audit.records()
    assert first.prev_hash == GENESIS
    assert second.prev_hash == first.hash
    ok, problems = audit.verify()
    assert ok, problems


def test_an_edited_record_is_detected_as_edited(wired):
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="priya@co")
    router.handle(ASSIGN, speaker="sam@co")
    assert audit.verify()[0]

    # Rewrite history: make the denial look like it was someone else.
    lines = audit.path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["actor"] = "priya@co"
    lines[1] = json.dumps(tampered)
    audit.path.write_text("\n".join(lines) + "\n")

    ok, problems = audit.verify()
    assert not ok
    assert any("was edited after writing" in p for p in problems)


def test_a_record_removed_from_the_middle_is_detected(wired):
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="priya@co")     # 1 acted
    router.handle(ASSIGN, speaker="sam@co")       # 2 denied  ← the inconvenient one
    router.handle(ASSIGN, speaker="dana@co")      # 3 acted
    assert audit.verify()[0]

    lines = audit.path.read_text().splitlines()
    audit.path.write_text(lines[0] + "\n" + lines[2] + "\n")

    ok, problems = audit.verify()
    assert not ok
    assert any("dropped" in p for p in problems), problems
    assert any("chain broken" in p for p in problems), problems


def test_truncating_the_tail_is_a_known_limit_of_a_hash_chain(wired):
    """Worth pinning as a documented limitation rather than pretending otherwise.

    A hash chain proves nothing was altered *within* the sequence it still holds. It
    cannot prove records were never appended past the end — detecting that needs the
    tail hash anchored somewhere the writer can't reach (a second store, a signed
    receipt, an external timestamp).
    """
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="priya@co")
    router.handle(ASSIGN, speaker="sam@co")

    last_hash_before = audit.tail().hash
    audit.path.write_text(audit.path.read_text().splitlines()[0] + "\n")

    ok, _ = audit.verify()
    assert ok, "chain still internally consistent — this is the gap"
    assert audit.tail().hash != last_hash_before, (
        "the tail hash changed, which is what an external anchor would catch")


def test_the_table_view_is_flat_enough_for_the_dashboard(wired):
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="priya@co")
    router.handle(ASSIGN, speaker="sam@co")

    rows = audit.table()
    assert [r["outcome"] for r in rows] == ["acted", "denied"]
    assert rows[0]["skill"] == "assign_and_prioritize@v1"
    assert rows[1]["detail"] == "linear.update_issue"
    assert all(isinstance(v, (str, int, bool)) for r in rows for v in r.values())


def test_per_actor_filtering(wired):
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="priya@co")
    router.handle(ASSIGN, speaker="sam@co")

    assert len(audit.for_actor("priya@co")) == 1
    assert len(audit.for_actor("sam@co")) == 1
    assert audit.for_actor("nobody@co") == []


def test_the_log_survives_being_reopened(wired, tmp_path):
    router, _, audit = wired()
    router.handle(ASSIGN, speaker="priya@co")

    reopened = AuditLog(audit.path)
    assert len(reopened) == 1
    assert reopened.verify()[0]
    assert reopened.tail().seq == 1
