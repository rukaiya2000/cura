"""Milestone 2 — the forge itself.

Headline assertion: a composite skill gets forged from a natural-language ask and passes
its own generated test. Plus the two properties the product rests on — the forge cannot
exceed the speaker's scope, and every rejection comes back as the next attempt's input.
"""

import copy

import pytest

from skillforge.adapters.fake_scoped import BoundScopedClient, FakeScalekitActions
from skillforge.adapters.llm import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    ForgeRequest,
    GenerationError,
    ScriptedGenerator,
)
from skillforge.core.forge import Forge
from skillforge.core.manifest import Trust

KWARGS = {"issue_id": "LIN-402", "escalate_to": "sam@co"}
INTENT = "escalate LIN-402 to Sam, mark it urgent, and re-triage what I was blocking"

GOOD_SOURCE = '''\
URGENT = "Urgent"
FLOOR = "High"


def run(scoped_client, issue_id, escalate_to):
    issue = scoped_client.call("linear.get_issue", issue_id=issue_id)
    previous = issue["assignee"]
    scoped_client.call("linear.update_issue", issue_id=issue_id,
                       assignee=escalate_to, priority=URGENT)
    cycle = scoped_client.call("linear.get_active_cycle", team_id=issue["team_id"])
    scoped_client.call("linear.link_issue", issue_id=issue_id, target_id=cycle["id"])

    rebalanced = []
    if previous and previous != escalate_to:
        blockers = scoped_client.call("linear.list_issues", assignee=previous,
                                      priority_gte=FLOOR)
        for blocker in blockers:
            if blocker["id"] == issue_id:
                continue
            scoped_client.call("linear.create_comment", issue_id=blocker["id"],
                               body="Re-triage: " + issue_id + " was escalated.")
            rebalanced.append(blocker["id"])

    observed = scoped_client.call("linear.get_issue", issue_id=issue_id)
    return {
        "issue_id": observed["id"],
        "observed_assignee": observed["assignee"],
        "observed_priority": observed["priority"],
        "observed_links": observed["links"],
        "previous_assignee": previous,
        "rebalanced": rebalanced,
        "cycle": cycle["name"],
    }
'''

GOOD_TEST = '''\
def check(result, calls):
    assert result["observed_assignee"] == "sam@co", result["observed_assignee"]
    assert result["observed_priority"] == "Urgent", result["observed_priority"]
    assert "CYC-14" in result["observed_links"], result["observed_links"]
    assert sorted(result["rebalanced"]) == ["LIN-377", "LIN-388"], result["rebalanced"]
    assert calls[0]["primitive"] == "linear.get_issue", "must read before writing"
    assert calls[-1]["primitive"] == "linear.get_issue", "must read back after acting"
'''

#: Attempt 1 reaches for a primitive that doesn't exist in the introspected set — the
#: realistic first failure, and the one the demo shows.
BAD_SOURCE = GOOD_SOURCE.replace("linear.get_active_cycle", "linear.get_epic")

FULL_PRIMITIVES = [
    "linear.get_issue", "linear.update_issue", "linear.get_active_cycle",
    "linear.link_issue", "linear.list_issues", "linear.create_comment",
]


def payload(*, source, test_source=GOOD_TEST, skill="escalate_and_rebalance",
            primitives=None, effects="write", reversible=True,
            inverse="restore_snapshot", description="Escalate an issue and re-triage."):
    return {
        "skill": skill,
        "description": description,
        "primitives_used": list(FULL_PRIMITIVES if primitives is None else primitives),
        "effects": effects,
        "reversible": reversible,
        "inverse": inverse,
        "source": source,
        "test_source": test_source,
    }


@pytest.fixture
def forge_for(library):
    def _make(payloads, **kw):
        generator = ScriptedGenerator(payloads)
        return Forge(generator=generator, library=library, **kw), generator
    return _make


@pytest.fixture
def simulator():
    """A throwaway workspace per temper run — a candidate proves itself on a copy."""
    def _make():
        return BoundScopedClient(FakeScalekitActions(), "priya@co")
    return _make


# --- the headline: a composite skill is forged and passes its own test -------


def test_forges_a_composite_skill_that_passes_its_generated_test(
    forge_for, simulator, client_for, library
):
    forge, _ = forge_for([payload(source=GOOD_SOURCE)])

    outcome = forge.forge(
        intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
        simulator_factory=simulator, kwargs=KWARGS,
    )

    assert outcome.ok, outcome.error
    assert outcome.attempts_made == 1
    assert not outcome.reused

    skill = outcome.skill
    assert skill.name == "escalate_and_rebalance"
    assert skill.trust is Trust.TEMPERED           # earned, not declared
    assert len(skill.manifest.primitives_used) == 6
    assert skill.manifest.forged_by == "priya@co"  # the maker's mark, set by us

    # And it is in the armory, reloadable.
    assert library.load("escalate_and_rebalance").trust is Trust.TEMPERED


def test_the_forge_never_touches_reality_while_forging(
    forge_for, simulator, client_for, actions
):
    """Tempering runs against a simulator, so a candidate cannot mutate production."""
    forge, _ = forge_for([payload(source=GOOD_SOURCE)])
    before = copy.deepcopy(actions.workspace)

    outcome = forge.forge(
        intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
        simulator_factory=simulator, kwargs=KWARGS,
    )

    assert outcome.ok, outcome.error
    assert actions.workspace == before, "forging mutated the real workspace"
    assert actions.log == [], "forging executed against the real client"


# --- the Reflexion loop -----------------------------------------------------


def test_a_failure_becomes_the_next_attempt_s_input(forge_for, simulator, client_for):
    """The loop that makes self-correction real: attempt 2 is generated *from* the
    reason attempt 1 failed."""
    forge, generator = forge_for([
        payload(source=BAD_SOURCE),   # calls linear.get_epic — not in the granted set
        payload(source=GOOD_SOURCE),
    ])

    outcome = forge.forge(
        intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
        simulator_factory=simulator, kwargs=KWARGS,
    )

    assert outcome.ok, outcome.error
    assert outcome.attempts_made == 2
    assert outcome.attempts[0].ok is False
    assert "linear.get_epic" in outcome.attempts[0].reason

    # The generator was told why, and was given the code that failed.
    first, second = generator.requests
    assert first.feedback is None
    assert second.attempt == 2
    assert "linear.get_epic" in second.feedback
    assert second.previous_source == BAD_SOURCE

    # That reason reaches the prompt, not just the dataclass.
    rendered = second.prompt()
    assert "Attempt 1 failed" in rendered
    assert "linear.get_epic" in rendered


def test_a_failing_generated_test_is_also_feedback(forge_for, simulator, client_for):
    forge, generator = forge_for([
        payload(source=GOOD_SOURCE,
                test_source="def check(result, calls):\n"
                            "    assert result['observed_priority'] == 'Low', 'wanted Low'\n"),
        payload(source=GOOD_SOURCE),
    ])

    outcome = forge.forge(
        intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
        simulator_factory=simulator, kwargs=KWARGS,
    )

    assert outcome.ok, outcome.error
    assert "wanted Low" in outcome.attempts[0].reason
    assert "wanted Low" in generator.requests[1].feedback


def test_gives_up_after_max_attempts_and_says_why(forge_for, simulator, client_for):
    forge, _ = forge_for([payload(source=BAD_SOURCE)] * 3, max_attempts=3)

    outcome = forge.forge(
        intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
        simulator_factory=simulator, kwargs=KWARGS,
    )

    assert not outcome.ok
    assert outcome.attempts_made == 3
    assert outcome.skill is None
    assert "gave up after 3 attempts" in outcome.error
    assert "linear.get_epic" in outcome.error


def test_a_generator_that_cannot_produce_anything_is_reported(
    forge_for, simulator, client_for
):
    forge, _ = forge_for([], max_attempts=2)

    outcome = forge.forge(
        intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
        simulator_factory=simulator, kwargs=KWARGS,
    )

    assert not outcome.ok
    assert "ran out of payloads" in outcome.error


# --- the scope ceiling ------------------------------------------------------


def test_the_generator_is_shown_only_what_the_speaker_holds(
    forge_for, simulator, client_for
):
    forge, generator = forge_for([payload(source=GOOD_SOURCE)])
    forge.forge(intent=INTENT, speaker="sam@co", client=client_for("sam@co"),
                simulator_factory=simulator, kwargs=KWARGS)

    shown = {t["definition"]["name"] for t in generator.requests[0].tools}
    assert shown == {
        "linear.get_issue", "linear.list_issues",
        "linear.get_active_cycle", "linear.create_comment",
    }
    assert "linear.update_issue" not in shown
    # The prompt itself carries the ceiling, not just the request object.
    assert "linear.update_issue" not in generator.requests[0].prompt()


def test_a_manifest_cannot_widen_its_own_reach(forge_for, simulator, client_for):
    """Defence in depth: even if the model declared a primitive it wasn't shown,
    the scope gate refuses it before the code is ever tempered."""
    forge, _ = forge_for([payload(source=GOOD_SOURCE)])   # declares all six

    outcome = forge.forge(
        intent=INTENT, speaker="sam@co", client=client_for("sam@co"),  # holds four
        simulator_factory=simulator, kwargs=KWARGS,
    )

    assert not outcome.ok
    reason = outcome.attempts[0].reason
    assert "linear.update_issue" in reason and "linear.link_issue" in reason
    assert "not granted to sam@co" in reason


def test_a_destructive_primitive_nobody_holds_cannot_be_forged(
    forge_for, simulator, client_for
):
    forge, _ = forge_for([payload(
        source=('def run(scoped_client, project_id):\n'
                '    return scoped_client.call("linear.delete_project", '
                'project_id=project_id)\n'),
        test_source="def check(result, calls):\n    assert result\n",
        skill="delete_project", primitives=["linear.delete_project"],
        effects="destructive", reversible=False, inverse=None,
        description="Delete a project.",
    )])

    outcome = forge.forge(
        intent="delete Priya's whole project", speaker="sam@co",
        client=client_for("sam@co"), simulator_factory=simulator,
        kwargs={"project_id": "PRJ-1"},
    )

    assert not outcome.ok
    assert "linear.delete_project" in outcome.attempts[0].reason
    assert "not granted" in outcome.attempts[0].reason


def test_code_reaching_past_its_manifest_is_refused_before_execution(
    forge_for, simulator, client_for
):
    forge, _ = forge_for([payload(
        source=GOOD_SOURCE,
        primitives=[p for p in FULL_PRIMITIVES if p != "linear.create_comment"],
    )])

    outcome = forge.forge(
        intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
        simulator_factory=simulator, kwargs=KWARGS,
    )

    assert not outcome.ok
    assert "linear.create_comment" in outcome.attempts[0].reason
    assert "not declared in the manifest" in outcome.attempts[0].reason


def test_generated_code_breaking_the_static_gate_is_refused(
    forge_for, simulator, client_for
):
    forge, _ = forge_for([payload(
        source=('import os\n'
                'def run(scoped_client, issue_id):\n'
                '    return os.environ.get("LINEAR_API_KEY")\n'),
        primitives=["linear.get_issue"],
    )])

    outcome = forge.forge(
        intent="read the api key", speaker="priya@co", client=client_for("priya@co"),
        simulator_factory=simulator, kwargs={"issue_id": "LIN-402"},
    )

    assert not outcome.ok
    assert "allowlist" in outcome.attempts[0].reason


def test_a_skill_cannot_claim_someone_elses_mark_or_declare_itself_trusted(
    forge_for, simulator, client_for
):
    forge, _ = forge_for([{
        **payload(source=GOOD_SOURCE),
        "skill": "escalate_and_rebalance",
    }])
    # The model has no way to send forged_by/trust — they aren't in the response schema.
    assert "forged_by" not in RESPONSE_SCHEMA["properties"]
    assert "trust" not in RESPONSE_SCHEMA["properties"]

    outcome = forge.forge(
        intent=INTENT, speaker="dana@co", client=client_for("dana@co"),
        simulator_factory=simulator, kwargs=KWARGS,
    )

    assert outcome.ok, outcome.error
    assert outcome.skill.manifest.forged_by == "dana@co"


# --- recognition: reuse instead of re-inventing ------------------------------


def test_recognizes_an_existing_skill_and_does_not_forge_again(
    forge_for, simulator, client_for
):
    forge, generator = forge_for([payload(source=GOOD_SOURCE)])
    first = forge.forge(intent=INTENT, speaker="priya@co",
                        client=client_for("priya@co"),
                        simulator_factory=simulator, kwargs=KWARGS)
    assert first.ok and not first.reused

    # Same ask again — the generator has no payloads left, so a second forge would fail.
    second = forge.forge(intent=INTENT, speaker="priya@co",
                         client=client_for("priya@co"),
                         simulator_factory=simulator, kwargs=KWARGS)

    assert second.ok
    assert second.reused
    assert second.skill.name == "escalate_and_rebalance"
    assert len(generator.requests) == 1, "recognition should have skipped generation"


def test_reuse_re_scopes_a_skill_to_whoever_asked(forge_for, simulator, client_for):
    """Priya forges it; Dana reuses it; Sam cannot, because he lacks the grants."""
    forge, _ = forge_for([payload(source=GOOD_SOURCE)])
    forge.forge(intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
                simulator_factory=simulator, kwargs=KWARGS)

    granted_dana = client_for("dana@co").granted_primitives()
    granted_sam = client_for("sam@co").granted_primitives()

    assert forge.recognize(INTENT, granted=granted_dana) is not None
    assert forge.recognize(INTENT, granted=granted_sam) is None


def test_a_quarantined_skill_is_never_offered_as_reuse(forge_for, simulator,
                                                      client_for, library):
    forge, _ = forge_for([payload(source=GOOD_SOURCE)])
    outcome = forge.forge(intent=INTENT, speaker="priya@co",
                          client=client_for("priya@co"),
                          simulator_factory=simulator, kwargs=KWARGS)

    library.melt_down(outcome.skill)
    granted = client_for("priya@co").granted_primitives()
    assert forge.recognize(INTENT, granted=granted) is None


def test_an_unrelated_intent_is_not_mistaken_for_reuse(forge_for, simulator, client_for):
    forge, _ = forge_for([payload(source=GOOD_SOURCE)])
    forge.forge(intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
                simulator_factory=simulator, kwargs=KWARGS)

    granted = client_for("priya@co").granted_primitives()
    assert forge.recognize("what did we ship last sprint", granted=granted) is None


def test_recognition_survives_a_verbose_model_written_description(
    forge_for, simulator, client_for
):
    """The description is model-written, so its length is not ours to control —
    a rich one must not dilute a correct match."""
    verbose = ("Escalate an issue to someone, mark it urgent, pull it into the team's "
               "active cycle, and flag the previous assignee's remaining high-priority "
               "work for re-triage.")
    forge, _ = forge_for([payload(source=GOOD_SOURCE, description=verbose)])
    forge.forge(intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
                simulator_factory=simulator, kwargs=KWARGS)

    granted = client_for("dana@co").granted_primitives()
    assert forge.recognize(INTENT, granted=granted) is not None


def test_one_shared_generic_noun_is_not_a_match(forge_for, simulator, client_for,
                                                library):
    """`delete_project` and "update the project" share exactly one word. That is a
    coincidence, and matching on it would be dangerous rather than merely wrong."""
    from skillforge.core.library import new_skill
    from skillforge.core.manifest import CapabilityManifest

    forge, _ = forge_for([])
    danger = new_skill(
        CapabilityManifest.from_dict({
            "skill": "delete_project", "app": "linear",
            "primitives_used": ["linear.get_issue"], "effects": "read",
            "reversible": False, "inverse": None, "forged_by": "priya@co",
            "trust": "quarantined", "description": "Delete a project.",
        }),
        source="def run(scoped_client):\n    return 1\n",
    )
    library.register(danger)
    library.temper(danger)

    granted = client_for("priya@co").granted_primitives()
    assert forge.recognize("update the project", granted=granted) is None
    assert forge.score_match("update the project", danger) == 0.0


def test_the_matcher_is_pluggable(forge_for, simulator, client_for):
    """The single hook an embedding lookup needs."""
    forge, _ = forge_for([payload(source=GOOD_SOURCE)])
    forge.forge(intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
                simulator_factory=simulator, kwargs=KWARGS)

    granted = client_for("priya@co").granted_primitives()
    forge._matcher = lambda intent, skill: 0.0
    assert forge.recognize(INTENT, granted=granted) is None
    forge._matcher = lambda intent, skill: 1.0
    assert forge.recognize("something else entirely", granted=granted) is not None


def test_reforging_the_same_name_creates_a_new_version(forge_for, simulator,
                                                       client_for, library):
    forge, _ = forge_for([payload(source=GOOD_SOURCE), payload(source=GOOD_SOURCE)])
    first = forge.forge(intent=INTENT, speaker="priya@co",
                        client=client_for("priya@co"),
                        simulator_factory=simulator, kwargs=KWARGS)
    second = forge.forge(intent=INTENT, speaker="priya@co",
                         client=client_for("priya@co"),
                         simulator_factory=simulator, kwargs=KWARGS,
                         allow_reuse=False)

    assert first.skill.version == 1
    assert second.skill.version == 2
    assert library.versions("escalate_and_rebalance") == [1, 2]


# --- the event stream the Armory consumes -----------------------------------


def test_the_forge_emits_the_events_the_armory_renders(forge_for, simulator, client_for):
    from skillforge.ui.events import EventLog

    log = EventLog()
    forge, _ = forge_for([payload(source=BAD_SOURCE), payload(source=GOOD_SOURCE)],
                         events=log)
    outcome = forge.forge(intent=INTENT, speaker="priya@co",
                          client=client_for("priya@co"),
                          simulator_factory=simulator, kwargs=KWARGS,
                          speculative=True)
    assert outcome.ok, outcome.error

    kinds = [e["type"] for e in log.events]
    assert kinds[0] == "forge_start"
    assert log.events[0]["speculative"] is True
    assert "introspect" in kinds
    assert kinds.count("forge_code") == 2
    assert "temper_failed" in kinds
    assert "skill_registered" in kinds
    assert kinds[-1] == "skill_trust"

    stages = [e["stage"] for e in log.events if e["type"] == "forge_stage"]
    assert stages[0] == "heating"
    assert "hammering" in stages and "tempering" in stages
    assert stages[-1] == "stamped"

    # The introspect event carries the ceiling, so the UI can show it.
    introspect = next(e for e in log.events if e["type"] == "introspect")
    assert len(introspect["primitives"]) == 6


# --- the prompt contract ----------------------------------------------------


def test_the_system_prompt_states_the_rules_the_static_gate_enforces():
    """If these drift apart, the gate rejects everything the model writes."""
    for rule in ["literal", "identifier", "run(scoped_client", "primitives_used",
                 "read back", "json, math, re, datetime, typing"]:
        assert rule in SYSTEM_PROMPT, f"prompt no longer states: {rule}"


def test_the_prompt_carries_the_intent_and_every_granted_schema(client_for):
    tools = client_for("priya@co").granted_tools()
    rendered = ForgeRequest(intent=INTENT, speaker="priya@co", apps=["linear"],
                            tools=tools).prompt()

    assert INTENT in rendered
    assert "priya@co" in rendered
    for tool in tools:
        assert tool["definition"]["name"] in rendered
        assert "input_schema" in rendered


def test_scripted_generator_is_exhaustible():
    gen = ScriptedGenerator([])
    with pytest.raises(GenerationError):
        gen.generate(ForgeRequest(intent="x", speaker="a@b", apps=["linear"], tools=[]))


def test_the_prompt_states_the_caller_s_argument_names(client_for):
    """Observed failing for real: without this the model guesses parameter names and
    burns an entire attempt on `TypeError: unexpected keyword argument`."""
    tools = client_for("priya@co").granted_tools()
    rendered = ForgeRequest(
        intent=INTENT, speaker="priya@co", apps=["linear"], tools=tools, args=KWARGS,
    ).prompt()

    assert "issue_id='LIN-402'" in rendered
    assert "escalate_to='sam@co'" in rendered
    assert "Your signature must accept these names" in rendered


def test_the_forge_tells_the_generator_the_kwargs_it_will_pass(
    forge_for, simulator, client_for
):
    forge, generator = forge_for([payload(source=GOOD_SOURCE)])
    forge.forge(intent=INTENT, speaker="priya@co", client=client_for("priya@co"),
                simulator_factory=simulator, kwargs=KWARGS)

    assert generator.requests[0].args == KWARGS


# --- a skill that spans two services ----------------------------------------
#
# The milestone for widening `app` to `apps`. The most valuable skill in a clinical
# workflow — "book the follow-up and log the request on the record" — is one intent
# reaching Calendar and HubSpot, and the single-app manifest rejected it outright.
#
# The two-app client is built here rather than in `fake_scoped` so the Linear demo
# fixture is left exactly as it is; a clinical adapter belongs in adapters/ once the
# consultation layer exists, not smuggled in under a manifest refactor.

CLINICAL_CATALOGUE = {
    "hubspot.get_contact": (
        "read", "Fetch a contact record by id.",
        {"type": "object", "properties": {"contact_id": {"type": "string"}},
         "required": ["contact_id"]}),
    "hubspot.create_note": (
        "write", "Add a note to a contact's record.",
        {"type": "object", "properties": {"contact_id": {"type": "string"},
                                          "body": {"type": "string"}},
         "required": ["contact_id", "body"]}),
    "calendar.create_event": (
        "write", "Create a calendar event and invite an attendee.",
        {"type": "object", "properties": {"title": {"type": "string"},
                                          "starts_at": {"type": "string"},
                                          "attendee": {"type": "string"}},
         "required": ["title", "starts_at"]}),
    "gmail.send": (
        "write", "Send an email.",
        {"type": "object", "properties": {"to": {"type": "string"}},
         "required": ["to"]}),
}


class ClinicalClient:
    """A speaker holding three connected services, only two of which they will use."""

    def __init__(self, granted=("hubspot.get_contact", "hubspot.create_note",
                                "calendar.create_event", "gmail.send")):
        self.granted = set(granted)
        self.contacts = {"PT-10482": {"id": "PT-10482", "name": "Amara Okafor",
                                      "notes": []}}
        self.events = []

    @property
    def identifier(self):
        return "priya.rao@clinic.test"

    def granted_tools(self):
        return [{"definition": {"name": n, "description": d, "input_schema": s},
                 "effect": e}
                for n, (e, d, s) in CLINICAL_CATALOGUE.items() if n in self.granted]

    def call(self, primitive, **tool_input):
        if primitive not in self.granted:
            from skillforge.core.sandbox import ScopedCallDenied
            raise ScopedCallDenied(f"not granted {primitive!r}")
        if primitive == "hubspot.get_contact":
            return copy.deepcopy(self.contacts[tool_input["contact_id"]])
        if primitive == "hubspot.create_note":
            contact = self.contacts[tool_input["contact_id"]]
            contact["notes"].append(tool_input["body"])
            return copy.deepcopy(contact)
        if primitive == "calendar.create_event":
            self.events.append(dict(tool_input))
            return {"id": f"evt-{len(self.events)}", **tool_input}
        raise AssertionError(f"unexpected {primitive}")


CROSS_APP_SOURCE = '''\
def run(scoped_client, contact_id, starts_at):
    contact = scoped_client.call("hubspot.get_contact", contact_id=contact_id)
    event = scoped_client.call("calendar.create_event",
                               title="Follow-up consultation",
                               starts_at=starts_at, attendee=contact["name"])
    scoped_client.call("hubspot.create_note", contact_id=contact_id,
                       body="Follow-up booked for " + starts_at)
    observed = scoped_client.call("hubspot.get_contact", contact_id=contact_id)
    return {"event_id": event["id"], "notes": observed["notes"],
            "patient": observed["name"]}
'''

CROSS_APP_TEST = '''\
def check(result, calls):
    assert result["event_id"], "no event created"
    assert any("Follow-up booked" in n for n in result["notes"]), result["notes"]
    assert calls[0]["primitive"] == "hubspot.get_contact", "must read before writing"
    assert calls[-1]["primitive"] == "hubspot.get_contact", "must read back after acting"
'''

CROSS_APP_KWARGS = {"contact_id": "PT-10482", "starts_at": "2026-09-05T09:20"}
CROSS_APP_PRIMITIVES = ["hubspot.get_contact", "calendar.create_event",
                        "hubspot.create_note"]


def cross_app_payload(**over):
    return payload(source=CROSS_APP_SOURCE, test_source=CROSS_APP_TEST,
                   skill="schedule_followup_and_log_request",
                   primitives=CROSS_APP_PRIMITIVES, effects="write",
                   reversible=True, inverse="cancel_followup",
                   description="Book a follow-up and log it on the record.") | over


def test_forges_a_skill_that_spans_calendar_and_the_crm(forge_for, library):
    """The milestone. Under the single-app manifest this was rejected before it ran."""
    forge, _ = forge_for([cross_app_payload()])
    outcome = forge.forge(intent="book Amara a follow-up in six weeks and log it",
                          speaker="priya.rao@clinic.test", client=ClinicalClient(),
                          simulator_factory=ClinicalClient,
                          kwargs=CROSS_APP_KWARGS)

    assert outcome.ok, outcome.error
    assert outcome.skill.manifest.apps == ["calendar", "hubspot"]
    assert outcome.skill.manifest.trust is Trust.TEMPERED


def test_the_declared_apps_describe_the_skill_not_the_speaker(forge_for, library):
    """The speaker holds Gmail too. The manifest must not claim it — a policy denying
    Gmail would then refuse a skill that cannot reach Gmail, and a ceiling that fires on
    an unreachable service is a false refusal."""
    forge, _ = forge_for([cross_app_payload()])
    outcome = forge.forge(intent="book Amara a follow-up in six weeks and log it",
                          speaker="priya.rao@clinic.test", client=ClinicalClient(),
                          simulator_factory=ClinicalClient, kwargs=CROSS_APP_KWARGS)

    assert "gmail" not in outcome.skill.manifest.apps
    assert "gmail.send" in ClinicalClient().granted


def test_a_cross_app_skill_still_cannot_exceed_the_speakers_scope(forge_for, library):
    """Widening the manifest must not widen the ceiling. The speaker has no Calendar
    grant here, so the same composition is refused however it is declared."""
    crm_only = ClinicalClient(granted=("hubspot.get_contact", "hubspot.create_note"))
    forge, _ = forge_for([cross_app_payload()] * 3)
    outcome = forge.forge(intent="book Amara a follow-up in six weeks and log it",
                          speaker="priya.rao@clinic.test", client=crm_only,
                          simulator_factory=lambda: crm_only,
                          kwargs=CROSS_APP_KWARGS)

    assert not outcome.ok
    blocked = " ".join(a.reason or "" for a in outcome.attempts)
    assert "calendar.create_event" in blocked, blocked
    assert not crm_only.events, "an event was created despite the missing grant"
