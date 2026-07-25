"""Reading the calendar before writing to it, and reading the patient's record.

The property under test throughout: **what gets reported is what the service actually
said.** A conflict check that assumes rather than reads, or a booking whose description
does not match what was found, would be worse than no check — it would be a check the
doctor trusts.
"""

import pytest

from skillforge.core.clinic import (
    Conflict,
    ClinicError,
    check_conflict,
    patient_record,
    schedule,
)


class FakeClient:
    """A scoped client that records calls and returns canned results."""

    def __init__(self, results=None, fail=None, prefix="conn-x9."):
        self.results = results or {}
        self.fail = fail
        self.prefix = prefix
        self.calls = []

    def granted_tools(self):
        """Named the way a real connector names them: a generated connection suffix,
        and the provider repeated inside the action."""
        return [{"definition": {"name": f"{self.prefix}googlecalendar_{a}"},
                 "effect": "read"}
                for a in ("list_events", "list_event_instances", "create_event")] + [
                {"definition": {"name": f"{self.prefix}hubspot_graphql_execute"},
                 "effect": "read"}]

    def call(self, primitive, **kwargs):
        action = primitive.split("_", 1)[-1] if "_" in primitive else primitive
        for known in ("list_events", "create_event", "graphql_execute"):
            if primitive.endswith(known):
                action = known
                break
        self.calls.append((action, kwargs))
        if self.fail and action == self.fail:
            raise RuntimeError("upstream said no")
        return self.results.get(action, {})


def event(summary, start, end):
    return {"id": f"e-{summary}", "summary": summary,
            "start": {"dateTime": start}, "end": {"dateTime": end}}


def calendar(*events):
    return {"list_events": {"items": list(events)},
            "create_event": {"id": "evt-new", "htmlLink": "https://cal/evt-new"}}


# --- the conflict check ------------------------------------------------------


def test_an_empty_calendar_is_clear():
    c = check_conflict(FakeClient(calendar()), starts_at="2026-09-05T09:20:00")
    assert c.clear
    assert "nothing else booked" in c.message()
    assert "Done" in c.message()


def test_an_overlapping_event_is_a_conflict():
    client = FakeClient(calendar(
        event("Anticoagulation review", "2026-09-05T09:20:00", "2026-09-05T09:40:00")))
    c = check_conflict(client, starts_at="2026-09-05T09:20:00", minutes=30)

    assert not c.clear
    assert c.clashes[0]["summary"] == "Anticoagulation review"
    assert "CONFLICT" in c.message()
    assert "Anticoagulation review" in c.message()


def test_an_adjacent_event_is_not_a_conflict():
    """A consultation butting directly onto another is a full clinic, not a clash. Calling
    it one would make the warning fire constantly and stop being read."""
    client = FakeClient(calendar(
        event("Earlier", "2026-09-05T08:50:00", "2026-09-05T09:20:00")))
    assert check_conflict(client, starts_at="2026-09-05T09:20:00", minutes=30).clear


def test_a_partial_overlap_counts():
    client = FakeClient(calendar(
        event("Runs over", "2026-09-05T09:10:00", "2026-09-05T09:25:00")))
    assert not check_conflict(client, starts_at="2026-09-05T09:20:00").clear


def test_the_window_read_is_wider_than_the_slot():
    """A long event starting hours earlier still overlaps. Reading only the slot itself
    would miss exactly the appointments most worth catching."""
    client = FakeClient(calendar())
    check_conflict(client, starts_at="2026-09-05T09:20:00")

    _, kwargs = client.calls[0]
    assert kwargs["time_min"] < "2026-09-05T09:20:00"
    assert kwargs["time_max"] > "2026-09-05T09:50:00"


def test_several_clashes_are_counted_not_just_the_first():
    client = FakeClient(calendar(
        event("One", "2026-09-05T09:15:00", "2026-09-05T09:35:00"),
        event("Two", "2026-09-05T09:25:00", "2026-09-05T09:45:00"),
        event("Three", "2026-09-05T09:30:00", "2026-09-05T09:50:00"),
        event("Four", "2026-09-05T09:31:00", "2026-09-05T09:51:00")))
    c = check_conflict(client, starts_at="2026-09-05T09:20:00")

    assert len(c.clashes) == 4
    assert "4 things" in c.message()
    assert "and 1 more" in c.message()


def test_an_unreadable_time_is_refused_before_any_call():
    client = FakeClient(calendar())
    with pytest.raises(ClinicError, match="not a datetime"):
        check_conflict(client, starts_at="next tuesday-ish")
    assert client.calls == [], "the calendar was read with a nonsense time"


def test_a_calendar_that_will_not_read_is_an_error_not_a_clear_slot():
    """Silently treating an unreadable calendar as free is the one failure that books
    over a real appointment while reporting success."""
    client = FakeClient(calendar(), fail="list_events")
    with pytest.raises(ClinicError, match="could not read the calendar"):
        check_conflict(client, starts_at="2026-09-05T09:20:00")


@pytest.mark.parametrize("envelope", [
    {"items": []}, {"events": []}, {"data": {"items": []}}, [], {},
])
def test_response_envelopes_are_tolerated(envelope):
    client = FakeClient({"list_events": envelope})
    assert check_conflict(client, starts_at="2026-09-05T09:20:00").clear


# --- booking, with the finding written in -------------------------------------


def test_a_clear_slot_books_and_says_done():
    client = FakeClient(calendar())
    result = schedule(client, title="Follow-up", starts_at="2026-09-05T09:20:00",
                      attendee="amara@example.test")

    assert result["conflict"] is False
    primitive, kwargs = client.calls[-1]
    assert primitive == "create_event"
    assert "Done" in kwargs["description"]
    assert kwargs["attendees_emails"] == ["amara@example.test"]


def test_a_clash_is_written_into_the_event_itself():
    """Not a toast, not a log line. A doctor opening the appointment three weeks later
    sees why it looks the way it does, without anyone having to have told them."""
    client = FakeClient(calendar(
        event("Anticoagulation review", "2026-09-05T09:20:00", "2026-09-05T09:40:00")))
    result = schedule(client, title="Follow-up", starts_at="2026-09-05T09:20:00")

    assert result["conflict"] is True
    _, kwargs = client.calls[-1]
    assert "CONFLICT" in kwargs["description"]
    assert "Anticoagulation review" in kwargs["description"]


def test_it_reads_before_it_writes():
    client = FakeClient(calendar())
    schedule(client, title="Follow-up", starts_at="2026-09-05T09:20:00")

    assert [c[0] for c in client.calls] == ["list_events", "create_event"]


def test_the_event_uses_the_connectors_real_field_names():
    """`start_datetime` and `event_duration_minutes` — not the start/end pair most
    calendar APIs take. Read off a live connected account, not assumed."""
    client = FakeClient(calendar())
    schedule(client, title="Follow-up", starts_at="2026-09-05T09:20:00", minutes=20)

    _, kwargs = client.calls[-1]
    assert kwargs["start_datetime"] == "2026-09-05T09:20:00"
    assert kwargs["event_duration_minutes"] == 20
    assert "end" not in kwargs and "end_datetime" not in kwargs


def test_a_note_from_the_consultation_leads_the_description():
    client = FakeClient(calendar())
    schedule(client, title="Follow-up", starts_at="2026-09-05T09:20:00",
             note="Review HbA1c and postural symptoms.")

    _, kwargs = client.calls[-1]
    assert kwargs["description"].startswith("Review HbA1c")
    assert "Booked by Cura" in kwargs["description"]


def test_a_failed_write_does_not_report_success():
    client = FakeClient(calendar(), fail="create_event")
    with pytest.raises(ClinicError, match="could not create the event"):
        schedule(client, title="Follow-up", starts_at="2026-09-05T09:20:00")


# --- the patient's record -----------------------------------------------------


def contact_response(**over):
    item = {"hs_object_id": 88412, "email": "amara@example.test",
            "firstname": "Amara", "lastname": "Okafor", "phone": "0113 496 0112",
            "lifecyclestage": "customer", "notes_last_updated": "2026-06-12"}
    item.update(over)
    return {"graphql_execute": {
        "data": {"CRM": {"contact_collection": {"items": [item]}}}}}


def test_a_patient_record_is_read_from_hubspot():
    client = FakeClient(contact_response())
    record = patient_record(client, email="amara@example.test")

    assert record["crm_id"] == "88412"
    assert record["name"] == "Amara Okafor"
    assert record["phone"] == "0113 496 0112"


def test_a_patient_with_no_record_is_none_not_an_error():
    """No record is the *new patient* case, which the product handles rather than
    treating as a failure."""
    client = FakeClient({"graphql_execute": {
        "data": {"CRM": {"contact_collection": {"items": []}}}}})
    assert patient_record(client, email="nobody@example.test") is None


def test_no_email_means_no_lookup():
    client = FakeClient(contact_response())
    assert patient_record(client, email="") is None
    assert client.calls == [], "HubSpot was queried with an empty email"


def test_the_query_is_parameterised_not_interpolated():
    """An email spliced into a query string is an injection waiting to happen, and
    patient emails are attacker-influenced in exactly the way that matters."""
    client = FakeClient(contact_response())
    patient_record(client, email='a" OR 1==1 --@x.test')

    _, kwargs = client.calls[0]
    assert kwargs["variables"]["email"] == 'a" OR 1==1 --@x.test'
    assert "OR 1==1" not in kwargs["query"]


def test_a_json_string_response_is_parsed():
    import json as _json
    client = FakeClient({"graphql_execute": _json.dumps(
        {"data": {"CRM": {"contact_collection": {"items": [
            {"hs_object_id": 1, "email": "a@b.test", "firstname": "A"}]}}}})})
    assert patient_record(client, email="a@b.test")["crm_id"] == "1"


def test_a_record_that_will_not_read_is_an_error():
    client = FakeClient(contact_response(), fail="graphql_execute")
    with pytest.raises(ClinicError, match="could not read the record"):
        patient_record(client, email="amara@example.test")


# --- the message a doctor actually reads --------------------------------------


def test_the_clear_message_is_plain():
    assert Conflict().message() == \
        "Checked against your calendar — nothing else booked. Done."


def test_the_conflict_message_says_it_booked_anyway():
    """It does not silently refuse. The doctor asked; the bot books and flags it, because
    a refusal here means the appointment simply does not exist and nobody notices."""
    c = Conflict(clashes=[{"summary": "Clinic", "start": "2026-09-05T09:20:00", "id": "x"}])
    assert "Booked anyway" in c.message()
    assert "move it if that is wrong" in c.message()


# --- surviving a renamed connector -------------------------------------------


def test_primitives_are_resolved_not_hardcoded():
    """Connection names carry generated suffixes, so the full primitive is
    `googlecalendar-ru.googlecalendar_list_events`. Hardcoding that breaks the next time a
    connection is recreated — which has already happened twice."""
    from skillforge.core.clinic import resolve

    client = FakeClient(calendar(), prefix="googlecalendar-ru.")
    assert resolve(client, "list_events") == \
        "googlecalendar-ru.googlecalendar_list_events"


def test_a_renamed_connection_still_works():
    client = FakeClient(calendar(), prefix="googlecalendar-TOTALLY-DIFFERENT.")
    assert check_conflict(client, starts_at="2026-09-05T09:20:00").clear


def test_the_shortest_match_wins():
    """`list_event_instances` also ends in a superstring of `events` for some connectors
    and is a different call entirely."""
    from skillforge.core.clinic import resolve

    assert resolve(FakeClient(calendar()), "list_events").endswith("list_events")


def test_resolution_can_only_reach_granted_tools():
    """The candidate list is the speaker's own introspected tools, so this cannot name a
    primitive they were never given."""
    from skillforge.core.clinic import resolve

    with pytest.raises(ClinicError, match="no granted primitive"):
        resolve(FakeClient(calendar()), "delete_everything")


def test_timestamps_carry_an_offset():
    """`isoformat()` on a naive datetime is valid ISO 8601 and NOT valid RFC3339, and the
    Calendar connector answers FAILED_PRECONDITION without saying which field it disliked.
    Observed against a live calendar: identical call, one with `Z` and one without."""
    client = FakeClient(calendar())
    check_conflict(client, starts_at="2026-09-05T09:20:00")

    _, kwargs = client.calls[0]
    for field in ("time_min", "time_max"):
        assert kwargs[field].endswith("Z") or "+" in kwargs[field][10:], \
            f"{field}={kwargs[field]} has no timezone offset"


def test_an_offset_aware_time_is_not_double_stamped():
    client = FakeClient(calendar())
    check_conflict(client, starts_at="2026-09-05T09:20:00+01:00")

    _, kwargs = client.calls[0]
    assert not kwargs["time_min"].endswith("ZZ")
    assert kwargs["time_min"].count("+") == 1


def test_the_events_key_the_connector_actually_uses():
    """The live response is `{"events": [...]}`, not the `items` most Google clients
    return. Both are handled, but this pins the real one."""
    client = FakeClient({"list_events": {"events": [
        event("Clinic", "2026-09-05T09:20:00", "2026-09-05T09:40:00")]}})
    assert not check_conflict(client, starts_at="2026-09-05T09:20:00").clear
