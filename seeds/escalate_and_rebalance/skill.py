"""escalate_and_rebalance — the shape a forged composite skill takes.

Hand-written for milestone 1 so the manifest, static gate, sandbox and armory can be
exercised before the forge exists. It is deliberately a *composition* of six scoped
primitives with state read between them, not a single mutation: escalate the issue, pull
it into the active cycle, then flag whatever the previous assignee was still blocking.

Note the last call. The skill reports what it *observed* after acting, never what it
intended — so the caller can state facts about the world rather than about the agent.
"""

URGENT = "Urgent"
REBALANCE_FLOOR = "High"


def run(scoped_client, issue_id, escalate_to):
    issue = scoped_client.call("linear.get_issue", issue_id=issue_id)
    previous_assignee = issue["assignee"]

    scoped_client.call(
        "linear.update_issue",
        issue_id=issue_id,
        assignee=escalate_to,
        priority=URGENT,
    )

    cycle = scoped_client.call("linear.get_active_cycle", team_id=issue["team_id"])
    scoped_client.call("linear.link_issue", issue_id=issue_id, target_id=cycle["id"])

    rebalanced = []
    if previous_assignee and previous_assignee != escalate_to:
        blockers = scoped_client.call(
            "linear.list_issues",
            assignee=previous_assignee,
            priority_gte=REBALANCE_FLOOR,
        )
        for blocker in blockers:
            if blocker["id"] == issue_id:
                continue
            scoped_client.call(
                "linear.create_comment",
                issue_id=blocker["id"],
                body=(
                    f"Re-triage: {issue_id} was escalated to {escalate_to} and pulled into "
                    f"{cycle['name']}. Confirm this is still blocked on {previous_assignee}."
                ),
            )
            rebalanced.append(blocker["id"])

    observed = scoped_client.call("linear.get_issue", issue_id=issue_id)
    return {
        "issue_id": observed["id"],
        "observed_assignee": observed["assignee"],
        "observed_priority": observed["priority"],
        "observed_links": observed["links"],
        "previous_assignee": previous_assignee,
        "rebalanced": rebalanced,
        "cycle": cycle["name"],
    }
