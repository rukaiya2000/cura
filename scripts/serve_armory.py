"""Serve the Armory locally so it can follow a running session.

    .venv/bin/python scripts/serve_armory.py

Then in another terminal:

    .venv/bin/python scripts/call.py

The page polls `events.json` from the same origin and follows the session as it happens.
Published as an artifact it cannot do that — a strict CSP blocks every request — so it
falls back to the timeline baked in at build time. Both paths are the same page; the
difference is only whether the fetch succeeds.

Serves `build/` and nothing else, on localhost only.
"""

import http.server
import socketserver
import sys
import webbrowser
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
PORT = 8765


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # The session file changes while the page is open; a cached copy would make the
        # dashboard look frozen mid-run.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, *args):
        pass


def main() -> int:
    page = BUILD / "armory.html"
    if not page.is_file():
        print(f"no {page.relative_to(ROOT)} — run scripts/build_armory.py first")
        return 1

    events = BUILD / "events.json"
    if not events.is_file():
        print(f"note: no {events.relative_to(ROOT)} yet — the page will show its built-in "
              f"timeline until scripts/call.py records one")

    url = f"http://127.0.0.1:{PORT}/armory.html"
    handler = partial(Handler, directory=str(BUILD))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), handler) as server:
        print(f"\nThe Armory  →  {url}")
        print("  run scripts/call.py in another terminal; the page follows along")
        print("  ctrl-c to stop\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
