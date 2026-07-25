"""The two things the bot does with the doctor's own connected accounts.

**Before the call — know who this is.** The patient's record is read from HubSpot at the
moment the bot is dispatched, so it arrives already informed rather than asking the doctor
to repeat what is already written down.

**When asked to schedule — look before writing.** The calendar is read first, and whatever
is found goes into the event's own description: *conflicts with X* if the slot is taken,
*Checked — nothing else booked* if it is free. The doctor sees the finding in the place
they will actually look, which is the appointment itself, rather than in a notification
they have already dismissed.

Primitive names here are the real ones, read off a live connected account rather than
guessed: `googlecalendar_list_events`, `googlecalendar_create_event`,
`hubspot_graphql_execute`. Two consequences worth knowing:

* the Calendar connector uses `start_datetime` and `event_duration_minutes`, not the
  start/end pair most calendar APIs take;
* **the HubSpot connector exposes no contact read at all** — 100 tools and not one of
  them fetches a contact by email. GraphQL is the only route in, which is why the lookup
  below is a query string rather than a call.

Everything goes through the scoped client, so every call here is bound to the acting
clinician's own grants. Nothing in this module can reach an account they do not hold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

CALENDAR_LIST = "list_events"
CALENDAR_CREATE = "create_event"
HUBSPOT_GRAPHQL = "graphql_execute"

#: How far either side of a proposed slot counts as a clash. A consultation butting
#: directly onto another is not a conflict; overlapping one is.
DEFAULT_MINUTES = 30


class ClinicError(Exception):
    """A read or write against the doctor's connected accounts failed."""


def resolve(client, action: str) -> str:
    """The granted primitive whose name ends in `action`, for this connection.

    Connection names carry generated suffixes — `googlecalendar-ru`, `hubspot-TsJM0b7V` —
    so the full primitive is `googlecalendar-ru.googlecalendar_list_events`, and a
    hardcoded name breaks the next time a connection is recreated. That has already
    happened twice. Matching on the action instead means this module keeps working
    across a rename, and **it can only ever resolve to something the speaker was actually
    granted**, because the candidate list is their own introspected tools.
    """
    granted = [t["definition"]["name"] for t in client.granted_tools()]
    matches = [n for n in granted if n.split(".", 1)[-1].endswith(action)]
    if not matches:
        raise ClinicError(
            f"no granted primitive ending in {action!r} — this account holds "
            f"{len(granted)} tools, none of them that one")
    # Shortest wins: `list_events` over `list_event_instances`, which also ends in
    # "events" for some connectors and is a different call entirely.
    return min(matches, key=len)


@dataclass
class Conflict:
    """What the calendar already holds at a proposed time."""

    clashes: list[dict] = field(default_factory=list)

    @property
    def clear(self) -> bool:
        return not self.clashes

    def message(self) -> str:
        """The line that goes into the calendar event's own description.

        Written for the doctor reading the appointment later, not for a log: it says what
        was checked and what was found, so an event that turned out to be double-booked
        carries its own explanation.
        """
        if self.clear:
            return "Checked against your calendar — nothing else booked. Done."
        listed = "; ".join(
            f"{c['summary']} at {_clock(c['start'])}" for c in self.clashes[:3])
        more = f" (and {len(self.clashes) - 3} more)" if len(self.clashes) > 3 else ""
        return (f"CONFLICT — this overlaps {len(self.clashes)} thing"
                f"{'s' if len(self.clashes) > 1 else ''} already in your calendar: "
                f"{listed}{more}. Booked anyway at your request; move it if that is wrong.")


def _clock(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M")
    except (ValueError, AttributeError):
        return value or "an unknown time"


def _rfc3339(moment: datetime) -> str:
    """A timestamp Google will accept — meaning one that carries an offset.

    `datetime.isoformat()` on a naive value produces `2026-07-28T09:20:00`, which is valid
    ISO 8601 and *not* valid RFC3339, and the connector answers FAILED_PRECONDITION with
    no indication of which field it disliked. A naive value is treated as UTC here, which
    is the only defensible reading when nothing carried a zone.
    """
    if moment.tzinfo is None:
        return moment.isoformat() + "Z"
    return moment.isoformat()


def _overlaps(start_a, end_a, start_b, end_b) -> bool:
    return start_a < end_b and start_b < end_a


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_bounds(event: dict):
    """Start and end of a Google Calendar event, whichever shape it arrives in."""
    start = event.get("start") or {}
    end = event.get("end") or {}
    if isinstance(start, dict):
        start = start.get("dateTime") or start.get("date")
    if isinstance(end, dict):
        end = end.get("dateTime") or end.get("date")
    return _parse(start), _parse(end)


def check_conflict(client, *, starts_at: str, minutes: int = DEFAULT_MINUTES) -> Conflict:
    """Read the calendar around a proposed slot and report what is already there.

    A read before a write, which is the same discipline the forged skills follow: the
    answer is what the calendar *said*, not what we assumed it would say.
    """
    start = _parse(starts_at)
    if start is None:
        raise ClinicError(f"{starts_at!r} is not a datetime I can read")
    end = start + timedelta(minutes=minutes)

    try:
        raw = client.call(
            resolve(client, CALENDAR_LIST),
            time_min=_rfc3339(start - timedelta(hours=12)),
            time_max=_rfc3339(end + timedelta(hours=12)),
            single_events=True, order_by="startTime", max_results=50)
    except Exception as e:  # noqa: BLE001
        raise ClinicError(f"could not read the calendar: {type(e).__name__}: {e}") from e

    clashes = []
    for event in _events(raw):
        other_start, other_end = _event_bounds(event)
        if other_start is None:
            continue
        if other_end is None:
            other_end = other_start + timedelta(minutes=DEFAULT_MINUTES)
        if _overlaps(start, end, other_start, other_end):
            clashes.append({
                "summary": event.get("summary") or "(untitled)",
                "start": other_start.isoformat(),
                "id": event.get("id", ""),
            })
    return Conflict(clashes=clashes)


def _events(raw) -> list[dict]:
    """The event list, whichever envelope the connector wraps it in."""
    if isinstance(raw, dict):
        for key in ("items", "events", "data", "result"):
            value = raw.get(key)
            if isinstance(value, list):
                return [e for e in value if isinstance(e, dict)]
            if isinstance(value, dict):
                return _events(value)
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    return []


def schedule(client, *, title: str, starts_at: str, minutes: int = DEFAULT_MINUTES,
             attendee: str = "", note: str = "") -> dict:
    """Check the calendar, then book — with the finding written into the event.

    The conflict message is not a side channel. It is the event's own description, so a
    doctor opening the appointment three weeks later sees why it looks the way it does
    without anyone having to remember to tell them.
    """
    conflict = check_conflict(client, starts_at=starts_at, minutes=minutes)

    description = conflict.message()
    if note:
        description = f"{note}\n\n{description}"
    description += "\n\nBooked by Cura at the clinician's request."

    payload = {
        "summary": title,
        "start_datetime": starts_at,
        "event_duration_minutes": minutes,
        "description": description,
    }
    if attendee:
        payload["attendees_emails"] = [attendee]

    try:
        created = client.call(resolve(client, CALENDAR_CREATE), **payload)
    except Exception as e:  # noqa: BLE001
        raise ClinicError(f"could not create the event: {type(e).__name__}: {e}") from e

    return {
        "created": created,
        "conflict": not conflict.clear,
        "clashes": conflict.clashes,
        "message": conflict.message(),
        "starts_at": starts_at,
        "attendee": attendee,
    }


# --- the patient's record ----------------------------------------------------

CONTACT_QUERY = """\
query PatientByEmail($email: String!) {
  CRM {
    contact_collection(filter: {email__eq: $email}, limit: 1) {
      items {
        hs_object_id
        email
        firstname
        lastname
        phone
        lifecyclestage
        notes_last_updated
      }
    }
  }
}
"""


def patient_record(client, *, email: str) -> dict | None:
    """The patient's HubSpot contact, or None if there isn't one.

    GraphQL because the connector offers no contact read — 100 tools and not one fetches
    a contact by email. Returning None rather than raising for "not found" matters: a
    patient with no record is the *new patient* case, which the product handles rather
    than treats as an error.
    """
    if not email:
        return None
    try:
        raw = client.call(resolve(client, HUBSPOT_GRAPHQL), query=CONTACT_QUERY,
                          variables={"email": email})
    except Exception as e:  # noqa: BLE001
        raise ClinicError(f"could not read the record: {type(e).__name__}: {e}") from e

    items = _dig(raw, "CRM", "contact_collection", "items")
    if not items:
        return None
    contact = items[0]
    name = " ".join(filter(None, [contact.get("firstname"), contact.get("lastname")]))
    return {
        "crm_id": str(contact.get("hs_object_id") or ""),
        "name": name or contact.get("email", ""),
        "email": contact.get("email", ""),
        "phone": contact.get("phone") or "",
        "stage": contact.get("lifecyclestage") or "",
        "last_note_at": contact.get("notes_last_updated") or "",
    }


def _dig(raw, *keys):
    """Walk a nested response, tolerating the envelope the connector adds."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    node = raw
    # The payload may be wrapped in `data`, `result` or nothing at all.
    for envelope in ("data", "result", "response"):
        if isinstance(node, dict) and envelope in node and keys[0] not in node:
            node = node[envelope]
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node
