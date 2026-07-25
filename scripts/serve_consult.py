"""Run the consultation UI behind Scalekit login.

    .venv/bin/python scripts/build_consult.py     # build the page
    .venv/bin/python scripts/serve_consult.py     # serve it, signed in

Opens a browser at the login route. Sign in as the clinician whose Scalekit identifier
holds the HubSpot and Calendar connected accounts — the authenticated subject *is* that
identifier, so there is no mapping between the two to get wrong.
"""

import argparse
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.adapters.auth import Auth
from skillforge.config import get, load_env
from skillforge.ui.serve import make_server

BUILD = ROOT / "build"
DIM, BOLD, GREEN, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;179m", "\033[31m", "\033[0m",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    load_env()

    page = BUILD / "consult.html"
    if not page.is_file():
        print(f"{RED}no {page.relative_to(ROOT)}{OFF} — run scripts/build_consult.py first")
        return 1

    origin = f"http://{args.host}:{args.port}"
    redirect_uri = f"{origin}/auth/callback"

    missing = [k for k in ("SCALEKIT_ENVIRONMENT_URL", "SCALEKIT_CLIENT_ID",
                           "SCALEKIT_CLIENT_SECRET") if not get(k)]

    print(f"\n{BOLD}Forge — consultation assistant{OFF}")
    print(f"  {origin}")
    if missing:
        print(f"\n  {AMBER}login not configured{OFF} — missing {', '.join(missing)}")
        print(f"  {DIM}The server will start and every page will show that, rather than "
              f"letting you in unauthenticated.{OFF}")
    else:
        print(f"  {GREEN}login via Scalekit{OFF} {DIM}· redirect {redirect_uri}{OFF}")
        print(f"\n  {DIM}This redirect URI must be registered in the Scalekit dashboard, "
              f"or the provider will reject the callback.{OFF}")
    print(f"\n  {DIM}ctrl-c to stop{OFF}\n")

    server = make_server(root=BUILD, auth=Auth(redirect_uri=redirect_uri),
                         host=args.host, port=args.port)
    if not args.no_browser:
        try:
            webbrowser.open(f"{origin}/auth/login")
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped\n")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
