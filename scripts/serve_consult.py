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
from skillforge.ui.serve import PatientStore, make_server

BUILD = ROOT / "build"
DIM, BOLD, GREEN, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;179m", "\033[31m", "\033[0m",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--public", default="",
                        help="public https URL MeetStream can reach (cloudflared)")
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

    print(f"\n{BOLD}Cura — consultation assistant{OFF}")
    print(f"  {origin}")
    if missing:
        print(f"\n  {AMBER}login not configured{OFF} — missing {', '.join(missing)}")
        print(f"  {DIM}The server will start and every page will show that, rather than "
              f"letting you in unauthenticated.{OFF}")
    else:
        print(f"  {GREEN}login via Scalekit{OFF} {DIM}· redirect {redirect_uri}{OFF}")
        print(f"\n  {DIM}This redirect URI must be registered in the Scalekit dashboard, "
              f"or the provider will reject the callback.{OFF}")
    if args.public:
        print(f"  {GREEN}bot dispatch enabled{OFF} {DIM}· webhooks → {args.public}{OFF}")
    else:
        print(f"  {AMBER}no --public{OFF} {DIM}· the Send Cura button will explain that "
              f"MeetStream cannot\n    reach this machine. Start a tunnel with "
              f"`cloudflared tunnel --url http://{args.host}:{args.port}`{OFF}")
    print(f"\n  {DIM}ctrl-c to stop{OFF}\n")

    #: Patients live next to the armory rather than in build/, which is generated and
    #: safe to delete. Losing a patient list to a rebuild would be unforgivable.
    server = make_server(root=BUILD, auth=Auth(redirect_uri=redirect_uri),
                         host=args.host, port=args.port,
                         patients=PatientStore(path=ROOT / "data" / "patients.json"),
                         public_url=args.public)
    if not args.no_browser:
        try:
            # `/`, not `/auth/login`. Going straight to the provider skips the page that
            # explains what this is, which is the one thing a first-time visitor needs.
            webbrowser.open(origin)
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
