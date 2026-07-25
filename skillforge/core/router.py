"""The router — what the agent does with something someone said.

    utterance ─▶ detect ─▶ not a request?           ─▶ IGNORED
                        ─▶ underdetermined?         ─▶ CLARIFY
                        ─▶ identity not established? ─▶ DENIED (reads only)
                        ─▶ armory covers it?        ─▶ reuse
                        ─▶ nothing covers it?       ─▶ forge
                                                       │
                              scope check ─▶ outside your mark? ─▶ DENIED
                              trust check ─▶ needs a human?     ─▶ NEEDS_CONFIRMATION
                                                       ▼
                                                    execute ─▶ read back ─▶ ACTED

This is the module that turns a forge you call into an agent that decides, so its job is
mostly restraint. Four gates run before anything executes, in this order, and each one is
cheaper and more conservative than the next:

1. **Is this even a request?** Chatter and thinking out loud produce IGNORED.
2. **Do we know who is speaking?** An unidentified speaker gets reads and nothing else —
   voice is an intent signal, not an authentication factor.
3. **Does the speaker hold the primitives?** Checked before execution so a denial is a
   refusal rather than a half-finished action. The sandbox is still the backstop.
4. **Has the skill earned autonomy?** Quarantined or destructive work needs a human,
   and there is no way to grant that from inside the utterance.

Admin ceilings over manifests (deny `effects: destructive` org-wide, deny an app
outright) land in `policy.py`; the gates here are the ones derivable from the skill and
the speaker alone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..ui.events import ACTION, ACTION_DENIED, TRANSCRIPT
from .audit import AuditLog
from .checker import required_params
from .forge import Forge, ForgeOutcome
from .intent import Intent, IntentDetector, RuleIntentDetector
from .library import Skill, SkillLibrary
from .manifest import Effect
from .policy import Policy
from .sandbox import run_skill


class Confidence(str, Enum):
    """How sure we are who is speaking.

    Diarization distinguishes voices; it does not prove identity. `HIGH` means a roster
    match backed by a stable speaker cluster; `UNKNOWN` means someone is talking and we
    cannot say who — which is a normal state on a call with a guest, not an error.
    """

    HIGH = "high"
    LOW = "low"
    UNKNOWN = "unknown"


class Decision(str, Enum):
    IGNORED = "ignored"
    CLARIFY = "clarify"
    DENIED = "denied"
    NEEDS_CONFIRMATION = "needs_confirmation"
    ACTED = "acted"
    FAILED = "failed"


@dataclass
class Outcome:
    decision: Decision
    utterance: str
    speaker: str
    intent: Intent | None = None
    skill: Skill | None = None
    reused: bool = False
    observed: object = None
    reason: str | None = None
    question: str | None = None
    preview: str | None = None
    blocked_at: str | None = None
    forge: ForgeOutcome | None = None
    calls: list = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.decision is Decision.ACTED

    def say(self) -> str:
        """What the agent says back — observed state, never intent."""
        if self.decision is Decision.ACTED:
            observed = self.observed if isinstance(self.observed, dict) else {}
            facts = ", ".join(
                f"{k.replace('observed_', '').replace('_', ' ')} "
                f"{', '.join(v) if isinstance(v, list) else v}"
                for k, v in observed.items()
                if k.startswith("observed_") or k in ("cycle",)
            )
            return f"Done. {facts}." if facts else "Done."
        if self.decision is Decision.CLARIFY:
            return self.question or "Could you be more specific?"
        if self.decision is Decision.DENIED:
            return f"Can't hold that one — {self.reason}"
        if self.decision is Decision.NEEDS_CONFIRMATION:
            return f"Ready to run {self.preview}. Confirm?"
        if self.decision is Decision.FAILED:
            return f"Couldn't do that — {self.reason}"
        return ""


def _first_missing(skill: Skill, granted: set[str]) -> str | None:
    """The first declared primitive the speaker lacks, in declaration order.

    Declaration order rather than alphabetical, because the manifest lists primitives
    roughly in the order the skill calls them — so this names where execution *would*
    have stopped. Sorting instead would report whichever name happens to come first in
    the alphabet, which tells the user nothing about their request.
    """
    for primitive in skill.manifest.primitives_used:
        if primitive not in granted:
            return primitive
    return None


class Router:
    def __init__(
        self,
        *,
        forge: Forge,
        library: SkillLibrary,
        client_factory: Callable[[str], object],
        simulator_factory: Callable[[], object],
        detector: IntentDetector | None = None,
        roster: dict[str, str] | None = None,
        events=None,
        confirm: Callable[[str], bool] | None = None,
        policy: Policy | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.forge = forge
        self.library = library
        self.client_factory = client_factory
        self.simulator_factory = simulator_factory
        self.detector = detector or RuleIntentDetector()
        self.roster = roster or {}
        self.events = events
        #: Defaults to the forge's policy, so wiring one place configures both and the
        #: two layers cannot silently disagree about what is permitted.
        self.policy = policy or forge.policy
        self.audit = audit
        #: Human-in-the-loop hook. Absent means high-stakes work is never auto-approved —
        #: the safe default, because silence must not read as consent.
        self.confirm = confirm
        self._t0 = time.monotonic()

    def _emit(self, type: str, **payload) -> None:
        if self.events is not None:
            self.events.emit(time.monotonic() - self._t0, type, **payload)

    # --- the loop ----------------------------------------------------------

    def handle(
        self,
        utterance: str,
        *,
        speaker: str,
        confidence: Confidence = Confidence.HIGH,
        extra_args: dict | None = None,
    ) -> Outcome:
        started = time.monotonic()

        #: Outcomes worth a permanent record. IGNORED and CLARIFY are left out: neither
        #: touched anything, and logging every line of call chatter would bury the four
        #: kinds of entry a reviewer is actually looking for.
        audited = {Decision.ACTED, Decision.DENIED, Decision.FAILED,
                   Decision.NEEDS_CONFIRMATION}

        def done(outcome: Outcome) -> Outcome:
            outcome.duration_s = time.monotonic() - started
            if self.audit is not None and outcome.decision in audited:
                self.audit.record(
                    outcome=outcome.decision.value,
                    actor=speaker,
                    skill=outcome.skill,
                    utterance=utterance,
                    args=outcome.intent.args if outcome.intent else {},
                    calls=outcome.calls,
                    duration_s=outcome.duration_s,
                    blocked_at=outcome.blocked_at,
                    reason=outcome.reason,
                    identity_confidence=confidence.value,
                )
            return outcome

        # Everything said goes on the record, including what gets ignored. The ticker is
        # meant to show the whole call — a transcript containing only the lines that
        # triggered something hides the agent's most common and most important behaviour,
        # which is staying quiet.
        self._emit(TRANSCRIPT, speaker=speaker, text=utterance,
                   confidence=confidence.value)

        intent = self.detector.detect(utterance, speaker=speaker, roster=self.roster)

        # 1. Is this a request at all?
        if not intent.is_confident:
            return done(Outcome(Decision.IGNORED, utterance, speaker, intent=intent,
                                reason=f"not a request (confidence {intent.confidence:.2f})"))

        # 2. Do we know who is speaking? Reads are fine; changes are not.
        if confidence is not Confidence.HIGH and not intent.reads_only:
            reason = ("I can't confirm who's speaking, so I can only answer questions"
                      if confidence is Confidence.UNKNOWN
                      else "I'm not confident enough who's speaking to make changes")
            self._emit(ACTION_DENIED, skill=None, actor=speaker, utterance=utterance,
                       primitive=None, reason=reason,
                       note=f"identity confidence: {confidence.value}")
            return done(Outcome(Decision.DENIED, utterance, speaker, intent=intent,
                                reason=reason))

        # 3. Underdetermined? Ask rather than guess.
        if intent.ambiguities:
            amb = intent.ambiguities[0]
            return done(Outcome(Decision.CLARIFY, utterance, speaker, intent=intent,
                                question=amb.question()))

        client = self.client_factory(speaker)
        granted = client.granted_primitives()

        # 4. Reuse before re-inventing.
        skill = self.forge.recognize(intent.request, granted=granted)
        reused = skill is not None
        forge_outcome = None

        if skill is None:
            # Nothing the speaker can use covers this. Two very different reasons:
            # either nothing covers it at all, or something does and they may not run it.
            # Refuse the second case outright rather than forging a skill we would then
            # decline to execute.
            covered = self.forge.recognize(intent.request, granted=granted,
                                           require_granted=False)
            if covered is not None:
                blocked = _first_missing(covered, granted)
                reason = "that's outside your maker's mark"
                self.library.record_denial(covered)
                self._emit(ACTION_DENIED, skill=covered.name, actor=speaker,
                           utterance=utterance, primitive=blocked,
                           reason=f"{speaker} is not granted {blocked!r} — {reason}")
                return done(Outcome(Decision.DENIED, utterance, speaker, intent=intent,
                                    skill=covered, reason=reason, blocked_at=blocked))

            forge_outcome = self.forge.forge(
                intent=intent.request,
                speaker=speaker,
                client=client,
                simulator_factory=self.simulator_factory,
                kwargs={**intent.args, **(extra_args or {})},
                allow_reuse=False,
            )
            if not forge_outcome.ok:
                return done(Outcome(Decision.FAILED, utterance, speaker, intent=intent,
                                    reason=forge_outcome.error, forge=forge_outcome))
            skill = forge_outcome.skill
            reused = forge_outcome.reused

        manifest = skill.manifest

        # 5. Does the speaker hold everything the skill touches? Refuse before acting,
        #    not halfway through it.
        blocked = _first_missing(skill, granted)
        if blocked:
            reason = "that's outside your maker's mark"
            self.library.record_denial(skill)
            self._emit(ACTION_DENIED, skill=skill.name, actor=speaker,
                       utterance=utterance, primitive=blocked,
                       reason=f"{speaker} is not granted {blocked!r} — {reason}")
            return done(Outcome(Decision.DENIED, utterance, speaker, intent=intent,
                                skill=skill, reason=reason, blocked_at=blocked))

        # 6. Did the request supply everything the skill needs?
        args = {**intent.args, **(extra_args or {})}
        needed = [p for p in required_params(skill.source) if p not in args]
        if needed:
            slot = needed[0].replace("_", " ")
            return done(Outcome(Decision.CLARIFY, utterance, speaker, intent=intent,
                                skill=skill, question=f"which {slot}?"))

        # 7. Does policy still permit this? Re-checked at execution, not just at forge
        #    time — a reused skill was gated under whatever policy was in force when it
        #    was forged, and that may since have tightened.
        verdict = self.policy.evaluate(manifest)
        if not verdict:
            self.library.record_denial(skill)
            self._emit(ACTION_DENIED, skill=skill.name, actor=speaker,
                       utterance=utterance, primitive=None, reason=verdict.reason,
                       note="blocked by policy, not by scope")
            return done(Outcome(Decision.DENIED, utterance, speaker, intent=intent,
                                skill=skill, reason=verdict.reason))

        # 8. Has the skill earned the right to act unattended?
        preview = self._preview(skill, args, speaker)
        if manifest.needs_confirmation or self.policy.needs_confirmation(manifest):
            approved = self.confirm(preview) if self.confirm else False
            if not approved:
                return done(Outcome(Decision.NEEDS_CONFIRMATION, utterance, speaker,
                                    intent=intent, skill=skill, preview=preview,
                                    reason=self._why_confirm(manifest)))

        # 9. Act, then read back what actually happened.
        result = run_skill(
            skill.source,
            client=client,
            kwargs=args,
            allowed_primitives=set(manifest.primitives_used),
        )
        self.library.record_execution(skill, ok=result.ok, denied=not result.ok,
                                     duration_s=result.duration_s)

        if not result.ok:
            blocked = next((c.primitive for c in result.calls if not c.ok), None)
            self._emit(ACTION_DENIED, skill=skill.name, actor=speaker,
                       utterance=utterance, primitive=blocked, reason=result.error)
            return done(Outcome(Decision.DENIED, utterance, speaker, intent=intent,
                                skill=skill, reason=result.error, blocked_at=blocked,
                                calls=result.calls))

        self._emit(ACTION, skill=skill.name, actor=speaker, ok=True, reused=reused,
                   utterance=utterance, duration_s=round(result.duration_s, 3),
                   scoped_calls=len(result.calls), observed=result.result)
        return done(Outcome(Decision.ACTED, utterance, speaker, intent=intent,
                            skill=skill, reused=reused, observed=result.result,
                            forge=forge_outcome, calls=result.calls))

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _preview(skill: Skill, args: dict, speaker: str) -> str:
        shown = ", ".join(f"{k}={v!r}" for k, v in sorted(args.items()))
        return f"{skill.manifest.qualified_name} as {speaker} ({shown})"

    @staticmethod
    def _why_confirm(manifest) -> str:
        if manifest.effects is Effect.DESTRUCTIVE:
            return "destructive skills always need a human"
        return f"{manifest.skill} is {manifest.trust.value}, not yet trusted to act alone"
