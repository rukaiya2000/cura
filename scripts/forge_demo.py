"""Watch the anvil work, from a terminal.

    .venv/bin/python scripts/forge_demo.py           # scripted generator, no API key
    .venv/bin/python scripts/forge_demo.py --live    # real generation via Claude

The scripted run is deliberately rigged so attempt 1 reaches for a primitive that does
not exist in the introspected set — the same failure the demo shows — so the Reflexion
retry is visible rather than described.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.adapters.fake_scoped import BoundScopedClient, FakeScalekitActions
from skillforge.config import load_env
from skillforge.adapters.llm import ClaudeGenerator, ScriptedGenerator
from skillforge.core.forge import Forge
from skillforge.core.library import SkillLibrary
from skillforge.core.sandbox import run_skill
from skillforge.ui.events import EventLog

DIM, BOLD, ORANGE, BLUE, GOLD, RED, GREEN, OFF = (
    "\033[2m", "\033[1m", "\033[38;5;208m", "\033[38;5;75m", "\033[38;5;179m",
    "\033[31m", "\033[32m", "\033[0m",
)

INTENT = "escalate LIN-402 to Sam, mark it urgent, and re-triage what I was blocking"
KWARGS = {"issue_id": "LIN-402", "escalate_to": "sam@co"}

STAGE_GLYPH = {"heating": "🔥", "hammering": "⚒", "tempering": "💧", "stamped": "✅"}


def scripted_payloads():
    """Two canned attempts: the first reaches past the introspected set."""
    from tests.test_forge import BAD_SOURCE, GOOD_SOURCE, GOOD_TEST, FULL_PRIMITIVES

    base = {
        "skill": "escalate_and_rebalance",
        "description": "Escalate an issue to someone, mark it urgent, pull it into the "
                       "active cycle, and flag the previous assignee's remaining work.",
        "primitives_used": list(FULL_PRIMITIVES),
        "effects": "write",
        "reversible": True,
        "inverse": "restore_snapshot",
        "test_source": GOOD_TEST,
    }
    return [{**base, "source": BAD_SOURCE}, {**base, "source": GOOD_SOURCE}]


def show_code(source: str, *, indent: str = "    ") -> None:
    width = max(40, min(shutil.get_terminal_size((100, 24)).columns - 8, 96))
    for i, line in enumerate(source.rstrip().split("\n"), 1):
        print(f"{indent}{DIM}{i:>3}{OFF} {line[:width]}")


def _render(value, prefix: str = "", depth: int = 0) -> list[str]:
    """Flatten an arbitrary returned structure into readable lines.

    A forged skill's return shape is its own invention, so this cannot assume keys.
    """
    pad = "  " * depth
    if isinstance(value, dict):
        out = []
        for key, val in value.items():
            label = key.replace("_", " ")
            if isinstance(val, (dict, list)) and val:
                out.append(f"{pad}{DIM}{label}:{OFF}")
                out += _render(val, depth=depth + 1)
            else:
                shown = ", ".join(str(v) for v in val) if isinstance(val, list) else val
                out.append(f"{pad}{DIM}{label}:{OFF} {shown if shown != '' else '—'}")
        return out
    if isinstance(value, list):
        return [line for item in value for line in _render(item, depth=depth)]
    return [f"{pad}{value}"]


def render(event: dict) -> None:
    t, kind = event["at"], event["type"]
    stamp = f"{DIM}{t:>6.2f}s{OFF}"

    if kind == "forge_start":
        tag = f" {ORANGE}(speculative){OFF}" if event.get("speculative") else ""
        print(f"{stamp}  {BOLD}anvil lit{OFF}{tag}")
    elif kind == "introspect":
        print(f"{stamp}  introspected {BOLD}{len(event['primitives'])}{OFF} primitives "
              f"for {event['speaker']}")
        for p in event["primitives"]:
            print(f"          {DIM}·{OFF} {p}")
    elif kind == "forge_stage":
        print(f"{stamp}  {STAGE_GLYPH.get(event['stage'], '·')} {event['stage']}")
    elif kind == "forge_code":
        print(f"{stamp}  {ORANGE}attempt {event['attempt']}{OFF} — generated:")
        show_code(event["source"])
    elif kind == "temper_failed":
        print(f"{stamp}  {RED}temper failed{OFF} — {event['reason']}")
        print(f"          {DIM}↳ that reason is the next attempt's input{OFF}")
    elif kind == "skill_registered":
        print(f"{stamp}  registered {BOLD}{event['skill']}@v{event['version']}{OFF} "
              f"{DIM}trust={event['trust']} forged in {event['forge_duration_s']}s{OFF}")
    elif kind == "skill_trust":
        print(f"{stamp}  {BLUE}trust → {event['trust']}{OFF} "
              f"{DIM}({event['evidence']}){OFF}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="generate with Claude instead of canned attempts")
    args = parser.parse_args()

    actions = FakeScalekitActions()
    library = SkillLibrary(ROOT / "armory")
    events = EventLog()

    if args.live:
        load_env()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(f"\n{RED}--live needs ANTHROPIC_API_KEY{OFF} — add it to .env "
                  f"(see .env.example), then re-run.\n"
                  f"{DIM}Check what's configured: "
                  f".venv/bin/python -m skillforge.config{OFF}\n")
            return 2
        generator = ClaudeGenerator(effort=os.environ.get("SKILLFORGE_EFFORT", "high"))
        print(f"\n{BOLD}Live generation{OFF} {DIM}(claude-opus-5){OFF}")
    else:
        generator = ScriptedGenerator(scripted_payloads())
        print(f"\n{BOLD}Scripted generation{OFF} {DIM}(no API key needed; "
              f"--live for real generation){OFF}")

    forge = Forge(generator=generator, library=library, events=events)

    print(f'{DIM}utterance:{OFF} "{INTENT}"\n')

    outcome = forge.forge(
        intent=INTENT,
        speaker="priya@co",
        client=BoundScopedClient(actions, "priya@co"),
        # Tempering runs against a throwaway workspace, never the real one.
        simulator_factory=lambda: BoundScopedClient(FakeScalekitActions(), "priya@co"),
        kwargs=KWARGS,
        speculative=True,
        allow_reuse=False,
    )

    for event in events.events:
        render(event)

    if not outcome.ok:
        print(f"\n{RED}forge failed:{OFF} {outcome.error}\n")
        return 1

    skill = outcome.skill
    print(f"\n{GREEN}✅ forged{OFF} {BOLD}{skill.manifest.qualified_name}{OFF} "
          f"in {outcome.duration_s:.2f}s over {outcome.attempts_made} attempt(s)")
    print(f"{DIM}manifest:{OFF}")
    for line in json.dumps(skill.manifest.to_dict(), indent=2).split("\n"):
        print(f"    {line}")

    # Now execute it for real, as Priya — the forge only ever tempered against a copy.
    print(f"\n{BOLD}executing as priya@co{OFF}")
    result = run_skill(
        skill.source,
        client=BoundScopedClient(actions, "priya@co"),
        kwargs=KWARGS,
        allowed_primitives=set(skill.manifest.primitives_used),
    )
    if result.ok:
        # Render whatever the skill chose to return. Nothing may assume a fixed shape
        # here — the skill was invented, so its return keys were too. (An earlier version
        # of this script hardcoded the fixture's keys and crashed the first time a real
        # model returned its own.)
        print(f"  {GREEN}observed{OFF} {DIM}({len(result.calls)} scoped calls){OFF}")
        for line in _render(result.result):
            print(f"    {line}")
    else:
        print(f"  {RED}failed{OFF} — {result.error}")

    # And the same skill, same arguments, as someone who lacks the grants.
    print(f"\n{BOLD}the same skill as sam@co{OFF}")
    denied = run_skill(
        skill.source,
        client=BoundScopedClient(actions, "sam@co"),
        kwargs=KWARGS,
        allowed_primitives=set(skill.manifest.primitives_used),
    )
    blocked = next((c for c in denied.calls if not c.ok), None)
    print(f"  {RED}🚫 OUTSIDE YOUR MARK{OFF} — blocked at "
          f"{blocked.primitive if blocked else '?'}")

    # Recognition: the second ask never reaches the generator.
    reused = forge.recognize(INTENT, granted=BoundScopedClient(
        actions, "dana@co").granted_primitives())
    print(f"\n{GOLD}armory:{OFF} dana@co asking the same thing → "
          f"{BOLD}{reused.manifest.qualified_name if reused else 'no match'}{OFF} "
          f"{DIM}(recognized, not re-forged){OFF}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
