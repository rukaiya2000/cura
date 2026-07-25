def check(result, calls):
    assert result["event_id"], "no appointment was created"
    assert result["invited"], "the patient was not invited"
    assert result["tasks"], "the blood test was not logged as a task"
    assert calls[0]["primitive"] == "hubspot.get_contact", "must read before writing"
    assert calls[-1]["primitive"] == "hubspot.get_contact", "must read back after acting"
