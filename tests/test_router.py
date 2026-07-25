"""Milestone: the router decides.

The gates matter more than the happy path here, and most of them are about *not* acting.
A router that fires on chatter fails invisibly, so the negative cases carry as much weight
as the positive one.
"""

import pytest

from skillforge.adapters.fake_scoped import BoundScopedClient, FakeScalekitActions
from skillforge.adapters.llm import ScriptedGenerator
from skillforge.core.forge import Forge
from skillforge.core.intent import RuleIntentDetector
from skillforge.core.manifest import Trust
from skillforge.core.router import Confidence, Decision, Router
from skillforge.ui.events import EventLog

from tests.test_forge import GOOD_SOURCE, GOOD_TEST, FULL_PRIMITIVES, payload

ROSTER = {"priya": "priya@co", "sam": "sam@co", "dana": "dana@co"}
ESCALATE = "Forge — escalate LIN-402 to Sam, mark it urgent, and re-triage what I was blocking"
ASSIGN = "Forge, assign LIN-402 to me and mark it urgent"

#: A second canned skill, named after the *assign* phrasing. A real generator names a
#: skill after the request it was given, so a fixture that always returns the same name
#: would make recognition look broken when it is the fixture that is wrong.
ASSIGN_SOURCE = '''\
URGENT = "Urgent"


def run(scoped_client, issue_id, escalate_to):
    scoped_client.call("linear.update_issue", issue_id=issue_id,
                       assignee=escalate_to, priority=URGENT)
    observed = scoped_client.call("linear.get_issue", issue_id=issue_id)
    return {
        "issue_id": observed["id"],
        "observed_assignee": observed["assignee"],
        "observed_priority": observed["priority"],
    }
'''

ASSIGN_TEST = '''\
def check(result, calls):
    assert result["observed_priority"] == "Urgent", result
    assert result["observed_assignee"] == calls[0]["input"]["assignee"], result
    assert calls[-1]["primitive"] == "linear.get_issue", "must read back after acting"
'''


def assign_payload():
    return payload(
        source=ASSIGN_SOURCE, test_source=ASSIGN_TEST,
        skill="assign_and_prioritize",
        primitives=["linear.update_issue", "linear.get_issue"],
        description="Assign an issue to someone and mark it urgent.",
    )


@pytest.fixture
def router_for(library):
    """A router over a shared fake workspace, with the event log it feeds."""
    def _make(payloads=None, *, confirm=None, actions=None):
        actions = actions or FakeScalekitActions()
        events = EventLog()
        forge = Forge(
            generator=ScriptedGenerator(payloads if payloads is not None
                                        else [payload(source=GOOD_SOURCE)]),
            library=library,
            events=events,
        )
        router = Router(
            forge=forge,
            library=library,
            client_factory=lambda who: BoundScopedClient(actions, who),
            simulator_factory=lambda: BoundScopedClient(FakeScalekitActions(), "priya@co"),
            detector=RuleIntentDetector(),
            roster=ROSTER,
            events=events,
            confirm=confirm,
        )
        return router, actions, events
    return _make


# --- gate 1: is this even a request? ----------------------------------------


@pytest.mark.parametrize("chatter", [
    "The SSO login bug is worse than we thought.",
    "Agreed, that's been broken for a while.",
    "We should probably escalate this before the sprint closes.",   # hedged verb
    "I wonder if someone should assign LIN-402 to Sam.",            # hedged + entity
    "Do you think we should close LIN-377?",
    "Nice, that fixed it.",
])
def test_chatter_is_ignored(router_for, chatter):
    router, actions, _ = router_for()
    outcome = router.handle(chatter, speaker="priya@co")

    assert outcome.decision is Decision.IGNORED, outcome.intent.confidence
    assert outcome.say() == ""
    assert actions.log == [], "ignored chatter still touched the workspace"


def test_being_named_alone_is_not_a_request(router_for):
    """A wake word without a verb must not clear the bar on its own."""
    router, _, _ = router_for()
    outcome = router.handle("Forge is pretty good at this", speaker="priya@co")
    assert outcome.decision is Decision.IGNORED


def test_a_clear_request_is_acted_on(router_for):
    router, actions, _ = router_for(confirm=lambda p: True)
    outcome = router.handle(ESCALATE, speaker="priya@co")

    assert outcome.decision is Decision.ACTED, outcome.reason
    assert outcome.observed["observed_assignee"] == "sam@co"
    assert outcome.observed["observed_priority"] == "Urgent"
    assert actions.workspace["issues"]["LIN-402"]["assignee"] == "sam@co"


def test_it_reports_observed_state_not_intent(router_for):
    router, _, _ = router_for(confirm=lambda p: True)
    said = router.handle(ESCALATE, speaker="priya@co").say()

    assert "sam@co" in said and "Urgent" in said and "Cycle 14" in said
    assert "will" not in said and "should" not in said


# --- gate 2: voice is not an authentication factor --------------------------


@pytest.mark.parametrize("confidence", [Confidence.UNKNOWN, Confidence.LOW])
def test_an_unidentified_speaker_cannot_change_anything(router_for, confidence):
    router, actions, _ = router_for()
    outcome = router.handle(ESCALATE, speaker="someone@unknown", confidence=confidence)

    assert outcome.decision is Decision.DENIED
    assert "who's speaking" in outcome.reason
    assert actions.log == []


def test_an_unidentified_speaker_may_still_ask_questions(router_for):
    router, _, _ = router_for()
    outcome = router.handle("Forge, triage LIN-402", speaker="guest@external",
                            confidence=Confidence.UNKNOWN)
    # A read-shaped request is not blocked by the identity gate; it falls through to
    # the ordinary path (and finds nothing that covers it).
    assert outcome.decision is not Decision.DENIED


# --- gate 3: disambiguation before action -----------------------------------


def test_two_candidate_issues_produce_a_question(router_for):
    router, actions, _ = router_for()
    outcome = router.handle("Forge, escalate LIN-402 and LIN-407 to Sam",
                            speaker="priya@co")

    assert outcome.decision is Decision.CLARIFY
    assert outcome.question == "did you mean LIN-402 or LIN-407?"
    assert actions.log == [], "asked a question but acted anyway"


def test_a_missing_argument_produces_a_question_not_a_guess(router_for):
    router, _, _ = router_for()
    # Names a target but no issue — the skill needs both.
    outcome = router.handle("Forge, escalate it to Sam and mark it urgent",
                            speaker="priya@co")

    assert outcome.decision is Decision.CLARIFY
    assert outcome.question == "which issue id?"


# --- gate 4: the scope ceiling ---------------------------------------------


def test_one_sentence_two_mouths(router_for):
    """The demo's second beat, through the router this time.

    Identical sentence, identical resolved arguments — "to me" resolves to whoever
    spoke. Priya's runs; Sam's is refused, and nothing changes.
    """
    router, actions, _ = router_for([assign_payload()], confirm=lambda p: True)

    as_priya = router.handle(ASSIGN, speaker="priya@co")
    assert as_priya.decision is Decision.ACTED, as_priya.reason
    assert as_priya.intent.args["escalate_to"] == "priya@co"

    actions.workspace["issues"]["LIN-402"].update(
        assignee="priya@co", priority="Medium", links=[])
    calls_before = len(actions.log)

    as_sam = router.handle(ASSIGN, speaker="sam@co")
    assert as_sam.decision is Decision.DENIED
    assert as_sam.intent.args["escalate_to"] == "sam@co"
    assert as_sam.blocked_at == "linear.update_issue"
    assert "outside your maker's mark" in as_sam.say()

    assert actions.workspace["issues"]["LIN-402"]["priority"] == "Medium"
    assert len(actions.log) == calls_before, "a denied request still called something"


def test_denial_happens_before_execution_not_halfway_through(router_for):
    """A refusal must not leave a half-finished action behind."""
    router, actions, _ = router_for([assign_payload()], confirm=lambda p: True)
    router.handle(ASSIGN, speaker="priya@co")          # forge it as Priya
    before = len(actions.log)

    outcome = router.handle("Forge, assign LIN-388 to me and mark it urgent",
                            speaker="sam@co")

    assert outcome.decision is Decision.DENIED
    assert outcome.blocked_at == "linear.update_issue"
    assert len(actions.log) == before, "denied request executed part of the skill"


# --- gate 5: trust and confirmation ----------------------------------------


def test_a_freshly_forged_skill_needs_one_approval_and_silence_is_not_consent(
    router_for
):
    """A newly forged skill is tempered, not trusted — it has passed its own test but
    has no track record. With no approval hook wired up it must not act: an absent
    human is not an approving one."""
    router, actions, _ = router_for()          # deliberately no confirm hook

    outcome = router.handle(ESCALATE, speaker="priya@co")

    assert outcome.decision is Decision.NEEDS_CONFIRMATION
    assert "not yet trusted to act alone" in outcome.reason
    assert outcome.skill.trust is Trust.TEMPERED
    assert actions.log == [], "acted without approval"
    assert "Confirm?" in outcome.say()


def test_trust_earned_over_clean_runs_removes_the_prompt(router_for, library):
    """The gate is not permanent — it lifts when the skill has a record."""
    approvals = []
    router, _, _ = router_for(confirm=lambda p: approvals.append(p) or True)
    router.handle(ESCALATE, speaker="priya@co")

    skill = library.load("escalate_and_rebalance")
    assert skill.trust is Trust.TEMPERED
    assert skill.manifest.needs_confirmation
    assert len(approvals) == 1

    # Enough clean executions and the skill stops asking.
    for _ in range(3):
        library.record_execution(skill, ok=True, duration_s=0.1)
    assert library.load("escalate_and_rebalance").trust is Trust.TRUSTED
    assert not library.load("escalate_and_rebalance").manifest.needs_confirmation


def test_a_destructive_skill_always_needs_a_human(router_for, library):
    from skillforge.core.library import new_skill
    from skillforge.core.manifest import CapabilityManifest

    router, actions, _ = router_for()
    nuke = new_skill(
        CapabilityManifest.from_dict({
            "skill": "delete_project", "app": "linear",
            "primitives_used": ["linear.get_issue"], "effects": "destructive",
            "reversible": False, "inverse": None, "forged_by": "priya@co",
            "trust": "quarantined",
            "description": "Delete a project and everything in it.",
        }),
        source='def run(scoped_client, issue_id):\n'
               '    return scoped_client.call("linear.get_issue", issue_id=issue_id)\n',
        test_source="def check(result, calls):\n    assert result\n",
    )
    library.register(nuke)
    library.temper(nuke)
    # Even trusted, destructive work needs a human.
    nuke.manifest.trust = Trust.TRUSTED
    library._save_manifest(nuke)

    outcome = router.handle("Forge, delete LIN-402 project", speaker="priya@co")

    assert outcome.decision is Decision.NEEDS_CONFIRMATION
    assert "destructive skills always need a human" in outcome.reason
    assert "delete_project@v1 as priya@co" in outcome.preview
    assert actions.log == []


def test_an_approved_confirmation_executes(router_for, library):
    asked = []

    def approve(preview):
        asked.append(preview)
        return True

    router, actions, _ = router_for(confirm=approve)
    first = router.handle(ESCALATE, speaker="priya@co")
    assert first.decision is Decision.ACTED

    skill = library.load("escalate_and_rebalance")
    assert skill.trust is Trust.TEMPERED
    assert skill.manifest.needs_confirmation, "tempered-not-trusted should ask"
    assert asked, "the confirm hook was never called"
    assert "as priya@co" in asked[0]


def test_a_declined_confirmation_does_not_execute(router_for):
    router, actions, _ = router_for(confirm=lambda preview: False)
    outcome = router.handle(ESCALATE, speaker="priya@co")

    assert outcome.decision is Decision.NEEDS_CONFIRMATION
    assert actions.log == []


# --- reuse and re-scoping --------------------------------------------------


def test_the_second_ask_reuses_and_re_scopes(router_for, library):
    """Priya forges it; Dana reuses it, running as Dana."""
    router, actions, _ = router_for(confirm=lambda p: True)

    first = router.handle(ESCALATE, speaker="priya@co")
    assert first.decision is Decision.ACTED and not first.reused

    second = router.handle(
        "Forge, escalate LIN-388 to me and mark it urgent, re-triage around it",
        speaker="dana@co")

    assert second.decision is Decision.ACTED, second.reason
    assert second.reused, "re-forged instead of recognizing"
    assert second.observed["observed_assignee"] == "dana@co"
    assert actions.log[-1]["identifier"] == "dana@co", "ran as the wrong person"

    stats = library.load("escalate_and_rebalance").stats
    assert stats.executions == 2


def test_reuse_does_not_call_the_generator_again(router_for):
    router, _, _ = router_for([payload(source=GOOD_SOURCE)], confirm=lambda p: True)
    router.handle(ESCALATE, speaker="priya@co")

    # Only one payload was scripted; a second forge would raise.
    second = router.handle(
        "Forge, escalate LIN-388 to me and mark it urgent, re-triage around it",
        speaker="dana@co")
    assert second.decision is Decision.ACTED
    assert second.reused


def test_a_failed_forge_is_reported_not_swallowed(router_for):
    bad = payload(source=GOOD_SOURCE.replace("linear.get_active_cycle",
                                             "linear.get_epic"))
    router, actions, _ = router_for([bad, bad, bad], confirm=lambda p: True)

    outcome = router.handle(ESCALATE, speaker="priya@co")

    assert outcome.decision is Decision.FAILED
    assert "gave up after 3 attempts" in outcome.reason
    assert "linear.get_epic" in outcome.say()
    assert actions.log == []


# --- the event stream ------------------------------------------------------


def test_the_router_emits_action_and_denial_events(router_for):
    router, _, events = router_for([assign_payload()], confirm=lambda p: True)
    router.handle(ASSIGN, speaker="priya@co")
    router.handle(ASSIGN, speaker="sam@co")

    kinds = [e["type"] for e in events.events]
    assert "action" in kinds and "action_denied" in kinds

    acted = next(e for e in events.events if e["type"] == "action")
    assert acted["actor"] == "priya@co"
    assert acted["observed"]["observed_priority"] == "Urgent"
    assert acted["scoped_calls"] == 2
    assert acted["utterance"] == ASSIGN

    denied = next(e for e in events.events if e["type"] == "action_denied")
    assert denied["actor"] == "sam@co"
    assert denied["primitive"] == "linear.update_issue"
    # Same utterance, opposite outcome — the only variable is who spoke.
    assert acted["utterance"] == denied["utterance"]


def test_an_identity_denial_records_why(router_for):
    router, _, events = router_for()
    router.handle(ESCALATE, speaker="ghost@nowhere", confidence=Confidence.UNKNOWN)

    denied = next(e for e in events.events if e["type"] == "action_denied")
    assert "identity confidence: unknown" in denied["note"]
    assert denied["primitive"] is None


# --- the detector on its own ------------------------------------------------


def test_to_me_resolves_to_whoever_spoke():
    detector = RuleIntentDetector()
    for speaker in ("priya@co", "sam@co"):
        intent = detector.detect("assign LIN-402 to me", speaker=speaker, roster=ROSTER)
        assert intent.args["escalate_to"] == speaker


def test_a_named_target_resolves_through_the_roster():
    intent = RuleIntentDetector().detect(
        "escalate LIN-402 to Dana", speaker="priya@co", roster=ROSTER)
    assert intent.args["escalate_to"] == "dana@co"


def test_an_unrostered_name_is_not_invented():
    intent = RuleIntentDetector().detect(
        "escalate LIN-402 to Jordan", speaker="priya@co", roster=ROSTER)
    assert "escalate_to" not in intent.args


def test_a_hedge_subtracts_rather_than_being_ignored():
    detector = RuleIntentDetector()
    direct = detector.detect("Forge, escalate LIN-402", speaker="p@co", roster=ROSTER)
    hedged = detector.detect("Forge, should we escalate LIN-402?",
                             speaker="p@co", roster=ROSTER)
    assert direct.is_confident
    assert not hedged.is_confident
    assert hedged.confidence < direct.confidence


def test_issue_keys_are_matched_narrowly():
    """A loose pattern matches dates and versions; a false entity is worse than none."""
    detector = RuleIntentDetector()
    for text in ("ship it by 2026-07", "we're on v2-1 now", "meeting at 10-30"):
        intent = detector.detect(f"Forge, escalate {text}", speaker="p@co", roster=ROSTER)
        assert "issue_id" not in intent.args, text


def test_reads_only_distinguishes_questions_from_changes():
    detector = RuleIntentDetector()
    assert detector.detect("Forge, triage LIN-402", speaker="p@co",
                           roster=ROSTER).reads_only
    assert not detector.detect("Forge, assign LIN-402 to me", speaker="p@co",
                               roster=ROSTER).reads_only
