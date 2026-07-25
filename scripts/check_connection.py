"""Is the connected account actually usable? Ask Scalekit rather than guessing.

    .venv/bin/python scripts/check_connection.py            # status for every connection
    .venv/bin/python scripts/check_connection.py --link     # and mint a fresh auth link

The distinction that matters and is easy to miss: an account can *exist* while carrying no
token. `get_authorization_link` creates the record immediately, so the dashboard and the
API both show a connected account for the identifier — but until someone completes the
OAuth consent its status is PENDING_AUTH, and every tool call fails with the unhelpful
"connected account is not active". This prints the status so that is a five-second check
rather than a debugging session.
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.config import get, load_env

DIM, B, GREEN, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;179m", "\033[31m", "\033[0m",
)

#: Only ACTIVE can execute a tool. Everything else is reported as-is rather than
#: collapsed to "not working", because the remedy differs per state.
USABLE = "ACTIVE"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", action="store_true", help="mint a fresh authorisation link")
    ap.add_argument("--connection", default=None, help="limit to one connection")
    args = ap.parse_args()
    load_env()

    missing = [k for k in ("SCALEKIT_ENVIRONMENT_URL", "SCALEKIT_CLIENT_ID",
                           "SCALEKIT_CLIENT_SECRET") if not get(k)]
    if missing:
        print(f"{RED}missing {', '.join(missing)} in .env{OFF}")
        return 1

    import scalekit

    client = scalekit.client.ScalekitClient(
        env_url=os.environ["SCALEKIT_ENVIRONMENT_URL"],
        client_id=os.environ["SCALEKIT_CLIENT_ID"],
        client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
    )

    connections = [args.connection] if args.connection else _connections(client)
    if not connections:
        print(f"{AMBER}no connections configured{OFF} — create one in the Scalekit "
              f"dashboard under AgentKit → Connections")
        return 1

    print(f"\n{B}Connected accounts{OFF}")
    any_pending = False
    for connection in connections:
        try:
            resp = client.actions.list_connected_accounts(connection_name=connection)
            accounts = getattr(resp, "connected_accounts", []) or []
        except Exception as e:  # noqa: BLE001
            print(f"  {connection}: {RED}{type(e).__name__}{OFF} {str(e)[:80]}")
            continue

        if not accounts:
            print(f"  {connection}: {DIM}nobody has authorised this yet{OFF}")
            continue

        for a in accounts:
            status = getattr(a, "status", "?")
            ok = status == USABLE
            colour = GREEN if ok else AMBER
            print(f"  {connection:10} {a.identifier:28} {colour}{status}{OFF}")
            if not ok:
                any_pending = True
                print(f"  {DIM}{'':10} no token issued — tool calls will fail with "
                      f"'connected account is not active'{OFF}")

    if args.link or any_pending:
        identifier = get("SKILLFORGE_IDENTIFIER_FULL") or ""
        if not identifier:
            print(f"\n{AMBER}set SKILLFORGE_IDENTIFIER_FULL in .env to mint a link{OFF}")
            return 1
        for connection in connections:
            try:
                r = client.actions.get_authorization_link(
                    connection_name=connection, identifier=identifier)
            except Exception as e:  # noqa: BLE001
                print(f"\n{connection}: {RED}{type(e).__name__}{OFF} {str(e)[:100]}")
                continue
            print(f"\n{B}Authorise {connection} as {identifier}{OFF}")
            print(f"  {r.link}")
            print(f"  {DIM}expires {r.expiry.strftime('%H:%M')}{OFF}")

        print(f"\n{DIM}Google will warn that this app is unverified. Choose Advanced, "
              f"then\ncontinue — the consent screen is where the flow usually stops "
              f"without\nsaying so, leaving the account at PENDING_AUTH.{OFF}")

    print()
    return 0


def _connections(client) -> list[str]:
    """Connection names from whatever the SDK exposes, falling back to the .env list."""
    names: list[str] = []
    try:
        resp = client.actions.list_connected_accounts()
        for a in getattr(resp, "connected_accounts", []) or []:
            n = getattr(a, "connector", None)
            if n and n not in names:
                names.append(n)
    except Exception:  # noqa: BLE001
        pass
    if not names:
        names = [n.strip() for n in (get("SKILLFORGE_CONNECTIONS") or "gmail").split(",")
                 if n.strip()]
    return names


if __name__ == "__main__":
    raise SystemExit(main())
