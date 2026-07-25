"""The real Scalekit adapter, against a fake SDK.

No network. What's under test is the adapter's own judgement — name translation, effect
inference, and failure handling — because that is where a wrong guess is quiet.

The live behaviour these fakes stand in for is only partly verified: an unconnected
identifier raising `ScalekitNotFoundException` was confirmed against the real API; whether
an *ungranted* call raises or returns an error object was not, so both shapes are tested.
"""

import pytest

from skillforge.adapters.scalekit_client import (
    NoConnectedAccount,
    ScalekitActions,
    ScalekitScopedClient,
    classify_effect,
    normalize,
)
from skillforge.core.manifest import Effect
from skillforge.core.sandbox import ScopedCallDenied


class ScalekitNotFoundException(Exception):
    """Same name as the SDK's, since the adapter matches on the type name."""


class FakeTools:
    def __init__(self, by_identifier, error=None):
        self.by_identifier = by_identifier
        self.error = error
        self.calls = []

    def list_scoped_tools(self, *, identifier, filter=None, page_size=None):
        self.calls.append({"identifier": identifier, "filter": filter})
        if self.error:
            raise self.error
        if identifier not in self.by_identifier:
            raise ScalekitNotFoundException(
                "error getting connected account by identifier")
        return [
            {"definition": {"name": name, "description": desc, "input_schema": {}}}
            for name, desc in self.by_identifier[identifier]
        ], None


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeActions:
    def __init__(self, by_identifier, *, execute=None, error=None):
        self.tools = FakeTools(by_identifier, error=error)
        self._execute = execute
        self.executed = []

    def execute_tool(self, *, tool_name, identifier, tool_input):
        self.executed.append({"tool_name": tool_name, "identifier": identifier,
                              "tool_input": tool_input})
        if self._execute:
            return self._execute(tool_name, identifier, tool_input)
        return FakeResult({"ok": True, "tool": tool_name})


LINEAR_TOOLS = [
    ("linear_get_issue", "Fetch a single issue."),
    ("linear_update_issue", "Update an issue's assignee or priority."),
    ("linear_create_comment", "Add a comment to an issue."),
    ("linear_delete_project", "Delete a project and everything in it."),
]

READ_ONLY_TOOLS = [
    ("linear_get_issue", "Fetch a single issue."),
    ("linear_create_comment", "Add a comment to an issue."),
]


def actions_for(**by_identifier) -> ScalekitActions:
    return ScalekitActions(actions=FakeActions(by_identifier), connection="linear")


# --- name translation --------------------------------------------------------


@pytest.mark.parametrize("wire, expected", [
    ("linear_get_issue", "linear.get_issue"),
    ("LINEAR_GET_ISSUE", "linear.get_issue"),
    ("linear.get_issue", "linear.get_issue"),
    ("linear__get_issue", "linear.get_issue"),
    ("get_issue", "linear.get_issue"),              # no prefix at all
])
def test_normalizes_whatever_shape_scalekit_uses(wire, expected):
    assert normalize(wire, "linear") == expected


def test_the_wire_name_is_remembered_not_reconstructed():
    """Calling must use the exact name Scalekit gave us, never one we rebuilt by string
    surgery — normalisation is lossy and a guessed name is a silent 404."""
    actions = actions_for(**{"priya@co": [("LINEAR_GET_ISSUE", "Fetch.")]})
    client = ScalekitScopedClient(actions, "priya@co")
    assert client.granted_primitives() == {"linear.get_issue"}

    client.call("linear.get_issue", issue_id="LIN-402")
    assert actions.actions.executed[0]["tool_name"] == "LINEAR_GET_ISSUE"


def test_an_unknown_primitive_is_refused_rather_than_guessed():
    actions = actions_for(**{"priya@co": [("linear_get_issue", "Fetch.")]})
    client = ScalekitScopedClient(actions, "priya@co")
    client.granted_tools()

    with pytest.raises(ScopedCallDenied, match="not a known tool"):
        client.call("linear.invented_tool", x=1)
    assert actions.actions.executed == [], "guessed a wire name and called it anyway"


# --- effect inference --------------------------------------------------------


@pytest.mark.parametrize("name, expected", [
    ("linear.get_issue", Effect.READ),
    ("linear.list_issues", Effect.READ),
    ("linear.search_issues", Effect.READ),
    ("linear.update_issue", Effect.WRITE),
    ("linear.create_comment", Effect.WRITE),
    ("linear.assign_issue", Effect.WRITE),
    ("linear.delete_project", Effect.DESTRUCTIVE),
    ("linear.archive_project", Effect.DESTRUCTIVE),
    ("linear.remove_label", Effect.DESTRUCTIVE),
])
def test_classifies_effects_from_the_verb(name, expected):
    assert classify_effect(name) is expected


def test_an_unrecognised_verb_defaults_to_write_not_read():
    """The safety-biased default. Misfiling a write as a read waves it past every gate
    that matters; the reverse costs one unnecessary confirmation."""
    assert classify_effect("linear.frobnicate_widget") is Effect.WRITE
    assert classify_effect("linear.xyzzy", "does something opaque") is Effect.WRITE


def test_destructive_wins_over_a_co_occurring_write_verb():
    """"Update or delete a label" is a delete."""
    assert classify_effect("linear.delete_or_update_label") is Effect.DESTRUCTIVE
    assert classify_effect("linear.set_status",
                           "Set a status. May remove the previous one.") is Effect.DESTRUCTIVE


def test_effects_reach_the_tool_listing():
    actions = actions_for(**{"priya@co": LINEAR_TOOLS})
    by_name = {t["definition"]["name"]: t["effect"]
               for t in ScalekitScopedClient(actions, "priya@co").granted_tools()}

    assert by_name["linear.get_issue"] == "read"
    assert by_name["linear.update_issue"] == "write"
    assert by_name["linear.delete_project"] == "destructive"


# --- the scope ceiling, against the real shape -------------------------------


def test_each_identifier_sees_only_its_own_tools():
    actions = actions_for(**{"priya@co": LINEAR_TOOLS, "sam@co": READ_ONLY_TOOLS})

    priya = ScalekitScopedClient(actions, "priya@co").granted_primitives()
    sam = ScalekitScopedClient(actions, "sam@co").granted_primitives()

    assert "linear.update_issue" in priya
    assert "linear.update_issue" not in sam
    assert sam < priya


def test_the_connection_filter_is_passed_through():
    """A mismatched connection name returns nothing with no error, so pin that we send
    exactly what was configured."""
    actions = ScalekitActions(actions=FakeActions({"priya@co": LINEAR_TOOLS}),
                              connection="Linear-Prod")
    try:
        ScalekitScopedClient(actions, "priya@co").granted_tools()
    except NoConnectedAccount:
        pass
    assert actions.actions.tools.calls[0]["filter"] == {
        "connection_names": ["Linear-Prod"]}


def test_identity_is_bound_at_construction_and_reaches_every_call():
    actions = actions_for(**{"sam@co": READ_ONLY_TOOLS})
    client = ScalekitScopedClient(actions, "sam@co")
    client.granted_tools()
    client.call("linear.get_issue", issue_id="LIN-402")

    assert actions.actions.executed[0]["identifier"] == "sam@co"
    # There is no parameter through which generated code could change it.
    with pytest.raises(TypeError):
        client.call("linear.get_issue", identifier="priya@co", issue_id="LIN-402")


# --- failure shapes ----------------------------------------------------------


def test_an_unconnected_identifier_is_reported_as_such():
    """Confirmed against the live API: this raises rather than returning empty. Telling
    someone to authorise when they already have is its own kind of unhelpful."""
    actions = actions_for(**{"priya@co": LINEAR_TOOLS})

    with pytest.raises(NoConnectedAccount, match="no connected account"):
        ScalekitScopedClient(actions, "stranger@co").granted_tools()


def test_connected_but_empty_is_not_confused_with_unconnected():
    actions = actions_for(**{"priya@co": []})
    assert ScalekitScopedClient(actions, "priya@co").granted_primitives() == set()


def test_an_unrelated_listing_error_is_not_swallowed():
    """Only NOT_FOUND means "unconnected". A network or auth failure must surface."""
    actions = ScalekitActions(
        actions=FakeActions({}, error=RuntimeError("connection reset")),
        connection="linear")
    with pytest.raises(RuntimeError, match="connection reset"):
        ScalekitScopedClient(actions, "priya@co").granted_tools()


def test_a_raised_execution_failure_becomes_a_denial():
    def boom(tool_name, identifier, tool_input):
        raise PermissionError("insufficient scope for this tool")

    actions = ScalekitActions(
        actions=FakeActions({"sam@co": READ_ONLY_TOOLS}, execute=boom),
        connection="linear")
    client = ScalekitScopedClient(actions, "sam@co")
    client.granted_tools()

    with pytest.raises(ScopedCallDenied, match="insufficient scope"):
        client.call("linear.get_issue", issue_id="LIN-402")


@pytest.mark.parametrize("payload", [
    {"error": "forbidden"},
    {"error_message": "user lacks permission"},
    {"success": False, "message": "denied"},
    {"ok": False, "message": "denied"},
])
def test_an_error_shaped_result_also_becomes_a_denial(payload):
    """Not yet verified against a live ungranted call, so both shapes are handled.
    Treating a refusal as a success would turn every denial into a silent no-op."""
    actions = ScalekitActions(
        actions=FakeActions({"sam@co": READ_ONLY_TOOLS},
                            execute=lambda *a: FakeResult(payload)),
        connection="linear")
    client = ScalekitScopedClient(actions, "sam@co")
    client.granted_tools()

    with pytest.raises(ScopedCallDenied):
        client.call("linear.get_issue", issue_id="LIN-402")


def test_a_successful_result_is_unwrapped():
    actions = ScalekitActions(
        actions=FakeActions({"priya@co": LINEAR_TOOLS},
                            execute=lambda *a: FakeResult({"id": "LIN-402",
                                                           "priority": "Urgent"})),
        connection="linear")
    client = ScalekitScopedClient(actions, "priya@co")
    client.granted_tools()

    assert client.call("linear.get_issue", issue_id="LIN-402")["priority"] == "Urgent"


# --- interface parity with the fake ------------------------------------------


def test_it_is_a_drop_in_for_the_fake_client():
    """Everything above the adapter depends on this surface and nothing else."""
    from skillforge.adapters.fake_scoped import BoundScopedClient

    required = {"identifier", "granted_tools", "granted_primitives", "call"}
    assert required <= set(dir(BoundScopedClient))
    assert required <= set(dir(ScalekitScopedClient))


@pytest.mark.parametrize("tool_name", [
    "linear.frobnicate_widget",   # "get" hides inside "widget"
    "linear.update_address",      # "add" hides inside "address"
    "linear.reset_asset",         # "set" hides inside "asset"
    "linear.review_board",        # "view" hides inside "review"
])
def test_a_hint_hiding_inside_an_unrelated_word_does_not_classify(tool_name):
    """Every hint is short enough to hide in another word, and every one of those
    mistakes points the unsafe way — so whole-word matching, not substring."""
    assert classify_effect(tool_name) is not Effect.READ


def test_camel_case_verbs_are_understood():
    assert classify_effect("linear.getIssue") is Effect.READ
    assert classify_effect("linear.deleteProject") is Effect.DESTRUCTIVE
