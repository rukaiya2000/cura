"""An in-memory stand-in for Scalekit + Linear, shaped like the real thing.

Mirrors the Scalekit `actions` surface we actually use:

    actions.tools.list_scoped_tools(identifier=…, filter={"connection_names": ["linear"]})
    actions.execute_tool(tool_name=…, identifier=…, tool_input={…})

Having this lets the whole forge loop — introspect, generate, check, temper, execute —
be exercised without a live call, a Linear workspace, or a token. Crucially the *grants*
are per-identifier, so the "one sentence, two mouths" property is testable here first:
the set of primitives Forge is even shown depends on who is speaking.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from ..core.manifest import Effect
from ..core.sandbox import ScopedCallDenied

#: The primitive catalogue, with the effect class of each. Effects matter because
#: quarantine and dry-run decisions are made on them, not on the primitive's name.
CATALOGUE: dict[str, dict] = {
    "linear.get_issue": {
        "effect": Effect.READ,
        "description": "Fetch a single issue by id.",
        "input_schema": {"type": "object", "properties": {"issue_id": {"type": "string"}},
                         "required": ["issue_id"]},
    },
    "linear.list_issues": {
        "effect": Effect.READ,
        "description": "List issues, optionally filtered by assignee and minimum priority.",
        "input_schema": {"type": "object", "properties": {
            "assignee": {"type": "string"},
            "priority_gte": {"type": "string", "enum": ["Low", "Medium", "High", "Urgent"]},
        }},
    },
    "linear.get_active_cycle": {
        "effect": Effect.READ,
        "description": "Get the currently active cycle (sprint) for a team.",
        "input_schema": {"type": "object", "properties": {"team_id": {"type": "string"}},
                         "required": ["team_id"]},
    },
    "linear.update_issue": {
        "effect": Effect.WRITE,
        "description": "Update an issue's assignee, priority or state.",
        "input_schema": {"type": "object", "properties": {
            "issue_id": {"type": "string"},
            "assignee": {"type": "string"},
            "priority": {"type": "string", "enum": ["Low", "Medium", "High", "Urgent"]},
            "state": {"type": "string"},
        }, "required": ["issue_id"]},
    },
    "linear.create_comment": {
        "effect": Effect.WRITE,
        "description": "Add a comment to an issue.",
        "input_schema": {"type": "object", "properties": {
            "issue_id": {"type": "string"}, "body": {"type": "string"},
        }, "required": ["issue_id", "body"]},
    },
    "linear.link_issue": {
        "effect": Effect.WRITE,
        "description": "Link an issue to a cycle or parent issue.",
        "input_schema": {"type": "object", "properties": {
            "issue_id": {"type": "string"}, "target_id": {"type": "string"},
        }, "required": ["issue_id", "target_id"]},
    },
    "linear.delete_project": {
        "effect": Effect.DESTRUCTIVE,
        "description": "Delete a project and everything in it.",
        "input_schema": {"type": "object", "properties": {"project_id": {"type": "string"}},
                         "required": ["project_id"]},
    },
}

#: Per-user grants. Priya is a PM with issue write; Dana is an eng manager with the same
#: reach; Sam is a contractor who can read and comment but not reassign or reprioritise.
#: Nobody has delete_project.
#:
#: Three people rather than two on purpose. The demo needs a *denied* speaker (Sam) and a
#: separate *reusing* speaker (Dana): a skill Sam is not granted is a skill Sam also cannot
#: reuse, so reuse has to be proven by someone who holds the grants but did not forge it.
DEFAULT_GRANTS: dict[str, set[str]] = {
    "priya@co": {
        "linear.get_issue", "linear.list_issues", "linear.get_active_cycle",
        "linear.update_issue", "linear.create_comment", "linear.link_issue",
    },
    "dana@co": {
        "linear.get_issue", "linear.list_issues", "linear.get_active_cycle",
        "linear.update_issue", "linear.create_comment", "linear.link_issue",
    },
    "sam@co": {
        "linear.get_issue", "linear.list_issues", "linear.get_active_cycle",
        "linear.create_comment",
    },
}


def _seed_workspace() -> dict:
    return {
        "teams": {"TEAM-1": {"id": "TEAM-1", "name": "Platform"}},
        "cycles": {"CYC-14": {"id": "CYC-14", "team_id": "TEAM-1", "name": "Cycle 14",
                              "active": True}},
        "issues": {
            "LIN-402": {"id": "LIN-402", "title": "Login fails on SSO redirect",
                        "assignee": "priya@co", "priority": "Medium", "state": "Todo",
                        "team_id": "TEAM-1", "links": [], "comments": []},
            "LIN-377": {"id": "LIN-377", "title": "Rotate session keys",
                        "assignee": "priya@co", "priority": "High", "state": "In Progress",
                        "team_id": "TEAM-1", "links": [], "comments": []},
            "LIN-388": {"id": "LIN-388", "title": "Backfill audit events",
                        "assignee": "priya@co", "priority": "Urgent", "state": "Todo",
                        "team_id": "TEAM-1", "links": [], "comments": []},
            "LIN-391": {"id": "LIN-391", "title": "Tidy up log formatting",
                        "assignee": "priya@co", "priority": "Low", "state": "Todo",
                        "team_id": "TEAM-1", "links": [], "comments": []},
        },
        "projects": {"PRJ-1": {"id": "PRJ-1", "name": "Auth revamp", "owner": "priya@co"}},
    }


_PRIORITY_ORDER = ["Low", "Medium", "High", "Urgent"]


@dataclass
class ToolResult:
    data: object


@dataclass
class _Tools:
    owner: FakeScalekitActions

    def list_scoped_tools(self, *, identifier: str, filter: dict | None = None,
                          page_size: int = 100) -> tuple[list[dict], object]:
        """Return only the tools this identifier has actually been granted.

        This is the introspection step *and* the scope ceiling in one call: the forge is
        never shown a capability the speaker lacks, so it cannot compose one.
        """
        # No filter means no filtering. This used to default to `("linear",)`, which
        # quietly hid every other connection from an unfiltered introspection — so a
        # speaker holding Calendar and HubSpot appeared to hold nothing at all.
        names = tuple(filter.get("connection_names") or ()) if filter else ()
        granted = self.owner.grants.get(identifier, set())
        tools = [
            {"definition": {
                "name": name,
                "description": spec["description"],
                "input_schema": spec["input_schema"],
            }, "effect": spec["effect"].value}
            for name, spec in CATALOGUE.items()
            if name in granted and (not names or name.split(".")[0] in names)
        ]
        return tools[:page_size], None


class FakeScalekitActions:
    """Stands in for `scalekit_client.actions`."""

    def __init__(self, grants: dict[str, set[str]] | None = None) -> None:
        self.grants = copy.deepcopy(grants or DEFAULT_GRANTS)
        self.workspace = _seed_workspace()
        self.tools = _Tools(self)
        self.log: list[dict] = []

    # --- execution ---------------------------------------------------------

    def execute_tool(self, *, tool_name: str, identifier: str, tool_input: dict) -> ToolResult:
        if tool_name not in CATALOGUE:
            raise ScopedCallDenied(f"unknown primitive {tool_name!r}")
        if tool_name not in self.grants.get(identifier, set()):
            # This is the deny beat, and it is the same code path for every primitive:
            # identity decides, not a list of scary verbs.
            raise ScopedCallDenied(
                f"{identifier} is not granted {tool_name!r} — outside your maker's mark"
            )
        self.log.append({"tool": tool_name, "identifier": identifier, "input": tool_input})
        return ToolResult(self._dispatch(tool_name, identifier, tool_input))

    def _dispatch(self, tool_name: str, identifier: str, args: dict):
        ws = self.workspace
        action = tool_name.split(".", 1)[1]

        if action == "get_issue":
            issue = ws["issues"].get(args["issue_id"])
            if issue is None:
                raise ScopedCallDenied(f"no such issue {args['issue_id']!r}")
            return copy.deepcopy(issue)

        if action == "list_issues":
            floor = _PRIORITY_ORDER.index(args["priority_gte"]) if args.get("priority_gte") else 0
            return [
                copy.deepcopy(i) for i in ws["issues"].values()
                if (not args.get("assignee") or i["assignee"] == args["assignee"])
                and _PRIORITY_ORDER.index(i["priority"]) >= floor
            ]

        if action == "get_active_cycle":
            for cycle in ws["cycles"].values():
                if cycle["team_id"] == args["team_id"] and cycle["active"]:
                    return copy.deepcopy(cycle)
            raise ScopedCallDenied(f"no active cycle for team {args['team_id']!r}")

        if action == "update_issue":
            issue = ws["issues"].get(args["issue_id"])
            if issue is None:
                raise ScopedCallDenied(f"no such issue {args['issue_id']!r}")
            for key in ("assignee", "priority", "state"):
                if key in args:
                    issue[key] = args[key]
            return copy.deepcopy(issue)

        if action == "create_comment":
            issue = ws["issues"].get(args["issue_id"])
            if issue is None:
                raise ScopedCallDenied(f"no such issue {args['issue_id']!r}")
            issue["comments"].append({"author": identifier, "body": args["body"]})
            return {"issue_id": issue["id"], "comment_count": len(issue["comments"])}

        if action == "link_issue":
            issue = ws["issues"].get(args["issue_id"])
            if issue is None:
                raise ScopedCallDenied(f"no such issue {args['issue_id']!r}")
            if args["target_id"] not in issue["links"]:
                issue["links"].append(args["target_id"])
            return copy.deepcopy(issue)

        if action == "delete_project":
            ws["projects"].pop(args["project_id"], None)
            return {"deleted": args["project_id"]}

        raise ScopedCallDenied(f"unimplemented primitive {tool_name!r}")


class BoundScopedClient:
    """Host-side client with the acting user's identity welded on.

    Generated code receives a proxy to this and can name a primitive but never an
    identity. Constructed once per action, from the resolved speaker.
    """

    def __init__(self, actions: FakeScalekitActions, identifier: str,
                 *, dry_run: bool = False) -> None:
        self._actions = actions
        self._identifier = identifier
        self._dry_run = dry_run
        self.simulated: list[dict] = []

    @property
    def identifier(self) -> str:
        return self._identifier

    def granted_tools(self) -> list[dict]:
        """The introspection step: the tool definitions this speaker actually holds.

        This is what the forge composes from — so it is also the scope ceiling. A
        capability absent from this list cannot be written, only refused.
        """
        tools, _ = self._actions.tools.list_scoped_tools(identifier=self._identifier)
        return tools

    def granted_primitives(self) -> set[str]:
        return {t["definition"]["name"] for t in self.granted_tools()}

    def call(self, primitive: str, **tool_input):
        if "identifier" in tool_input:
            # Mirrors the real adapter: identity is not the caller's to choose, and that
            # is enforced here as well as by the static gate.
            raise TypeError(
                "a skill may not pass 'identifier' — identity is bound by the host"
            )
        if self._dry_run and CATALOGUE.get(primitive, {}).get("effect") is not Effect.READ:
            # Quarantined skills still exercise their read path and their control flow,
            # so a dry-run temper is a real test — it just cannot change anything.
            if primitive not in self._actions.grants.get(self._identifier, set()):
                raise ScopedCallDenied(
                    f"{self._identifier} is not granted {primitive!r} — outside your maker's mark"
                )
            self.simulated.append({"primitive": primitive, "input": tool_input})
            return {"dry_run": True, "primitive": primitive, "input": tool_input}
        return self._actions.execute_tool(
            tool_name=primitive, identifier=self._identifier, tool_input=tool_input
        ).data
