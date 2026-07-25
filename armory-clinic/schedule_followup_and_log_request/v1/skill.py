def run(scoped_client, contact_id, starts_at):
    contact = scoped_client.call("hubspot.get_contact", contact_id=contact_id)
    day = starts_at.split("T")[0]
    clashes = scoped_client.call("calendar.list_events", date=day)
    conflict = [c for c in clashes if c["starts_at"] == starts_at]
    event = scoped_client.call("calendar.create_event",
                               title="Follow-up consultation",
                               starts_at=starts_at, minutes=20,
                               attendee=contact["email"])
    scoped_client.call("hubspot.create_note", contact_id=contact_id,
                       body="HbA1c requested. Follow-up booked for " + starts_at + ".")
    scoped_client.call("hubspot.create_task", contact_id=contact_id,
                       title="Chase HbA1c result", due="2026-08-29")
    observed = scoped_client.call("hubspot.get_contact", contact_id=contact_id)
    return {
        "event_id": event["id"],
        "starts_at": event["starts_at"],
        "invited": event["attendee"],
        "conflicts": [c["title"] for c in conflict],
        "notes": len(observed["notes"]),
        "tasks": [t["title"] for t in observed["tasks"]],
    }
