"""An in-memory stand-in for a doctor's connected services: HubSpot, Calendar, Gmail.

Shaped like `fake_scoped.py` and for the same reason — the whole forge loop (introspect,
generate, gate, temper, execute) runs without a Scalekit connector, a HubSpot account or a
live call. That matters more here than it did for Linear: the Scalekit dashboard has no
connectors configured at all, so this is currently the only way the clinical path can be
exercised end to end.

Three things are modelled deliberately rather than incidentally:

**Three services, not one.** The doctor holds Gmail as well, and never uses it in the
follow-up skill. That is what proves a manifest declares what a skill *reaches*, not what
its author *holds* — the difference between a ceiling and a false refusal.

**Calendar has `list_events`.** A skill cannot check for a clash it has no way to see, so
the conflict beat needs this primitive to exist before it can be composed.

**`gmail.send` is here but nothing composes it automatically.** Reaching a patient is the
one effect that never earns its way up the trust ladder; it waits for a human every time.

All patient data is synthetic.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from ..core.manifest import Effect
from ..core.sandbox import ScopedCallDenied

DOCTOR = "priya.rao@clinic.test"

CATALOGUE: dict[str, dict] = {
    "hubspot.get_contact": {
        "effect": Effect.READ,
        "description": "Fetch a patient's CRM contact record by id.",
        "input_schema": {"type": "object",
                         "properties": {"contact_id": {"type": "string"}},
                         "required": ["contact_id"]},
    },
    "hubspot.list_notes": {
        "effect": Effect.READ,
        "description": "List the notes already on a contact's record, newest first.",
        "input_schema": {"type": "object",
                         "properties": {"contact_id": {"type": "string"}},
                         "required": ["contact_id"]},
    },
    "hubspot.create_note": {
        "effect": Effect.WRITE,
        "description": "Add a note to a contact's record. Returns the created note.",
        "input_schema": {"type": "object", "properties": {
            "contact_id": {"type": "string"}, "body": {"type": "string"},
        }, "required": ["contact_id", "body"]},
    },
    "hubspot.create_task": {
        "effect": Effect.WRITE,
        "description": "Create a follow-up task against a contact, with a due date.",
        "input_schema": {"type": "object", "properties": {
            "contact_id": {"type": "string"}, "title": {"type": "string"},
            "due": {"type": "string", "description": "ISO date, e.g. 2026-08-29"},
        }, "required": ["contact_id", "title"]},
    },
    "calendar.list_events": {
        "effect": Effect.READ,
        "description": "List the clinician's events on a given date (ISO, e.g. 2026-09-05).",
        "input_schema": {"type": "object", "properties": {"date": {"type": "string"}},
                         "required": ["date"]},
    },
    "calendar.create_event": {
        "effect": Effect.WRITE,
        "description": "Create an appointment and invite the patient. Returns the event.",
        "input_schema": {"type": "object", "properties": {
            "title": {"type": "string"},
            "starts_at": {"type": "string", "description": "ISO datetime"},
            "minutes": {"type": "integer"},
            "attendee": {"type": "string", "description": "the patient's email"},
        }, "required": ["title", "starts_at"]},
    },
    "gmail.send": {
        "effect": Effect.WRITE,
        "description": "Send an email. Reaching a patient always waits for a human.",
        "input_schema": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"},
        }, "required": ["to", "subject", "body"]},
    },
}

#: The doctor holds all three services. A receptionist is included as the second identity
#: — same clinic, no ability to write to the record — so the scope ceiling is testable
#: here the way "two mouths" made it testable for Linear.
DEFAULT_GRANTS: dict[str, set[str]] = {
    DOCTOR: set(CATALOGUE),
    "reception@clinic.test": {"hubspot.get_contact", "calendar.list_events"},
}


def _seed() -> dict:
    return {
        "contacts": {
            "hs-contact-88412": {
                "id": "hs-contact-88412", "patient_id": "PT-10482",
                "name": "Amara Okafor", "email": "amara.okafor@example.test",
                "conditions": ["Type 2 diabetes (2019)", "Hypertension (2021)"],
                "medications": ["Metformin 1000 mg BD", "Ramipril 5 mg OD"],
                "notes": [{"id": "note-77104", "body": "12 Jun 2026 — HbA1c 58 mmol/mol. "
                                                       "Review in six weeks."}],
                "tasks": [],
            },
            "hs-contact-90233": {
                "id": "hs-contact-90233", "patient_id": "PT-10920",
                "name": "Nia Patel", "email": "n.patel@example.test",
                "conditions": ["Asthma"], "medications": ["Salbutamol PRN"],
                "notes": [], "tasks": [],
            },
        },
        # 5 Sep already has an appointment at 09:20, so a follow-up booked "six weeks
        # from today" lands on a clash. The conflict beat is in the data, not staged.
        "events": [
            {"id": "evt-3301", "title": "Anticoagulation review",
             "starts_at": "2026-09-05T09:20", "minutes": 20,
             "attendee": "t.lindqvist@example.test"},
        ],
        "sent": [],
    }


@dataclass
class _Tools:
    owner: FakeClinicActions

    def list_scoped_tools(self, *, identifier: str, filter: dict | None = None,
                          page_size: int = 100):
        names = tuple(filter.get("connection_names") or ()) if filter else ()
        granted = self.owner.grants.get(identifier, set())
        tools = [
            {"definition": {"name": name, "description": spec["description"],
                            "input_schema": spec["input_schema"]},
             "effect": spec["effect"].value}
            for name, spec in CATALOGUE.items()
            if name in granted and (not names or name.split(".")[0] in names)
        ]
        return tools[:page_size], None


@dataclass
class ToolResult:
    data: object


class FakeClinicActions:
    """Stands in for `scalekit_client.actions` across three connections."""

    def __init__(self, grants: dict[str, set[str]] | None = None) -> None:
        self.grants = copy.deepcopy(grants or DEFAULT_GRANTS)
        self.world = _seed()
        self.tools = _Tools(self)
        self.log: list[dict] = []

    def execute_tool(self, *, tool_name: str, identifier: str, tool_input: dict):
        if tool_name not in CATALOGUE:
            raise ScopedCallDenied(f"unknown primitive {tool_name!r}")
        if tool_name not in self.grants.get(identifier, set()):
            raise ScopedCallDenied(
                f"{identifier} is not granted {tool_name!r} — outside your maker's mark")
        self.log.append({"tool": tool_name, "identifier": identifier, "input": tool_input})
        return ToolResult(self._dispatch(tool_name, tool_input))

    def _dispatch(self, tool_name: str, a: dict):
        w = self.world
        action = tool_name.split(".", 1)[1]

        if action in ("get_contact", "list_notes", "create_note", "create_task"):
            contact = w["contacts"].get(a["contact_id"])
            if contact is None:
                raise ScopedCallDenied(f"no such contact {a['contact_id']!r}")

        if action == "get_contact":
            return copy.deepcopy(w["contacts"][a["contact_id"]])
        if action == "list_notes":
            return copy.deepcopy(w["contacts"][a["contact_id"]]["notes"])
        if action == "create_note":
            contact = w["contacts"][a["contact_id"]]
            note = {"id": f"note-{77200 + len(contact['notes'])}", "body": a["body"]}
            contact["notes"].insert(0, note)
            return copy.deepcopy(note)
        if action == "create_task":
            contact = w["contacts"][a["contact_id"]]
            task = {"id": f"task-{4400 + len(contact['tasks'])}",
                    "title": a["title"], "due": a.get("due")}
            contact["tasks"].append(task)
            return copy.deepcopy(task)

        if action == "list_events":
            return [copy.deepcopy(e) for e in w["events"]
                    if e["starts_at"].startswith(a["date"])]
        if action == "create_event":
            event = {"id": f"evt-{3400 + len(w['events'])}", "title": a["title"],
                     "starts_at": a["starts_at"], "minutes": a.get("minutes", 20),
                     "attendee": a.get("attendee")}
            w["events"].append(event)
            return copy.deepcopy(event)

        if action == "send":
            w["sent"].append(dict(a))
            return {"id": f"msg-{len(w['sent'])}", "to": a["to"]}

        raise ScopedCallDenied(f"unimplemented primitive {tool_name!r}")


class BoundClinicClient:
    """The doctor's scoped client, with their identity welded on."""

    def __init__(self, actions: FakeClinicActions, identifier: str = DOCTOR,
                 *, dry_run: bool = False) -> None:
        self._actions = actions
        self._identifier = identifier
        self._dry_run = dry_run
        self.simulated: list[dict] = []

    @property
    def identifier(self) -> str:
        return self._identifier

    def granted_tools(self) -> list[dict]:
        tools, _ = self._actions.tools.list_scoped_tools(identifier=self._identifier)
        return tools

    def granted_primitives(self) -> set[str]:
        return {t["definition"]["name"] for t in self.granted_tools()}

    def call(self, primitive: str, **tool_input):
        if "identifier" in tool_input:
            raise TypeError(
                "a skill may not pass 'identifier' — identity is bound by the host")
        if self._dry_run and CATALOGUE.get(primitive, {}).get("effect") is not Effect.READ:
            if primitive not in self._actions.grants.get(self._identifier, set()):
                raise ScopedCallDenied(
                    f"{self._identifier} is not granted {primitive!r} — "
                    "outside your maker's mark")
            self.simulated.append({"primitive": primitive, "input": tool_input})
            return {"dry_run": True, "primitive": primitive, "input": tool_input}
        return self._actions.execute_tool(
            tool_name=primitive, identifier=self._identifier, tool_input=tool_input).data
