"""What ClaudeGenerator actually puts on the wire.

Points the Anthropic SDK at a local stub that captures the request body and replays a
canned SSE stream. This proves the parameter names and nesting survive the SDK's
serializer and arrive in the shape the API expects — several of these are current-model
requirements that fail with a 400 rather than a warning, so they're worth pinning:

  * `effort` and `format` live inside `output_config`, not at the top level
  * `temperature` / `top_p` / `top_k` are rejected on claude-opus-5
  * a trailing assistant-turn prefill is rejected on claude-opus-5
  * code generation streams, because it is a long-output request

It does *not* prove the API accepts the request — that needs real credentials.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from skillforge.adapters.fake_scoped import BoundScopedClient, FakeScalekitActions
from skillforge.adapters.llm import ClaudeGenerator, ForgeRequest, GenerationError

pytest.importorskip("anthropic")

PAYLOAD = {
    "skill": "escalate_and_rebalance",
    "description": "Escalate an issue and re-triage.",
    "primitives_used": ["linear.get_issue", "linear.update_issue"],
    "effects": "write",
    "reversible": True,
    "inverse": "restore_snapshot",
    "source": 'def run(scoped_client, issue_id):\n'
              '    return scoped_client.call("linear.get_issue", issue_id=issue_id)\n',
    "test_source": "def check(result, calls):\n    assert result\n",
}


def _sse() -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_stub", "type": "message", "role": "assistant",
            "model": "claude-opus-5", "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        }}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta",
                                           "text": json.dumps(PAYLOAD)}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 120}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events
    ).encode()


@pytest.fixture
def stub_api():
    """A local endpoint that records the request body and replays a canned stream."""
    captured: dict = {}
    stream = _sse()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            captured.update(json.loads(self.rfile.read(length)))
            captured["_path"] = self.path
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(stream)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", captured
    finally:
        server.shutdown()


@pytest.fixture
def generated(stub_api, client_for):
    import anthropic

    base_url, captured = stub_api
    client = anthropic.Anthropic(api_key="sk-ant-stub", base_url=base_url)
    tools = client_for("priya@co").granted_tools()
    request = ForgeRequest(
        intent="escalate LIN-402 to Sam and re-triage what I was blocking",
        speaker="priya@co", apps=["linear"], tools=tools,
    )
    generation = ClaudeGenerator(client=client).generate(request)
    return generation, captured, tools


# --- request shape ----------------------------------------------------------


def test_posts_a_streaming_messages_request(generated):
    _, captured, _ = generated
    assert captured["_path"] == "/v1/messages"
    assert captured["model"] == "claude-opus-5"
    assert captured["stream"] is True
    assert captured["max_tokens"] >= 32000, "codegen needs output headroom"


def test_effort_and_format_are_nested_in_output_config(generated):
    _, captured, _ = generated
    assert captured["output_config"]["effort"] == "high"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert "output_format" not in captured, "the top-level parameter is deprecated"
    assert "effort" not in captured, "effort is not a top-level parameter"


def test_uses_adaptive_thinking(generated):
    _, captured, _ = generated
    assert captured["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(captured["thinking"])


def test_omits_parameters_the_model_rejects(generated):
    _, captured, _ = generated
    for param in ("temperature", "top_p", "top_k"):
        assert param not in captured, f"{param} returns 400 on claude-opus-5"
    assert all(m["role"] != "assistant" for m in captured["messages"]), \
        "a trailing assistant prefill returns 400 on claude-opus-5"


def test_the_response_schema_is_closed(generated):
    _, captured, _ = generated
    schema = captured["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_the_prompt_carries_the_scope_ceiling(generated):
    _, captured, tools = generated
    prompt = captured["messages"][0]["content"]
    for tool in tools:
        assert tool["definition"]["name"] in prompt
    assert "linear.delete_project" not in prompt, "showed a primitive nobody holds"
    assert "scoped_client" in captured["system"]


# --- response handling ------------------------------------------------------


def test_splits_the_response_into_code_test_and_manifest(generated):
    generation, _, _ = generated
    assert "def run(scoped_client" in generation.source
    assert "def check(" in generation.test_source
    assert generation.manifest["skill"] == "escalate_and_rebalance"


def test_the_host_owns_apps_mark_and_trust(generated):
    """A skill cannot claim someone else's mark or self-certify, and `apps` is derived
    from the primitives it declared rather than asserted independently — so it cannot
    drift from what the skill actually reaches."""
    generation, _, _ = generated
    assert generation.manifest["apps"] == ["linear"]
    assert generation.manifest["forged_by"] == "priya@co"
    assert generation.manifest["trust"] == "quarantined"


@pytest.mark.parametrize("stop_reason, expected", [
    ("refusal", "declined"),
    ("max_tokens", "truncated"),
])
def test_refusal_and_truncation_are_surfaced_not_swallowed(stop_reason, expected):
    class FakeMessage:
        def __init__(self):
            self.stop_reason = stop_reason
            self.stop_details = {"category": "cyber"}
            self.content = []

    class FakeStream:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def get_final_message(self): return FakeMessage()

    class FakeClient:
        class messages:
            @staticmethod
            def stream(**kwargs): return FakeStream()

    request = ForgeRequest(intent="x", speaker="priya@co", apps=["linear"], tools=[])
    with pytest.raises(GenerationError, match=expected):
        ClaudeGenerator(client=FakeClient()).generate(request)
