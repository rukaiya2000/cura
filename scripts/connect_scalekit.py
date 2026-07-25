"""Create the connected accounts `--scalekit` needs.

    .venv/bin/python scripts/connect_scalekit.py priya@co sam@co

An identifier is **not** something you look up — it is a string you choose, and Scalekit
files that person's Linear token under it. This script turns each identifier you name into
an authorisation link; opening one and approving it creates the connected account.

The order matters and it is the easy thing to get wrong:

    identifier  →  authorisation link  →  **sign in as the intended Linear user**  →  approve

Whoever is signed in to Linear *in that browser* is whose permissions get attached. Open
both links in the same session and you will connect the same Linear account twice under
two different names — the tool sets will match, the denial beat will pass while proving
nothing, and nothing in the code can detect it. Use a private window for the second, or
sign out in between.

For the two-mouths beat the accounts must differ in Linear itself: one able to reassign
and reprioritise issues, one able only to read and comment. If both are workspace admins,
there is nothing to demonstrate.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.config import get, load_env

DIM, BOLD, GREEN, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;179m", "\033[31m", "\033[0m",
)

ENV_KEYS = ["SKILLFORGE_IDENTIFIER_FULL", "SKILLFORGE_IDENTIFIER_LIMITED",
            "SKILLFORGE_IDENTIFIER_PEER"]
ROLES = ["can reassign and reprioritise issues",
         "can read and comment, but NOT reassign",
         "same grants as the first, but a different person (proves reuse re-scopes)"]


def main() -> int:
    load_env()
    identifiers = sys.argv[1:]
    if not identifiers:
        print(__doc__)
        print(f"{DIM}Any strings work — an email, a username, a UUID. They only have to "
              f"be stable, because they are the key your tokens are filed under.{OFF}\n")
        return 1

    missing = [k for k in ("SCALEKIT_ENVIRONMENT_URL", "SCALEKIT_CLIENT_ID",
                           "SCALEKIT_CLIENT_SECRET") if not get(k)]
    if missing:
        print(f"{RED}missing credentials: {', '.join(missing)}{OFF}")
        return 2

    import os

    import scalekit

    connection = get("SKILLFORGE_LINEAR_CONNECTION_NAME", "linear")
    client = scalekit.client.ScalekitClient(
        env_url=os.environ["SCALEKIT_ENVIRONMENT_URL"],
        client_id=os.environ["SCALEKIT_CLIENT_ID"],
        client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
    )
    actions = client.actions

    print(f"\n{BOLD}Authorisation links{OFF} {DIM}· connection {connection!r}{OFF}")
    print(f"{DIM}Open each in a SEPARATE browser session, signed in as the intended "
          f"Linear user.{OFF}\n")

    produced = []
    for i, identifier in enumerate(identifiers):
        role = ROLES[i] if i < len(ROLES) else "another user"
        print(f"{BOLD}{identifier}{OFF} {DIM}— should be someone who {role}{OFF}")
        try:
            actions.get_or_create_connected_account(
                connection_name=connection, identifier=identifier)
            link = actions.get_authorization_link(
                connection_name=connection, identifier=identifier)
            url = getattr(link, "link", None) or getattr(link, "url", None) or link
            print(f"  {GREEN}{url}{OFF}\n")
            produced.append(identifier)
        except Exception as e:
            print(f"  {RED}{type(e).__name__}{OFF}: {e}")
            # Method names are the likeliest thing to have moved; show what exists
            # rather than leaving the caller to guess.
            surface = [n for n in dir(actions) if not n.startswith("_")]
            print(f"  {DIM}available on actions: {', '.join(surface)}{OFF}\n")

    if produced:
        print(f"{BOLD}Then add these to .env{OFF}")
        for key, identifier in zip(ENV_KEYS, produced):
            print(f"  {key}={identifier}")
        print(f"\n{DIM}Verify with:  .venv/bin/python scripts/probe_scalekit.py{OFF}")
        print(f"{DIM}It prints DIFFER or IDENTICAL — and IDENTICAL means the two "
              f"accounts are interchangeable, so the demo's second beat has nothing to "
              f"show.{OFF}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
