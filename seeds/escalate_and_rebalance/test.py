"""The tempering test for escalate_and_rebalance.

Shaped as the forge will generate it: a single `check(result, calls)` that asserts against
the skill's *observed* return value and its actual call log. A skill that does not pass
this stays quarantined and never touches the real world.
"""


def check(result, calls):
    assert result["observed_assignee"] == "sam@co", (
        f"expected the issue to end up with sam@co, observed {result['observed_assignee']!r}"
    )
    assert result["observed_priority"] == "Urgent", (
        f"expected Urgent, observed {result['observed_priority']!r}"
    )
    assert "CYC-14" in result["observed_links"], (
        f"expected the issue linked to the active cycle, observed {result['observed_links']!r}"
    )
    assert result["previous_assignee"] == "priya@co"

    # The rebalance pass should touch the previous assignee's High/Urgent work and
    # nothing below that floor, and never the escalated issue itself.
    assert sorted(result["rebalanced"]) == ["LIN-377", "LIN-388"], (
        f"unexpected re-triage set: {result['rebalanced']!r}"
    )

    primitives = [c["primitive"] for c in calls]
    assert primitives[0] == "linear.get_issue", "the skill must read before it writes"
    assert primitives[-1] == "linear.get_issue", "the skill must read back after acting"
    assert all(c["ok"] for c in calls), (
        f"every scoped call should have succeeded: {[c for c in calls if not c['ok']]}"
    )
