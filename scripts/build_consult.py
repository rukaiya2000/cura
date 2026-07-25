"""Build the clinician's UI by inlining the practice payload into the template.

    .venv/bin/python scripts/build_consult.py

`skillforge/ui/consult.py` stays the single source of truth for the shape, so the screen
and the agent core cannot drift apart on what a consultation event looks like.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.ui import consult

TEMPLATE = ROOT / "skillforge" / "ui" / "consult.template.html"
OUT = ROOT / "build" / "consult.html"
TOKEN = "__CONSULTATION_JSON__"

#: The sign-in screen needs no data inlined, but it is copied here rather than served
#: from the source tree so `build/` stays the one directory the server exposes. A web
#: root that reaches back into the repo is how a .py file ends up downloadable.
SIGNIN_TEMPLATE = ROOT / "skillforge" / "ui" / "signin.template.html"
SIGNIN_OUT = ROOT / "build" / "signin.html"


def main() -> int:
    template = TEMPLATE.read_text()
    if TOKEN not in template:
        print(f"error: {TOKEN} not found in {TEMPLATE}", file=sys.stderr)
        return 1

    # `<` escaped so the JSON can never close the surrounding script element.
    payload = consult.to_json().replace("<", "\\u003c")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(template.replace(TOKEN, payload))

    SIGNIN_OUT.write_text(SIGNIN_TEMPLATE.read_text())

    data = consult.practice()
    print(f"wrote {OUT.relative_to(ROOT)}  ({OUT.stat().st_size / 1024:.1f} KB)")
    print(f"wrote {SIGNIN_OUT.relative_to(ROOT)}  "
          f"({SIGNIN_OUT.stat().st_size / 1024:.1f} KB)")
    print(f"  {len(data['patients'])} patients · {len(data['schedule'])} appointments · "
          f"{len(data['consultation'])} consultation events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
