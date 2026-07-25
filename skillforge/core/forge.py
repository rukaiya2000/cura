"""The anvil — introspect, generate, gate, temper, register.

    introspect   list_scoped_tools(identifier=speaker)   what THIS person holds
    recognize    already in the armory? reuse instead of re-inventing
    generate     the model composes those primitives into a new verb
    scope gate   declared primitives ⊆ granted primitives          ← hard refusal
    static gate  reconcile code against its manifest               ← hard refusal
    temper       run sandboxed against a simulator; generated test must pass
    register     into the armory, quarantined, then tempered on a pass

Two properties worth naming, because they are what make the rest of the product
defensible:

**The forge cannot exceed the speaker's scope.** The generator is shown only the
primitives the speaker was granted, and the scope gate re-checks the manifest against
that same set. A capability nobody granted cannot be composed, cannot be declared, and
cannot reach execution — there is no path from "someone asked" to "the code exists".

**A failure is an input, not an end.** Every rejection — malformed manifest, undeclared
reach, failed test — comes back as a reason, and the reason is what the next attempt is
generated from. That is the Reflexion loop, and it is the same loop whether the failure
came from the model, the gate, or the test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4
from typing import Callable

from ..adapters.llm import CodeGenerator, ForgeRequest, GenerationError
# The event vocabulary is shared between the core and the dashboard. It lives under ui/
# today and moves into core when the audit log lands.
from ..ui.events import (
    FORGE_CODE,
    FORGE_STAGE,
    FORGE_START,
    INTROSPECT,
    SKILL_REGISTERED,
    SKILL_TRUST,
    TEMPER_FAILED,
)
from .checker import reconcile
from .library import Skill, SkillLibrary, new_skill
from .manifest import CapabilityManifest, ManifestError, Trust
from .policy import Policy
from .sandbox import DEFAULT_TIMEOUT
from .temper import temper

DEFAULT_MAX_ATTEMPTS = 3

# --- recognition -----------------------------------------------------------------
#
# Lexical overlap is the wrong tool for this and the default below is a placeholder for
# an embedding lookup — but it is a *conservative* placeholder, chosen after both obvious
# metrics failed in opposite directions:
#
#   * Jaccard (÷ union) punishes a correct match for a wordy intent — the caller says
#     "escalate LIN-402 to Sam, mark it urgent, and re-triage what I was blocking" and
#     the task-specific tokens no skill description could contain sink the score.
#   * Coverage of the skill's own tokens (÷ skill vocabulary) punishes a correct match
#     for a wordy *description* — and the description is model-written, so its length
#     is not something we control.
#
# So: overlap coefficient (÷ the smaller side, which is scale-free in both directions),
# plus two guards that kill the failure mode a low threshold would otherwise open —
# a single shared generic noun must not match ("update the project" vs `delete_project`
# shares "project" and nothing else, and is emphatically not a match).
RECOGNITION_THRESHOLD = 0.3
MIN_SHARED_TOKENS = 2          # one shared word is a coincidence, not a match
REQUIRE_NAME_TOKEN = True      # the skill's own verb/noun must appear in the intent

_STOPWORDS = frozenset(
    "a an and the to for of in on at is it its this that with as by or be "
    "me my you your i we our them their he she they can could would should "
    "please just now then so if but all any".split()
)


def _tokens(text: str) -> set[str]:
    out = set()
    for raw in text.replace("_", " ").replace("-", " ").lower().split():
        word = "".join(ch for ch in raw if ch.isalnum())
        if word and word not in _STOPWORDS and not word.isdigit():
            out.add(word)
    return out


@dataclass
class Attempt:
    n: int
    ok: bool
    source: str = ""
    test_source: str = ""
    manifest: dict | None = None
    reason: str | None = None


@dataclass
class ForgeOutcome:
    ok: bool
    skill: Skill | None = None
    attempts: list[Attempt] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0
    reused: bool = False

    @property
    def attempts_made(self) -> int:
        return len(self.attempts)


class Forge:
    def __init__(
        self,
        *,
        generator: CodeGenerator,
        library: SkillLibrary,
        events=None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        timeout: float = DEFAULT_TIMEOUT,
        matcher: Callable[[str, Skill], float] | None = None,
        policy: Policy | None = None,
    ) -> None:
        self.generator = generator
        self.library = library
        self.events = events          # any object exposing .emit(at, type, **payload)
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.policy = policy or Policy()   # permits everything by default
        self._matcher = matcher or self.score_match
        self._t0 = time.monotonic()

    # --- events ------------------------------------------------------------

    def _emit(self, type: str, **payload) -> None:
        if self.events is not None:
            self.events.emit(time.monotonic() - self._t0, type, **payload)

    # --- recognition -------------------------------------------------------

    def score_match(self, intent: str, skill: Skill) -> float:
        """How well `skill` covers `intent`, in [0, 1]. Override to use embeddings.

        The one hook a vector search needs: replace this method (or pass `matcher=` to
        the constructor) and the rest of recognition — trust gating, scope gating, best-
        of selection — keeps working unchanged.
        """
        wanted = _tokens(intent)
        name_tokens = _tokens(skill.name)
        have = name_tokens | _tokens(skill.manifest.description)
        if not wanted or not have:
            return 0.0

        shared = wanted & have
        if len(shared) < MIN_SHARED_TOKENS:
            return 0.0
        if REQUIRE_NAME_TOKEN and not (wanted & name_tokens):
            return 0.0
        return len(shared) / min(len(wanted), len(have))

    def recognize(self, intent: str, *, granted: set[str],
                  require_granted: bool = True) -> Skill | None:
        """Find an armory skill that already covers this intent and this speaker.

        Two gates before scoring, both of which matter more than the score:

        * a quarantined skill is never offered — it hasn't earned the right to act;
        * a skill whose primitives the speaker lacks is not a match. It would only fail
          at execution, and calling that "reuse" would be a lie about what happened.

        `require_granted=False` drops the second gate, which the router uses to tell two
        very different situations apart: nothing covers this intent (forge), versus
        something covers it and this speaker may not use it (refuse). Without that
        distinction a denial looks like a gap, and the agent burns generation attempts
        producing a skill it was always going to refuse to run.
        """
        best, best_score = None, 0.0
        for skill in self.library.all_skills():
            if not skill.manifest.may_execute_for_real:
                continue
            if require_granted and not set(skill.manifest.primitives_used) <= granted:
                continue
            score = self._matcher(intent, skill)
            if score > best_score:
                best, best_score = skill, score

        return best if best_score >= RECOGNITION_THRESHOLD else None

    # --- the loop ----------------------------------------------------------

    def forge(
        self,
        *,
        intent: str,
        speaker: str,
        client,
        simulator_factory: Callable[[], object],
        kwargs: dict,
        speculative: bool = False,
        allow_reuse: bool = True,
    ) -> ForgeOutcome:
        """Forge a skill for `intent`, or reuse one that already covers it.

        `client` is the speaker's bound scoped client — used for introspection only;
        the forge never executes through it. `simulator_factory` returns a throwaway
        client for tempering, so a candidate proves itself without touching reality.
        """
        started = time.monotonic()
        # A forge cycle needs an identity of its own, because the *skill's* name is not
        # known until the model invents it — `forge_start` genuinely cannot carry one.
        # Consumers that correlate on the skill name therefore lose every event before
        # registration, which is exactly what the dashboard did until this existed.
        forge_id = uuid4().hex[:8]

        # Two ceilings, applied in the same breath: what the speaker was granted, and
        # what policy permits anyone to hold. The generator sees the intersection, so a
        # banned capability is never composed rather than being refused afterwards.
        tools = self.policy.filter_primitives(client.granted_tools())
        granted = {t["definition"]["name"] for t in tools}
        # Which services are on the table, discovered rather than configured. Named in the
        # prompt so the model knows a single skill may span them — "book the follow-up and
        # log it to the record" is one intent across two services, and a generator that
        # thinks it is confined to one will split it into something the router can't run.
        connected = sorted({p.split(".", 1)[0] for p in granted})

        if allow_reuse:
            existing = self.recognize(intent, granted=granted)
            if existing is not None:
                return ForgeOutcome(
                    ok=True, skill=existing, reused=True,
                    duration_s=time.monotonic() - started,
                )

        self._emit(FORGE_START, forge_id=forge_id, skill=None,
                   speculative=speculative, trigger=intent)
        self._emit(INTROSPECT, forge_id=forge_id, speaker=speaker,
                   primitives=sorted(granted),
                   note=f"composed from {speaker}'s granted primitives, discovered at forge time")
        self._emit(FORGE_STAGE, forge_id=forge_id, skill=None, stage="heating")

        attempts: list[Attempt] = []
        feedback: str | None = None
        previous_source: str | None = None

        for n in range(1, self.max_attempts + 1):
            request = ForgeRequest(
                intent=intent, speaker=speaker, apps=connected, tools=tools,
                args=kwargs,          # so the signature it writes matches the call site
                feedback=feedback, previous_source=previous_source, attempt=n,
            )
            self._emit(FORGE_STAGE, forge_id=forge_id, skill=None, stage="hammering")

            try:
                generation = self.generator.generate(request)
            except GenerationError as e:
                attempts.append(Attempt(n=n, ok=False, reason=str(e)))
                feedback, previous_source = str(e), previous_source
                continue

            source = generation.source
            previous_source = source
            self._emit(FORGE_CODE, forge_id=forge_id,
                       skill=generation.manifest.get("skill"),
                       attempt=n, source=source)

            reason = self._gate(generation, granted)
            if reason:
                attempts.append(Attempt(n=n, ok=False, source=source,
                                        test_source=generation.test_source,
                                        manifest=generation.manifest, reason=reason))
                self._emit(TEMPER_FAILED, forge_id=forge_id,
                           skill=generation.manifest.get("skill"),
                           attempt=n, reason=reason)
                feedback = reason
                continue

            manifest = CapabilityManifest.from_dict({
                **generation.manifest,
                "version": self.library.next_version(generation.manifest["skill"]),
            })
            candidate = new_skill(manifest, source, generation.test_source)

            self._emit(FORGE_STAGE, forge_id=forge_id, skill=candidate.name,
                       stage="tempering")
            outcome = temper(
                candidate,
                client_factory=simulator_factory,
                kwargs=kwargs,
                timeout=self.timeout,
            )
            if not outcome.ok:
                attempts.append(Attempt(n=n, ok=False, source=source,
                                        test_source=generation.test_source,
                                        manifest=generation.manifest,
                                        reason=outcome.reason))
                self._emit(TEMPER_FAILED, forge_id=forge_id, skill=candidate.name,
                           attempt=n, reason=outcome.reason)
                feedback = outcome.reason
                continue

            # Tempered. Register quarantined, then let the passing test move it.
            self.library.register(candidate)
            self._emit(FORGE_STAGE, forge_id=forge_id, skill=candidate.name,
                       stage="stamped")
            self._emit(SKILL_REGISTERED, forge_id=forge_id, skill=candidate.name,
                       version=candidate.version, trust=Trust.QUARANTINED.value,
                       manifest=candidate.manifest.to_dict(),
                       forge_duration_s=round(time.monotonic() - started, 2))
            self.library.temper(candidate)
            self._emit(SKILL_TRUST, forge_id=forge_id, skill=candidate.name,
                       trust=candidate.trust.value,
                       evidence="generated test passed against the simulator")

            attempts.append(Attempt(n=n, ok=True, source=source,
                                    test_source=generation.test_source,
                                    manifest=candidate.manifest.to_dict()))
            return ForgeOutcome(ok=True, skill=candidate, attempts=attempts,
                                duration_s=time.monotonic() - started)

        return ForgeOutcome(
            ok=False,
            attempts=attempts,
            error=(f"gave up after {self.max_attempts} attempts; "
                   f"last reason: {attempts[-1].reason}" if attempts else "no attempts made"),
            duration_s=time.monotonic() - started,
        )

    # --- gates -------------------------------------------------------------

    def _gate(self, generation, granted: set[str]) -> str | None:
        """Manifest, scope and static checks. Returns a failure reason, or None."""
        try:
            manifest = CapabilityManifest.from_dict({**generation.manifest, "version": 1})
        except (ManifestError, TypeError) as e:
            return f"manifest rejected: {e}"

        # The scope ceiling. Checked here as well as being implicit in what the
        # generator was shown, so a manifest can never widen its own reach.
        undeclared = set(manifest.primitives_used) - granted
        if undeclared:
            return (
                f"{sorted(undeclared)} not granted to {manifest.forged_by} — "
                "compose only from the primitives you were shown"
            )

        # The admin ceiling. Catches what primitive filtering can't express — an effect
        # class, a composition size, a missing inverse.
        verdict = self.policy.evaluate(manifest)
        if not verdict:
            return verdict.reason

        static = reconcile(generation.source, manifest)
        if not static.ok:
            return "; ".join(static.errors)
        return None
