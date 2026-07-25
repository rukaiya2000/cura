"""Play a call transcript through the router and watch what it decides.

    .venv/bin/python scripts/call.py              in-memory Linear, canned generation
    .venv/bin/python scripts/call.py --scalekit   real Scalekit, real Linear, real model

Every line of the transcript goes in, including the ones that are not requests. Those
matter most: a router that fires on discussion fails invisibly, so the `· ignored` lines
are the ones doing the quiet work.

Approval is wired to auto-approve here so the run completes unattended. In a real
deployment `confirm` is a human — a Slack DM or a click in the Armory — and a freshly
forged skill genuinely waits for it.

**`--scalekit` writes to a real Linear workspace.** Two consequences worth understanding
before running it:

*Tempering still runs against the in-memory simulator, never against Linear.* A candidate
skill has passed no test yet, so letting it loose on real data to find out whether it
works would invert the entire point of quarantine. The cost of that choice is fidelity —
the simulator's responses are not Linear's — so a skill can temper green and still stumble
on the real API. The production answer is a dedicated sandbox workspace to temper against;
a simulator is the honest interim, not the destination.

*Canned skills are unusable here.* The scripted payloads name primitives from the fake
catalogue, and Scalekit's real tool names differ, so they would be refused by the scope
gate before running. `--scalekit` therefore implies real generation.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.adapters.fake_scoped import BoundScopedClient, FakeScalekitActions
from skillforge.adapters.llm import ClaudeGenerator, ScriptedGenerator
from skillforge.adapters.scalekit_client import (
    NoConnectedAccount,
    ScalekitActions,
    ScalekitScopedClient,
)
from skillforge.config import get, load_env
from skillforge.core.audit import AuditLog
from skillforge.core.forge import Forge
from skillforge.core.intent import RuleIntentDetector
from skillforge.core.library import SkillLibrary
from skillforge.core.policy import STRICT
from skillforge.core.router import Confidence, Decision, Router
from skillforge.ui.events import EventLog

DIM, BOLD, ORANGE, BLUE, GOLD, RED, GREEN, GREY, OFF = (
    "\033[2m", "\033[1m", "\033[38;5;208m", "\033[38;5;75m", "\033[38;5;179m",
    "\033[31m", "\033[32m", "\033[38;5;244m", "\033[0m",
)

ROSTER = {"priya": "priya@co", "sam": "sam@co", "dana": "dana@co"}
NAMES = {"priya@co": ("Priya", "PM"), "sam@co": ("Sam", "Contractor"),
         "dana@co": ("Dana", "Eng manager"), "guest@external": ("Guest", "External")}

#: speaker, identity confidence, what they said
TRANSCRIPT = [
    ("priya@co", Confidence.HIGH, "The SSO login bug is worse than we thought."),
    ("sam@co", Confidence.HIGH, "Agreed. Someone should own it before the sprint closes."),
    ("priya@co", Confidence.HIGH, "We should probably escalate this at some point."),
    ("priya@co", Confidence.HIGH,
     "Forge — escalate LIN-402 to Sam, mark it urgent, and re-triage what I was blocking"),
    ("sam@co", Confidence.HIGH, "Forge, escalate LIN-402 to me and mark it urgent"),
    ("dana@co", Confidence.HIGH,
     "Forge, escalate LIN-388 to me, mark it urgent and re-triage around it"),
    ("guest@external", Confidence.UNKNOWN, "Forge, escalate LIN-377 to me"),
    ("priya@co", Confidence.HIGH, "Forge, escalate it to Dana"),
    ("priya@co", Confidence.HIGH, "Forge, escalate LIN-377 and LIN-391 to Dana"),
]

DECISION_STYLE = {
    Decision.IGNORED: (GREY, "· ignored"),
    Decision.CLARIFY: (GOLD, "? clarify"),
    Decision.DENIED: (RED, "🚫 denied"),
    Decision.NEEDS_CONFIRMATION: (GOLD, "⏸ needs a human"),
    Decision.ACTED: (GREEN, "✅ acted"),
    Decision.FAILED: (RED, "✗ failed"),
}


def scripted():
    """Canned attempts: the first reaches past the introspected primitive set."""
    from tests.test_forge import BAD_SOURCE, GOOD_SOURCE, GOOD_TEST, FULL_PRIMITIVES

    base = {
        "skill": "escalate_and_rebalance",
        "description": "Escalate an issue to someone, mark it urgent, pull it into the "
                       "active cycle, and re-triage the previous assignee's work.",
        "primitives_used": list(FULL_PRIMITIVES),
        "effects": "write",
        "reversible": True,
        "inverse": "restore_snapshot",
        "test_source": GOOD_TEST,
    }
    return [{**base, "source": BAD_SOURCE}, {**base, "source": GOOD_SOURCE}]


def wire_scalekit():
    """Build the real-world wiring, or explain precisely what's missing.

    Returns `(client_factory, roster, names, transcript)` or None after printing why.
    """
    load_env()
    people = {
        "priya@co": ("SKILLFORGE_IDENTIFIER_FULL", "Priya", "PM"),
        "sam@co": ("SKILLFORGE_IDENTIFIER_LIMITED", "Sam", "Contractor"),
        "dana@co": ("SKILLFORGE_IDENTIFIER_PEER", "Dana", "Eng manager"),
    }
    resolved, missing = {}, []
    for placeholder, (key, _, _) in people.items():
        value = get(key)
        (resolved.__setitem__(placeholder, value) if value else missing.append(key))

    required = ["SKILLFORGE_IDENTIFIER_FULL", "SKILLFORGE_IDENTIFIER_LIMITED"]
    if any(k in missing for k in required):
        print(f"\n{RED}--scalekit needs two connected accounts{OFF}\n")
        for key in required:
            state = f"{GREEN}set{OFF}" if get(key) else f"{RED}missing{OFF}"
            print(f"  {key:<32} {state}")
        print(f"\n{DIM}An identifier string is not enough — each must have completed "
              f"Scalekit's OAuth flow for the connector, so a connected account exists "
              f"behind it. And the two must hold genuinely different Linear permissions: "
              f"two accounts with the same grants make the denial beat pass while "
              f"proving nothing.{OFF}")
        print(f"{DIM}Check what's configured: "
              f".venv/bin/python -m skillforge.config{OFF}\n")
        return None

    try:
        actions = ScalekitActions.from_env()
    except Exception as e:
        print(f"\n{RED}could not reach Scalekit{OFF} — {type(e).__name__}: {e}\n")
        return None

    # Confirm each account is actually connected before playing a transcript at it —
    # discovering it mid-call produces a confusing denial rather than a clear message.
    print(f"\n{BOLD}Connected accounts{OFF} {DIM}(connection: "
          f"{actions.connection!r}){OFF}")
    holdings = {}
    for placeholder, identifier in resolved.items():
        _, name, _ = people[placeholder]
        try:
            tools = ScalekitScopedClient(actions, identifier).granted_primitives()
        except NoConnectedAccount as e:
            print(f"  {name:<6} {RED}not connected{OFF} — {e}")
            return None
        except Exception as e:
            print(f"  {name:<6} {RED}{type(e).__name__}{OFF}: {e}")
            return None
        holdings[name] = tools
        print(f"  {name:<6} {GREEN}{len(tools)} tools{OFF} {DIM}{identifier}{OFF}")

    if len(holdings) > 1 and len(set(map(frozenset, holdings.values()))) == 1:
        print(f"\n  {RED}every account holds an identical tool set{OFF}")
        print(f"  {DIM}The denial beat cannot work here: nothing distinguishes these "
              f"speakers. Either their Linear permissions genuinely match, or grants are "
              f"per-connection rather than per-tool — see scripts/probe_scalekit.py.{OFF}")

    transcript = [(resolved.get(s, s), c, u) for s, c, u in TRANSCRIPT
                  if s in resolved or s == "guest@external"]
    roster = {}
    for placeholder, identifier in resolved.items():
        _, name, _ = people[placeholder]
        roster[name.lower()] = identifier
    names = {identifier: (people[p][1], people[p][2])
             for p, identifier in resolved.items()}
    names["guest@external"] = ("Guest", "External")

    return (lambda who: ScalekitScopedClient(actions, who)), roster, names, transcript


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scalekit", action="store_true",
                        help="run against real Scalekit + Linear (writes to a real "
                             "workspace; implies real generation)")
    args = parser.parse_args()

    library = SkillLibrary(ROOT / "armory")
    # One clock across forge and router, flushed to disk on every event so the Armory
    # can follow along while this runs.
    events = EventLog(path=ROOT / "build" / "events.json", live=True)
    audit = AuditLog(ROOT / "build" / "audit.jsonl")
    roster, names, transcript = ROSTER, NAMES, TRANSCRIPT

    if args.scalekit:
        wiring = wire_scalekit()
        if wiring is None:
            return 2
        client_factory, roster, names, transcript = wiring
        if not get("ANTHROPIC_API_KEY"):
            print(f"\n{RED}--scalekit needs ANTHROPIC_API_KEY too{OFF} — canned skills "
                  f"name fake primitives and would be refused by the scope gate.\n")
            return 2
        generator = ClaudeGenerator(effort=os.environ.get("SKILLFORGE_EFFORT", "high"))
        banner = (f"{RED}LIVE{OFF} — real Scalekit, real Linear writes, real generation")
    else:
        actions = FakeScalekitActions()
        client_factory = lambda who: BoundScopedClient(actions, who)  # noqa: E731
        generator = ScriptedGenerator(scripted())
        banner = f"{DIM}in-memory Linear · canned generation · --scalekit for real{OFF}"

    forge = Forge(generator=generator, library=library, events=events, policy=STRICT)
    router = Router(
        forge=forge,
        library=library,
        client_factory=client_factory,
        # Always the simulator, even in live mode. An untempered skill has passed no test
        # yet; letting it loose on real data to find out whether it works would invert
        # the point of quarantine. Fidelity is the price — see the module docstring.
        simulator_factory=lambda: BoundScopedClient(FakeScalekitActions(), "priya@co"),
        detector=RuleIntentDetector(),
        roster=roster,
        events=events,
        confirm=lambda preview: True,      # a human, in a real deployment
        audit=audit,
    )

    print(f"\n{BOLD}A call, played through the router{OFF}  {banner}")
    print(f"{DIM}every line goes in, including the ones that aren't requests{OFF}\n")
    NAMES.update(names)
    TRANSCRIPT[:] = transcript

    for speaker, confidence, utterance in TRANSCRIPT:
        name, role = NAMES.get(speaker, (speaker, "?"))
        tier = "" if confidence is Confidence.HIGH else f" {RED}[identity: {confidence.value}]{OFF}"
        print(f"{BOLD}{name}{OFF} {DIM}· {role}{OFF}{tier}")
        print(f'  {GREY}"{utterance}"{OFF}')

        outcome = router.handle(utterance, speaker=speaker, confidence=confidence)
        colour, label = DECISION_STYLE[outcome.decision]
        detail = ""
        if outcome.skill is not None:
            detail = f" {DIM}{outcome.skill.manifest.qualified_name}"
            if outcome.blocked_at:
                # A refused request neither forged nor reused — say what stopped it.
                detail += f" · blocked at {outcome.blocked_at}"
            elif outcome.decision is Decision.ACTED:
                detail += f" · {'reused' if outcome.reused else 'freshly forged'}"
            detail += OFF

        print(f"  {colour}{label}{OFF}{detail}")
        if outcome.say():
            print(f"  {DIM}forge:{OFF} {outcome.say()}")
        if outcome.decision is Decision.IGNORED:
            print(f"  {DIM}{outcome.reason}{OFF}")
        print()

    # What the session amounts to.
    acted = [e for e in events.events if e["type"] == "action"]
    denied = [e for e in events.events if e["type"] == "action_denied"]
    forged = [e for e in events.events if e["type"] == "skill_registered"]
    reused = [e for e in acted if e.get("reused")]

    print(f"{BOLD}Session{OFF}")
    print(f"  actions taken            {BOLD}{len(acted)}{OFF}")
    print(f"  via self-forged skills   {BOLD}{len(acted)}{OFF} "
          f"{DIM}({len(reused)} by reuse){OFF}")
    print(f"  human-written integrations {BLUE}0{OFF}")
    print(f"  scope violations blocked {RED}{len(denied)}{OFF}")
    if forged:
        print(f"  time to new capability   {ORANGE}{forged[0]['forge_duration_s']}s{OFF} "
              f"{DIM}vs. an engineering ticket{OFF}")

    print(f"\n{BOLD}Armory{OFF}")
    for skill in library.all_skills():
        stats = skill.stats
        print(f"  {skill.manifest.qualified_name} {DIM}·{OFF} "
              f"{BLUE}{skill.trust.value}{OFF} {DIM}· {stats.executions} run(s), "
              f"{stats.denials} denied, mark {skill.manifest.forged_by}{OFF}")

    # The audit trail — and proof it hasn't been touched.
    intact, problems = audit.verify()
    seal = f"{GREEN}chain intact{OFF}" if intact else f"{RED}TAMPERED{OFF}"
    print(f"\n{BOLD}Audit trail{OFF} {DIM}({audit.path.relative_to(ROOT)}){OFF} · {seal}")
    print(f"  {DIM}{'#':>2}  {'actor':<16}{'skill':<28}{'outcome':<10}detail{OFF}")
    for row in audit.table():
        colour = GREEN if row["outcome"] == "acted" else RED
        undo = f" {DIM}· undoable{OFF}" if row["reversible"] else ""
        print(f"  {row['seq']:>2}  {row['actor']:<16}{row['skill']:<28}"
              f"{colour}{row['outcome']:<10}{OFF}{GREY}{row['detail']}{OFF}{undo}")
    for problem in problems:
        print(f"  {RED}{problem}{OFF}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
