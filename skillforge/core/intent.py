"""Turning what someone said into what they want done — or into nothing at all.

Most of what happens on a call is not a request. The hard part of this module is the
*negative* case: staying quiet through discussion, hedging, and thinking out loud, and
only surfacing when someone actually asked for something. A router that acts on chatter
is worse than one that acts too rarely, because the failure is invisible until it isn't.

`RuleIntentDetector` is deterministic and covers the demo's vocabulary honestly. Its
limits are real and worth naming: it knows the verbs it was given, it resolves names from
a roster, and it cannot understand a request phrased in a way nobody anticipated. A
model-backed detector satisfying the same protocol is the upgrade path — the router
depends on `IntentDetector`, never on the rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

#: Saying the agent's name is the strongest signal that a request is addressed to it.
WAKE_WORDS = frozenset({"forge"})

#: Verbs that make an utterance a request rather than a remark.
ACTION_VERBS = frozenset({
    "escalate", "assign", "reassign", "create", "file", "open", "close", "comment",
    "link", "update", "mark", "prioritise", "prioritize", "triage", "retriage",
    "move", "set", "add", "flag", "bump", "unblock", "delete", "remove",
})

#: Phrasings that signal thinking out loud rather than asking. Checked before verbs,
#: because "we should probably escalate this" contains an action verb and is not a request.
HEDGES = (
    "should we", "should i", "could we", "maybe we", "i wonder", "what if",
    "do you think", "someone should", "we should probably", "i was thinking",
    "it might be worth", "at some point", "eventually we",
)

#: Issue keys like LIN-402. Deliberately narrow — a loose pattern matches dates and
#: version numbers, and a false entity is worse than a missing one.
ISSUE_RE = re.compile(r"\b([A-Z]{2,6}-\d{1,6})\b")

#: A pronoun standing in for something the utterance never named.
DANGLING_REF = re.compile(r"\b(it|this|that|those|them|these)\b")

_MIN_CONFIDENT = 0.6


@dataclass
class Ambiguity:
    """Something the utterance left underdetermined. Ask, don't guess."""

    slot: str
    candidates: list[str]

    def question(self) -> str:
        if not self.candidates:
            # Nothing to choose between — the slot was never filled at all.
            return f"which {self.slot.replace('_', ' ')}?"
        options = " or ".join(self.candidates)
        return f"did you mean {options}?"


@dataclass
class Intent:
    utterance: str
    request: str                                   # what gets handed to the forge
    args: dict = field(default_factory=dict)
    confidence: float = 0.0
    ambiguities: list[Ambiguity] = field(default_factory=list)
    addressed: bool = False                        # the agent was named
    verbs: frozenset[str] = frozenset()

    @property
    def is_confident(self) -> bool:
        return self.confidence >= _MIN_CONFIDENT

    @property
    def reads_only(self) -> bool:
        """True when nothing in the utterance asks for a change.

        Used to let an unidentified speaker ask questions without being able to act.
        """
        return not (self.verbs - {"triage", "retriage"})


class IntentDetector(Protocol):
    def detect(self, utterance: str, *, speaker: str, roster: dict[str, str]) -> Intent: ...


class RuleIntentDetector:
    """Deterministic detection over a known verb set and a meeting roster.

    Confidence is additive over independent signals rather than a single rule, so no one
    signal can carry an utterance over the threshold on its own — being named is not
    enough without a verb, and a verb is not enough inside a hedge.
    """

    def __init__(self, *, wake_words: frozenset[str] = WAKE_WORDS,
                 action_verbs: frozenset[str] = ACTION_VERBS) -> None:
        self.wake_words = wake_words
        self.action_verbs = action_verbs

    def detect(self, utterance: str, *, speaker: str,
               roster: dict[str, str] | None = None) -> Intent:
        roster = roster or {}
        lowered = utterance.lower()
        words = {w.strip(".,!?:;'\"") for w in lowered.replace("-", "").split()}

        addressed = bool(words & self.wake_words)
        verbs = frozenset(words & self.action_verbs)
        hedged = any(h in lowered for h in HEDGES)

        confidence = 0.0
        if verbs:
            confidence += 0.45
        if addressed:
            confidence += 0.3
        if _imperative(lowered):
            confidence += 0.15
        issues = ISSUE_RE.findall(utterance)
        if issues:
            confidence += 0.1
        if hedged:
            # A hedge doesn't just fail to add signal — it actively argues against
            # acting, so it subtracts rather than being ignored.
            confidence -= 0.45

        args: dict = {}
        ambiguities: list[Ambiguity] = []

        if issues:
            args["issue_id"] = issues[0]
            if len(set(issues)) > 1:
                ambiguities.append(Ambiguity("issue_id", sorted(set(issues))))
        elif verbs and DANGLING_REF.search(lowered):
            # "escalate it to Sam" — a request with an unresolved referent. The slot is
            # underdetermined at the level of the utterance, so ask now rather than
            # forging a skill and discovering mid-temper that we have nothing to pass it.
            ambiguities.append(Ambiguity("issue_id", []))

        target = _resolve_target(lowered, speaker=speaker, roster=roster)
        if target:
            args["escalate_to"] = target

        return Intent(
            utterance=utterance,
            request=utterance,
            args=args,
            confidence=max(0.0, min(1.0, confidence)),
            ambiguities=ambiguities,
            addressed=addressed,
            verbs=verbs,
        )


def _imperative(lowered: str) -> bool:
    """Cheap check for a command: the utterance opens with a verb, or names the agent."""
    stripped = lowered.lstrip(",. ")
    for word in WAKE_WORDS:
        if stripped.startswith(word):
            return True
    first = stripped.split(" ")[0].strip(".,!?:;")
    return first.replace("-", "") in ACTION_VERBS


def _resolve_target(lowered: str, *, speaker: str, roster: dict[str, str]) -> str | None:
    """Who the action is aimed at: "to me" is the speaker; "to Sam" needs the roster."""
    match = re.search(r"\bto (me|myself)\b", lowered)
    if match:
        return speaker
    for name, identifier in roster.items():
        if re.search(rf"\bto {re.escape(name.lower())}\b", lowered):
            return identifier
    return None
