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

#: The document these templates are wrapped in.
#:
#: They were served as bare fragments starting at `<title>`, which has three consequences
#: nobody would choose. Without a doctype the browser renders in **quirks mode**, where
#: the legacy width model applies — the only reason full-width padded buttons were not
#: overflowing. Without a viewport meta, every `max-width` media query in both files
#: **never fires on a phone**, so the responsive design was dead on the devices it was
#: written for. And without `lang`, a screen reader guesses at the pronunciation of a
#: clinical document.
#:
#: `box-sizing: border-box` is set here rather than per-template so the two pages cannot
#: disagree about it — one of them already did.
DOC_OPEN = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>*, *::before, *::after { box-sizing: border-box; }</style>
"""
DOC_MID = "</head>\n<body>\n"
DOC_CLOSE = "\n</body>\n</html>\n"


def wrap(fragment: str) -> str:
    """Put a fragment in a real document, with <title> and <style> hoisted into <head>."""
    head, _, rest = fragment.partition("\n")          # the <title> line
    return DOC_OPEN + head + "\n" + DOC_MID + rest + DOC_CLOSE


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
    OUT.write_text(wrap(template.replace(TOKEN, payload)))

    SIGNIN_OUT.write_text(wrap(SIGNIN_TEMPLATE.read_text()))

    data = consult.practice()
    print(f"wrote {OUT.relative_to(ROOT)}  ({OUT.stat().st_size / 1024:.1f} KB)")
    print(f"wrote {SIGNIN_OUT.relative_to(ROOT)}  "
          f"({SIGNIN_OUT.stat().st_size / 1024:.1f} KB)")
    print(f"  {len(data['patients'])} patients · {len(data['schedule'])} appointments · "
          f"{len(data['consultation'])} consultation events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
