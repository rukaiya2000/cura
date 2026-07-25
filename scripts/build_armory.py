"""Build the Armory page by inlining an event timeline into the template.

    .venv/bin/python scripts/build_armory.py            real session if one exists
    .venv/bin/python scripts/build_armory.py --demo     force the scripted timeline

Prefers `build/events.json` — a real session recorded by `scripts/call.py` — and falls
back to the hand-authored `demo_timeline()` when none exists. That default matters: a
dashboard showing a scripted recording of something the code now genuinely does is the
least honest artifact in the project, and preferring the real one fixes that by default
rather than by remembering a flag.

Either way `skillforge/ui/events.py` stays the single source of truth for event shape, so
the dashboard and the agent core cannot drift apart on what an event looks like.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.ui.events import EventLog, demo_timeline

TEMPLATE = ROOT / "skillforge" / "ui" / "armory.template.html"
OUT = ROOT / "build" / "armory.html"
SESSION = ROOT / "build" / "events.json"
TOKEN = "__TIMELINE_JSON__"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true",
                        help="use the scripted timeline even if a real session exists")
    parser.add_argument("--session", type=Path, default=SESSION)
    args = parser.parse_args()

    template = TEMPLATE.read_text()
    if TOKEN not in template:
        print(f"error: {TOKEN} not found in {TEMPLATE}", file=sys.stderr)
        return 1

    if not args.demo and args.session.is_file():
        log = EventLog.load(args.session)
        source = f"real session · {args.session.relative_to(ROOT)}"
    else:
        log = demo_timeline()
        source = "scripted demo timeline"
        if not args.demo:
            source += f"  (no session at {args.session.relative_to(ROOT)} — "
            source += "run scripts/call.py to record one)"

    # `<` is escaped so the JSON can never close the surrounding script element.
    payload = log.to_json().replace("<", "\\u003c")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(template.replace(TOKEN, payload))

    print(f"wrote {OUT.relative_to(ROOT)}  ({OUT.stat().st_size / 1024:.1f} KB)")
    print(f"  source: {source}")
    print(f"  events: {len(log.events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
