"""Milestone 1 — the foundation is real if these hold.

Two headline assertions from the plan:
  * a skill runs sandboxed and reports observed state
  * a skill that touches a primitive it did not declare is rejected

Plus the properties the governance story rests on: no ambient credentials, identity is
not the skill's to choose, and the same skill run by two different people produces two
different outcomes.
"""

import os

import pytest

from skillforge.core.checker import check_code, reconcile
from skillforge.core.library import TRUST_THRESHOLD, new_skill
from skillforge.core.manifest import (
    CapabilityManifest,
    Effect,
    ManifestError,
    Trust,
)
from skillforge.core import sandbox as sandbox_mod
from skillforge.core.sandbox import run_skill
from skillforge.core.temper import temper

DECLARED = {
    "linear.get_issue", "linear.update_issue", "linear.get_active_cycle",
    "linear.link_issue", "linear.list_issues", "linear.create_comment",
}


def _run(source, client, *, allowed=DECLARED, kwargs=None, timeout=10.0):
    return run_skill(source, client=client, kwargs=kwargs or {},
                     allowed_primitives=allowed, timeout=timeout)


# --- the skill runs, sandboxed, and reports what it observed -----------------


def test_composite_skill_runs_sandboxed_and_reports_observed_state(
    escalate_skill, client_for, actions
):
    result = _run(
        escalate_skill.source,
        client_for("priya@co"),
        kwargs={"issue_id": "LIN-402", "escalate_to": "sam@co"},
    )

    assert result.ok, result.error
    assert result.result["observed_assignee"] == "sam@co"
    assert result.result["observed_priority"] == "Urgent"
    assert "CYC-14" in result.result["observed_links"]
    assert sorted(result.result["rebalanced"]) == ["LIN-377", "LIN-388"]

    # It composed six distinct primitives with reads between the writes.
    assert result.primitives_called == DECLARED
    assert result.calls[0].primitive == "linear.get_issue"
    assert result.calls[-1].primitive == "linear.get_issue"

    # And the mutation actually landed in the workspace.
    assert actions.workspace["issues"]["LIN-402"]["assignee"] == "sam@co"


def test_tempering_promotes_a_quarantined_skill(escalate_skill, client_for, library):
    library.register(escalate_skill)
    assert escalate_skill.trust is Trust.QUARANTINED
    assert not escalate_skill.manifest.may_execute_for_real

    outcome = temper(
        escalate_skill,
        client_factory=lambda: client_for("priya@co"),
        kwargs={"issue_id": "LIN-402", "escalate_to": "sam@co"},
    )
    assert outcome.ok, outcome.reason

    library.temper(escalate_skill)
    assert escalate_skill.trust is Trust.TEMPERED
    assert escalate_skill.manifest.may_execute_for_real


def test_a_skill_with_no_test_cannot_be_tempered(escalate_skill, client_for):
    escalate_skill.test_source = ""
    outcome = temper(
        escalate_skill,
        client_factory=lambda: client_for("priya@co"),
        kwargs={"issue_id": "LIN-402", "escalate_to": "sam@co"},
    )
    assert not outcome.ok
    assert "no generated test" in outcome.reason


def test_failing_test_keeps_the_skill_quarantined_and_explains_why(
    escalate_skill, client_for
):
    escalate_skill.test_source = (
        "def check(result, calls):\n"
        "    assert result['observed_priority'] == 'Low', 'wanted Low'\n"
    )
    outcome = temper(
        escalate_skill,
        client_factory=lambda: client_for("priya@co"),
        kwargs={"issue_id": "LIN-402", "escalate_to": "sam@co"},
    )
    assert not outcome.ok
    # The reason is what Reflexion feeds back into the next generation attempt.
    assert "wanted Low" in outcome.reason


# --- undeclared reach is rejected -------------------------------------------


def test_code_calling_an_undeclared_primitive_is_rejected(escalate_skill):
    manifest = escalate_skill.manifest
    manifest.primitives_used = [p for p in manifest.primitives_used
                                if p != "linear.create_comment"]

    res = reconcile(escalate_skill.source, manifest)

    assert not res.ok
    assert any("linear.create_comment" in e and "not declared" in e for e in res.errors)


def test_declared_but_unused_primitive_is_only_a_warning(escalate_skill):
    escalate_skill.manifest.primitives_used.append("linear.delete_project")

    res = reconcile(escalate_skill.source, escalate_skill.manifest)

    assert res.ok
    assert any("delete_project" in w for w in res.warnings)


def test_host_refuses_an_undeclared_primitive_even_if_the_check_was_skipped(client_for):
    """Defence in depth: the sandbox enforces the declared set at runtime too."""
    source = (
        "def run(scoped_client):\n"
        "    return scoped_client.call('linear.delete_project', project_id='PRJ-1')\n"
    )
    result = _run(source, client_for("priya@co"), allowed={"linear.get_issue"})

    assert not result.ok
    assert "outside this skill's declared primitives" in result.error
    assert result.calls[0].ok is False


# --- the static gate --------------------------------------------------------


@pytest.mark.parametrize(
    "snippet, expected",
    [
        ("import os\ndef run(scoped_client):\n    return os.environ\n", "allowlist"),
        ("import requests\ndef run(scoped_client):\n    return 1\n", "allowlist"),
        ("def run(scoped_client):\n    return open('/etc/passwd').read()\n", "not allowed"),
        ("def run(scoped_client):\n    return eval('1+1')\n", "not allowed"),
        ("def run(scoped_client):\n    return run.__globals__\n", "not allowed"),
        ("def run(scoped_client):\n    return ().__class__\n", "not allowed"),
        ("def run(client):\n    return 1\n", "must take scoped_client"),
        ("def helper():\n    return 1\n", "entrypoint"),
        (
            "def run(scoped_client):\n"
            "    return scoped_client.call('linear.get_issue', identifier='admin@co')\n",
            "may not set 'identifier'",
        ),
        (
            "def run(scoped_client):\n"
            "    name = 'linear.' + 'get_issue'\n"
            "    return scoped_client.call(name)\n",
            "literal string",
        ),
        (
            "def run(scoped_client):\n    return scoped_client.execute_tool('x')\n",
            "may only use scoped_client.call()",
        ),
        (
            "def run(scoped_client):\n"
            "    return scoped_client.call('linear.get_issue', 'LIN-402')\n",
            "as keywords",
        ),
        ("x = open\ndef run(scoped_client):\n    return x\n", "not allowed"),
    ],
)
def test_static_gate_rejects(snippet, expected):
    res = check_code(snippet)
    assert not res.ok, f"expected rejection of:\n{snippet}"
    assert any(expected in e for e in res.errors), res.errors


def test_static_gate_accepts_a_reasonable_skill():
    source = (
        "import json\n"
        "FLOOR = 'High'\n"
        "def _fmt(issue):\n"
        "    return json.dumps({'id': issue['id']})\n"
        "def run(scoped_client, issue_id):\n"
        "    issue = scoped_client.call('linear.get_issue', issue_id=issue_id)\n"
        "    return _fmt(issue)\n"
    )
    res = check_code(source)
    assert res.ok, res.errors
    assert res.primitives_called == {"linear.get_issue"}


# --- no ambient credentials -------------------------------------------------


def test_the_sandbox_child_cannot_reach_host_credentials(client_for, monkeypatch):
    """A secret in the host's environment must be unreachable from a skill."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_do_not_leak")
    assert os.environ["LINEAR_API_KEY"] == "lin_api_do_not_leak"

    # Statically: the import is refused before anything runs.
    source = "import os\ndef run(scoped_client):\n    return os.environ.get('LINEAR_API_KEY')\n"
    assert not check_code(source).ok

    # And at runtime, even reaching for the import machinery fails inside the child.
    sneaky = (
        "def run(scoped_client):\n"
        "    mod = __import__('os')\n"
        "    return mod.environ.get('LINEAR_API_KEY')\n"
    )
    result = _run(sneaky, client_for("priya@co"))
    assert not result.ok
    assert "lin_api_do_not_leak" not in str(result.result)


def test_the_child_environment_holds_no_credentials_at_all(client_for, monkeypatch):
    """The structural half of the guarantee, tested independently of the import gate.

    Widen the allowlist so a skill *can* reach the environment, then assert there is
    nothing there to find. This is what makes "the forge cannot be talked into exceeding
    the speaker's scope" a property rather than a prompt instruction.
    """
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_do_not_leak")
    monkeypatch.setenv("SCALEKIT_CLIENT_SECRET", "sk_do_not_leak")
    monkeypatch.setattr(sandbox_mod, "IMPORT_ALLOWLIST", frozenset({"os"}))

    source = (
        "import os\n"
        "def run(scoped_client):\n"
        "    return sorted(os.environ.keys())\n"
    )
    result = _run(source, client_for("priya@co"), allowed=set())

    assert result.ok, result.error
    leaked = [k for k in result.result if "KEY" in k or "SECRET" in k or "TOKEN" in k]
    assert not leaked, f"credentials visible to a skill: {leaked}"


def test_the_skill_cannot_choose_who_it_acts_as(client_for, actions):
    """Identity is welded on by the host; the child has no way to name a user."""
    source = (
        "def run(scoped_client):\n"
        "    return scoped_client.call('linear.get_issue', issue_id='LIN-402')\n"
    )
    result = _run(source, client_for("sam@co"), allowed={"linear.get_issue"})

    assert result.ok, result.error
    assert actions.log[-1]["identifier"] == "sam@co"


def test_a_runaway_skill_is_killed(client_for):
    source = "def run(scoped_client):\n    while True:\n        pass\n"
    result = _run(source, client_for("priya@co"), timeout=1.0)

    assert not result.ok
    assert "timeout" in result.error


# --- one sentence, two mouths ----------------------------------------------


def test_same_skill_same_input_different_speaker_different_outcome(
    escalate_skill, client_for, actions
):
    """The demo's second beat, as a test.

    Identical code, identical arguments. The only variable is who is speaking, and that
    is enough to change the outcome — because Sam was never granted update_issue.
    """
    kwargs = {"issue_id": "LIN-402", "escalate_to": "sam@co"}

    as_priya = _run(escalate_skill.source, client_for("priya@co"), kwargs=kwargs)
    assert as_priya.ok, as_priya.error
    assert as_priya.result["observed_assignee"] == "sam@co"

    actions.workspace["issues"]["LIN-402"]["assignee"] = "priya@co"
    actions.workspace["issues"]["LIN-402"]["priority"] = "Medium"

    as_sam = _run(escalate_skill.source, client_for("sam@co"), kwargs=kwargs)
    assert not as_sam.ok
    assert "outside your maker's mark" in as_sam.error

    denied = [c for c in as_sam.calls if not c.ok]
    assert denied and denied[0].primitive == "linear.update_issue"
    # And nothing changed.
    assert actions.workspace["issues"]["LIN-402"]["priority"] == "Medium"


def test_the_forge_is_never_shown_a_capability_the_speaker_lacks(client_for):
    """Governance by construction: it cannot compose what it cannot see."""
    priya = client_for("priya@co").granted_primitives()
    sam = client_for("sam@co").granted_primitives()

    assert "linear.update_issue" in priya
    assert "linear.update_issue" not in sam
    assert "linear.delete_project" not in priya | sam


# --- manifest validation ----------------------------------------------------


def test_manifest_rejects_a_primitive_outside_the_declared_app():
    with pytest.raises(ManifestError, match="outside the declared app"):
        CapabilityManifest(
            skill="sneaky", apps=["linear"], effects=Effect.WRITE,
            primitives_used=["linear.get_issue", "workday.create_payment"],
        )


def test_a_skill_may_span_the_apps_it_declares():
    """The whole reason `app` became `apps`. "Book the follow-up and log it to the
    record" is one intent reaching two services; a single-app field rejected it, which
    made the most valuable skill in a clinical workflow unforgeable."""
    m = CapabilityManifest(
        skill="schedule_followup_and_log_request",
        apps=["calendar", "hubspot"], effects=Effect.WRITE,
        primitives_used=["calendar.create_event", "hubspot.create_note",
                         "hubspot.create_task"],
        reversible=True, inverse="cancel_followup",
    )
    assert m.apps == ["calendar", "hubspot"]


def test_spanning_apps_does_not_weaken_the_boundary():
    """Widening the field must not widen the ceiling: a primitive from a *third*,
    undeclared app is refused exactly as it always was."""
    with pytest.raises(ManifestError, match="outside the declared apps"):
        CapabilityManifest(
            skill="overreach", apps=["calendar", "hubspot"], effects=Effect.WRITE,
            primitives_used=["calendar.create_event", "workday.create_payment"],
        )


def test_a_single_app_needs_no_ceremony():
    """`apps="linear"` and `apps=["linear"]` mean the same thing — the common case
    should not have to think about the uncommon one."""
    m = CapabilityManifest(skill="peek", apps="linear", effects=Effect.READ,
                           primitives_used=["linear.get_issue"])
    assert m.apps == ["linear"]


def test_duplicate_apps_are_collapsed_but_order_is_kept():
    m = CapabilityManifest(
        skill="noisy", apps=["hubspot", "calendar", "hubspot"], effects=Effect.READ,
        primitives_used=["hubspot.get_contact", "calendar.list_events"],
    )
    assert m.apps == ["hubspot", "calendar"]


def test_a_manifest_written_before_apps_existed_still_loads():
    """Migration shim. Manifests are persisted to armory/ and outlive the schema; a
    reader that rejects yesterday's file makes its problem the user's problem."""
    m = CapabilityManifest.from_dict({
        "skill": "legacy", "app": "linear", "effects": "read",
        "primitives_used": ["linear.get_issue"],
    })
    assert m.apps == ["linear"]
    assert "app" not in m.to_dict(), "the old key must not survive a round trip"


def test_the_shim_never_overrides_an_explicit_apps():
    """Both keys present means someone is confused; the new one wins and the stale one
    is not silently merged in."""
    with pytest.raises(ManifestError, match="unknown manifest fields"):
        CapabilityManifest.from_dict({
            "skill": "confused", "app": "workday", "apps": ["linear"],
            "effects": "read", "primitives_used": ["linear.get_issue"],
        })


def test_manifest_rejects_reversible_without_an_inverse():
    with pytest.raises(ManifestError, match="must name their inverse"):
        CapabilityManifest(
            skill="half_done", apps=["linear"], effects=Effect.WRITE,
            primitives_used=["linear.update_issue"], reversible=True,
        )


def test_destructive_skills_always_need_confirmation():
    m = CapabilityManifest(
        skill="nuke", apps=["linear"], effects=Effect.DESTRUCTIVE,
        primitives_used=["linear.delete_project"], trust=Trust.TRUSTED,
    )
    assert m.needs_confirmation


# --- the armory -------------------------------------------------------------


def test_library_round_trip_and_versioning(library, escalate_skill):
    library.register(escalate_skill)
    loaded = library.load("escalate_and_rebalance")

    assert loaded.source == escalate_skill.source
    assert loaded.manifest.primitives_used == escalate_skill.manifest.primitives_used
    assert loaded.trust is Trust.QUARANTINED
    assert library.versions("escalate_and_rebalance") == [1]
    assert library.next_version("escalate_and_rebalance") == 2

    v2 = new_skill(
        CapabilityManifest.from_dict({**escalate_skill.manifest.to_dict(), "version": 2}),
        source=escalate_skill.source,
        test_source=escalate_skill.test_source,
    )
    library.register(v2)
    assert library.versions("escalate_and_rebalance") == [1, 2]
    assert library.load("escalate_and_rebalance").version == 2


def test_trust_follows_the_evidence(library, escalate_skill):
    library.register(escalate_skill)
    library.temper(escalate_skill)

    for _ in range(TRUST_THRESHOLD - 1):
        library.record_execution(escalate_skill, ok=True, duration_s=0.4)
    assert escalate_skill.trust is Trust.TEMPERED

    library.record_execution(escalate_skill, ok=True, duration_s=0.38)
    assert escalate_skill.trust is Trust.TRUSTED
    assert library.load("escalate_and_rebalance").trust is Trust.TRUSTED

    stats = library.load("escalate_and_rebalance").stats
    assert stats.executions == TRUST_THRESHOLD
    assert stats.success_rate == 1.0


def test_a_failing_skill_does_not_earn_trust_and_can_be_melted_down(library, escalate_skill):
    library.register(escalate_skill)
    library.temper(escalate_skill)

    library.record_execution(escalate_skill, ok=False, duration_s=0.2)
    for _ in range(TRUST_THRESHOLD):
        library.record_execution(escalate_skill, ok=True, duration_s=0.2)

    assert escalate_skill.trust is Trust.TEMPERED  # one failure blocks promotion

    library.melt_down(escalate_skill)
    assert library.load("escalate_and_rebalance").trust is Trust.QUARANTINED


def test_denials_are_recorded_separately_from_failures(library, escalate_skill):
    library.register(escalate_skill)
    library.record_execution(escalate_skill, ok=False, denied=True)

    stats = library.load("escalate_and_rebalance").stats
    assert stats.denials == 1
    assert stats.failures == 0
