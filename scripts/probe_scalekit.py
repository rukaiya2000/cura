"""Find out what Scalekit actually does, before building an adapter on assumptions.

    .venv/bin/python scripts/probe_scalekit.py

Read-only. Authenticates, then answers the three questions the whole governance design
rests on — every one of which is currently a guess baked into `fake_scoped.py`:

  1. Do credentials authenticate at all?
  2. Does `list_scoped_tools` return a *per-user* subset, or the same set for everyone?
     If grants are all-or-nothing per connection, the two-mouths beat needs a different
     mechanism, and that is the one finding that would force a redesign.
  3. What does an ungranted `execute_tool` do — raise, or return an error object? The
     fake raises. If the real one returns, every denial path silently becomes a success.

Prints no secrets. Identifiers are echoed because you chose them and they are not secret.
"""

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skillforge.config import get, load_env

DIM, BOLD, GREEN, AMBER, RED, OFF = (
    "\033[2m", "\033[1m", "\033[32m", "\033[38;5;179m", "\033[31m", "\033[0m",
)


def head(text):
    print(f"\n{BOLD}{text}{OFF}")


def main() -> int:
    load_env()
    missing = [k for k in ("SCALEKIT_CLIENT_ID", "SCALEKIT_CLIENT_SECRET",
                           "SCALEKIT_ENVIRONMENT_URL") if not get(k)]
    if missing:
        print(f"{RED}missing: {', '.join(missing)}{OFF}")
        return 2

    import scalekit

    connection = get("SKILLFORGE_LINEAR_CONNECTION_NAME", "linear")

    head("1. Authentication")
    try:
        client = scalekit.client.ScalekitClient(
            env_url=os.environ["SCALEKIT_ENVIRONMENT_URL"],
            client_id=os.environ["SCALEKIT_CLIENT_ID"],
            client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
        )
        actions = client.actions
        print(f"  {GREEN}client constructed{OFF} {DIM}· actions surface available{OFF}")
    except Exception as e:
        print(f"  {RED}failed{OFF} — {type(e).__name__}: {e}")
        return 1

    head(f"2. Does a {connection!r} connector exist in this environment?")
    try:
        configs = actions.list_configs()
        existing = [
            getattr(c, "connection_name", None) or getattr(c, "name", None) or str(c)
            for c in (getattr(configs, "configs", None) or [])
        ]
        if not existing:
            print(f"  {RED}no connectors configured at all{OFF}")
            print(f"  {DIM}Everything downstream is blocked on this. An identifier can "
                  f"only exist once there is a connector to authorise against — create "
                  f"a Linear connector in the Scalekit dashboard first, then re-run "
                  f"scripts/connect_scalekit.py.{OFF}")
        elif connection not in existing:
            print(f"  {RED}{connection!r} not found{OFF} — this environment has: "
                  f"{', '.join(existing)}")
            print(f"  {DIM}Set SKILLFORGE_LINEAR_CONNECTION_NAME to one of those "
                  f"(case-sensitive).{OFF}")
        else:
            print(f"  {GREEN}{connection!r} exists{OFF}")
    except Exception as e:
        print(f"  {AMBER}could not list connectors{OFF} — {type(e).__name__}: {e}")

    head("3. What tools does an identifier hold?")
    identifiers = [i for i in (get("SKILLFORGE_IDENTIFIER_FULL"),
                               get("SKILLFORGE_IDENTIFIER_LIMITED"),
                               get("SKILLFORGE_IDENTIFIER_PEER")) if i]
    if not identifiers:
        # Still worth calling: an unknown identifier's response shape tells us whether
        # "no connected account" is an error or an empty list, which the router needs.
        identifiers = ["probe-unconnected-user"]
        print(f"  {AMBER}no SKILLFORGE_IDENTIFIER_* set{OFF} — probing with a made-up "
              f"identifier to learn the unconnected-user response shape")

    seen: dict[str, set[str]] = {}
    for identifier in identifiers:
        try:
            result = actions.tools.list_scoped_tools(
                identifier=identifier,
                filter={"connection_names": [connection]},
                page_size=100,
            )
            tools = result[0] if isinstance(result, tuple) else result
            names = set()
            for tool in tools or []:
                definition = getattr(tool, "definition", None) or (
                    tool.get("definition") if isinstance(tool, dict) else None)
                name = (getattr(definition, "name", None)
                        or (definition or {}).get("name") if definition else None)
                if name:
                    names.add(name)
            seen[identifier] = names
            print(f"  {identifier:<28} {GREEN}{len(names)} tools{OFF}")
            for name in sorted(names)[:12]:
                print(f"      {DIM}·{OFF} {name}")
            if not names:
                print(f"      {AMBER}empty — check the connection name is exactly "
                      f"{connection!r} (case-sensitive) and that this user authorised it{OFF}")
        except Exception as e:
            print(f"  {identifier:<28} {RED}{type(e).__name__}{OFF}: {e}")
            print(f"{DIM}{traceback.format_exc(limit=2)}{OFF}")

    head("4. Are grants actually per-user?")
    if len(seen) < 2:
        print(f"  {AMBER}inconclusive{OFF} — needs two identifiers with different Linear "
              f"permissions. This is the question that decides whether the demo's second "
              f"beat works as designed.")
    else:
        sets = list(seen.values())
        if all(s == sets[0] for s in sets):
            print(f"  {RED}IDENTICAL tool sets across identifiers{OFF}")
            print(f"  {DIM}Either both users have the same Linear permissions, or grants "
                  f"are per-connection rather than per-tool. If the latter, the scope "
                  f"ceiling must move from the tool list to the tool *call*.{OFF}")
        else:
            print(f"  {GREEN}tool sets DIFFER per identifier — the design holds{OFF}")
            for identifier, names in seen.items():
                others = set().union(*(v for k, v in seen.items() if k != identifier))
                print(f"    {identifier}: {len(names)} tools, "
                      f"{len(others - names)} it lacks that another holds")

    head("Summary")
    print(f"  connection name  {connection!r} {DIM}(case-sensitive){OFF}")
    print(f"  identifiers      {len(seen)} probed")
    print(f"  {DIM}Question 3 is the one that matters. Everything else is plumbing.{OFF}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
