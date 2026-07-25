"""The demo's second beat, runnable from a terminal.

    .venv/bin/python scripts/two_mouths.py

Identical skill, identical arguments, two different speakers. Nothing about the request
changes — only who made it.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.adapters.fake_scoped import BoundScopedClient, FakeScalekitActions
from skillforge.core.library import SkillLibrary, new_skill
from skillforge.core.manifest import CapabilityManifest
from skillforge.core.sandbox import run_skill
from skillforge.core.temper import temper

SEED = ROOT / "seeds" / "escalate_and_rebalance"
UTTERANCE = "escalate LIN-402 to Sam, mark it urgent, and re-triage what I was blocking"

DIM, BOLD, ORANGE, BLUE, RED, GREEN, OFF = (
    "\033[2m", "\033[1m", "\033[38;5;208m", "\033[38;5;75m", "\033[31m", "\033[32m", "\033[0m",
)


def load_seed():
    return new_skill(
        CapabilityManifest.from_dict(json.loads((SEED / "manifest.json").read_text())),
        source=(SEED / "skill.py").read_text(),
        test_source=(SEED / "test.py").read_text(),
    )


def main():
    actions = FakeScalekitActions()
    library = SkillLibrary(ROOT / "armory")
    skill = load_seed()

    print(f"\n{BOLD}SkillForge — one sentence, two mouths{OFF}")
    print(f'{DIM}utterance:{OFF} "{UTTERANCE}"\n')

    # What each speaker is even allowed to be shown. The forge composes from this set,
    # so a capability outside it cannot be written, let alone run.
    for who in ("priya@co", "sam@co"):
        granted = sorted(BoundScopedClient(actions, who).granted_primitives())
        print(f"{DIM}scoped primitives for {who}:{OFF} {len(granted)}")
        for p in granted:
            print(f"    {DIM}·{OFF} {p}")
    print()

    # Temper it against a throwaway simulator before it is allowed anywhere near reality.
    outcome = temper(
        skill,
        client_factory=lambda: BoundScopedClient(FakeScalekitActions(), "priya@co"),
        kwargs={"issue_id": "LIN-402", "escalate_to": "sam@co"},
    )
    if not outcome.ok:
        print(f"{RED}temper failed:{OFF} {outcome.reason}")
        return 1
    library.register(skill, overwrite=True)
    library.temper(skill)
    print(f"{ORANGE}🔨 tempered{OFF} {skill.manifest.qualified_name} "
          f"{DIM}({outcome.sandbox.duration_s:.2f}s, "
          f"{len(outcome.sandbox.calls)} scoped calls){OFF}\n")

    kwargs = {"issue_id": "LIN-402", "escalate_to": "sam@co"}
    for who in ("priya@co", "sam@co"):
        result = run_skill(
            skill.source,
            client=BoundScopedClient(actions, who),
            kwargs=kwargs,
            allowed_primitives=set(skill.manifest.primitives_used),
        )
        library.record_execution(skill, ok=result.ok, denied=not result.ok,
                                 duration_s=result.duration_s)

        print(f"{BOLD}as {who}{OFF} {DIM}({result.duration_s:.2f}s){OFF}")
        if result.ok:
            r = result.result
            print(f"  {GREEN}✅ acted{OFF} — observed: {r['observed_assignee']}, "
                  f"{r['observed_priority']}, linked to {r['cycle']}")
            print(f"  {DIM}re-triaged:{OFF} {', '.join(r['rebalanced'])}")
        else:
            denied = next((c for c in result.calls if not c.ok), None)
            print(f"  {RED}🚫 OUTSIDE YOUR MARK{OFF} — {result.error}")
            if denied:
                print(f"  {DIM}blocked at:{OFF} {denied.primitive}")
            unchanged = actions.workspace["issues"]["LIN-402"]
            print(f"  {DIM}workspace unchanged:{OFF} assignee {unchanged['assignee']}, "
                  f"priority {unchanged['priority']}")
        print()

        # Reset between speakers so the second run starts from the same state.
        actions.workspace["issues"]["LIN-402"].update(assignee="priya@co", priority="Medium",
                                                     links=[])

    reloaded = library.load(skill.name)
    print(f"{BLUE}armory:{OFF} {reloaded.manifest.qualified_name} "
          f"trust={reloaded.trust.value} "
          f"executions={reloaded.stats.executions} denials={reloaded.stats.denials}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
