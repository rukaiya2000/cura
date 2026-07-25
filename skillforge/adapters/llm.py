"""Code generation behind one interface, so the forge doesn't know who writes the code.

Two implementations, both satisfying `CodeGenerator`:

  * `ClaudeGenerator`  — real generation via the Anthropic SDK.
  * `ScriptedGenerator` — deterministic canned attempts. Lets the whole anvil loop,
    including the Reflexion retry, be tested without model spend.

`SYSTEM_PROMPT` and `RESPONSE_SCHEMA` are provider-neutral on purpose: a different model
means a new class implementing `generate()` and nothing else in the forge changes.

The generator is told what the *speaker* holds, never what the app can do. That is the
whole point: a capability outside the introspected set cannot be composed, because the
model was never shown it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

MODEL = "claude-opus-5"

#: The contract generated code must satisfy. Mirrors `core/checker.py` exactly — if the
#: two drift, the static gate rejects everything the model writes.
SYSTEM_PROMPT = """\
You write small, single-purpose Python skills for an agent that acts on a user's behalf.

A skill composes *scoped primitives* the acting user already holds. You will be given the
exact primitives available. You may not use any other primitive, and you may not use any
other way of reaching the outside world.

Hard rules — code that breaks any of these is rejected before it runs:

1. Define exactly one entrypoint: `def run(scoped_client, ...)`. `scoped_client` is the
   first parameter. The remaining parameters must match the keyword arguments you are
   told the caller will pass — exactly those names. Do not rename them, do not add
   required parameters the caller has no value for.
2. The only way to act is `scoped_client.call("app.primitive", key=value, ...)`.
   - The primitive name must be a **literal string** — never built, joined or formatted.
   - Pass arguments as keywords only, never positionally.
   - You may not pass `identifier`. Identity is bound by the host and is not yours to set.
   - `scoped_client` has no other methods and may not be reassigned.
3. Imports are limited to: json, math, re, datetime, typing. Nothing else.
4. Forbidden entirely: eval, exec, compile, open, input, __import__, globals, locals,
   vars, getattr, setattr, delattr, dir, print, and any attribute starting with `__`.
5. Module level may contain only imports, constants, and definitions.

Design rules:

6. Read before you write, and **read back after you write**. Return the state you
   observed, not the state you intended — the caller reports your return value as fact.
7. Declare every primitive you call in `primitives_used`, and call every primitive you
   declare. An undeclared call is a hard failure.
8. `effects` is the strongest thing the skill does: `read`, `write`, or `destructive`.
9. `reversible` and `inverse` go together, always. If you set `reversible` true you must
   name the inverse skill in `inverse` — the two are one declaration and a manifest with
   one but not the other is rejected. Prefer making a write reversible and naming its
   inverse; only set `reversible` false when the effect genuinely cannot be undone.

Also write the test that earns the skill its trust: a single
`def check(result, calls):` that asserts against the returned value and the call log.
`calls` is a list of `{"primitive", "input", "ok", "error"}` dicts in execution order.
Assert observable outcomes, not implementation details. Use plain `assert` with a message.

The test module **may not import anything at all** — not even from the list above. It
receives `result` and `calls` as plain lists and dicts, so it needs nothing but `assert`
and the builtins. This is stricter than the skill itself, because the test runs on the
host rather than in the sandbox.
"""

#: Structured output schema — the response shape is guaranteed, so no parsing heuristics.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {
            "type": "string",
            "description": "snake_case Python identifier naming the skill",
        },
        "description": {"type": "string"},
        "primitives_used": {"type": "array", "items": {"type": "string"}},
        "effects": {"type": "string", "enum": ["read", "write", "destructive"]},
        "reversible": {"type": "boolean"},
        "inverse": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "source": {"type": "string", "description": "the skill module source"},
        "test_source": {"type": "string", "description": "module defining check(result, calls)"},
    },
    "required": [
        "skill", "description", "primitives_used", "effects",
        "reversible", "inverse", "source", "test_source",
    ],
    "additionalProperties": False,
}


@dataclass
class ForgeRequest:
    """Everything the generator is allowed to know."""

    intent: str
    speaker: str
    apps: list[str]                      # every service the speaker has connected
    tools: list[dict]                    # introspected: definition + effect, per primitive
    args: dict = field(default_factory=dict)   # the keywords run() will actually receive
    constraints: list[str] = field(default_factory=list)   # the admin's manifest ceilings
    feedback: str | None = None          # why the previous attempt failed
    previous_source: str | None = None
    attempt: int = 1

    def prompt(self) -> str:
        lines = [
            f"The user {self.speaker} said:",
            f"  {self.intent}",
        ]

        # The caller's argument names are not guessable, and a model that guesses wrong
        # burns a whole attempt on `TypeError: unexpected keyword argument`. Observed
        # doing exactly that on the first real generation run, so state them.
        if self.args:
            lines += [
                "",
                "`run()` will be called with exactly these keyword arguments, already "
                "extracted from what was said. Your signature must accept these names — "
                "no more, no fewer:",
            ]
            for key, value in sorted(self.args.items()):
                lines.append(f"  {key}={value!r}")

        # Named individually rather than as one blob: the model composes across services
        # now, and seeing "calendar and hubspot" is what tells it a single skill may book
        # the appointment and log it in one go.
        where = " and ".join(f"`{a}`" for a in self.apps) or "the connected services"
        lines += [
            "",
            f"Primitives {self.speaker} holds on {where} "
            f"— these are the only ones available to you. A skill may use primitives "
            f"from more than one of them:",
        ]
        for tool in self.tools:
            d = tool["definition"]
            lines.append(f"\n- {d['name']}  (effect: {tool.get('effect', 'unknown')})")
            lines.append(f"  {d['description']}")
            lines.append(f"  input_schema: {json.dumps(d['input_schema'])}")

        # Stated before the model writes anything, not discovered by being rejected.
        if self.constraints:
            lines += ["", "This deployment's administrator also requires:"]
            lines += [f"  - {c}" for c in self.constraints]

        if self.feedback:
            lines += [
                "",
                f"Attempt {self.attempt - 1} failed. This is why:",
                f"  {self.feedback}",
                "",
                "Fix that specific problem. The code you wrote was:",
                "```python",
                (self.previous_source or "").rstrip(),
                "```",
            ]
        return "\n".join(lines)


@dataclass
class Generation:
    source: str
    test_source: str
    manifest: dict
    raw: dict = field(default_factory=dict)


class CodeGenerator(Protocol):
    def generate(self, request: ForgeRequest) -> Generation: ...


def _to_generation(payload: dict, connected: list[str], speaker: str) -> Generation:
    """Split a model response into code, test and manifest.

    `apps`, `forged_by` and `trust` are set by us, never by the model — a skill cannot
    name its own apps, claim someone else's mark, or declare itself trustworthy.

    `apps` is *derived from the primitives declared*, not copied from what the speaker has
    connected. The distinction matters: a doctor with HubSpot, Calendar and Gmail
    connected who forges a calendar-only skill gets a manifest saying `["calendar"]`. Had
    it inherited all three, a policy denying Gmail would refuse a skill that never touches
    Gmail — a ceiling firing on a service the skill cannot reach.

    Letting the model influence this costs nothing, because it does not control the field
    that feeds it: `primitives_used` is reconciled against the code's actual calls by the
    static checker and against the speaker's grants by the forge's scope gate. A declared
    primitive the speaker lacks never survives to become a manifest.
    """
    apps = sorted({p.split(".", 1)[0] for p in payload["primitives_used"] if "." in p})
    return Generation(
        source=payload["source"],
        test_source=payload["test_source"],
        manifest={
            "skill": payload["skill"],
            "apps": apps or list(connected),
            "primitives_used": payload["primitives_used"],
            "effects": payload["effects"],
            "reversible": payload["reversible"],
            "inverse": payload["inverse"],
            "description": payload["description"],
            "forged_by": speaker,
            "trust": "quarantined",
        },
        raw=payload,
    )


class ClaudeGenerator:
    """Real generation via the Anthropic SDK.

    Streams because code generation is a long-output request, and uses structured
    outputs so the response shape is guaranteed rather than parsed hopefully.
    """

    def __init__(self, *, model: str = MODEL, max_tokens: int = 32000,
                 effort: str = "high", client=None) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic  # imported lazily so the scripted path needs no SDK

            self._client = anthropic.Anthropic()
        return self._client

    def generate(self, request: ForgeRequest) -> Generation:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
            messages=[{"role": "user", "content": request.prompt()}],
        ) as stream:
            message = stream.get_final_message()

        if message.stop_reason == "refusal":
            raise GenerationError(f"model declined to generate: {message.stop_details}")
        if message.stop_reason == "max_tokens":
            raise GenerationError(
                f"generation truncated at max_tokens={self.max_tokens}; raise it"
            )

        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            raise GenerationError("model returned no text block")

        return _to_generation(json.loads(text), request.apps, request.speaker)


class ScriptedGenerator:
    """Deterministic generator for tests and demos.

    Hands back canned payloads in order, recording each request it was given — so the
    Reflexion retry can be asserted on (attempt 2 must have received attempt 1's
    failure reason) without spending a token.
    """

    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = list(payloads)
        self.requests: list[ForgeRequest] = []

    def generate(self, request: ForgeRequest) -> Generation:
        self.requests.append(request)
        if not self._payloads:
            raise GenerationError("ScriptedGenerator ran out of payloads")
        return _to_generation(self._payloads.pop(0), request.apps, request.speaker)


class GenerationError(Exception):
    """The generator could not produce a candidate at all."""
