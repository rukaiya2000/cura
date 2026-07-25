"""The real Scalekit adapter — drop-in for `fake_scoped.BoundScopedClient`.

Named `scalekit_client` rather than `scalekit` on purpose: a module named after the
package it imports works, but reads like a bug every time someone opens it.

Three things here are adapter problems rather than API calls, and each is a place where a
wrong guess would be quiet rather than loud:

**Tool names.** Scalekit names its Linear tools whatever it names them; our manifests
require `app.action`. So names are normalised on the way in and translated back on the way
out, and the reverse map is authoritative — we never reconstruct a wire name by string
surgery at call time.

**Effect classes.** A manifest needs `read | write | destructive`, and policy gates on it,
but a tool definition may not say. It is inferred, and **the default when unsure is
`write`, never `read`** — misfiling a write as a read waves it past every gate that
matters, while misfiling a read as a write costs one unnecessary confirmation.

**Failure shapes.** The fake raises on an ungranted call. Whether the real API raises or
returns an error object is *not yet verified* against a live connected account, so both
are handled: an exception becomes `ScopedCallDenied`, and so does a result that looks like
an error. Being wrong in the other direction — treating a refusal as a success — would
turn every denial into a silent no-op.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..core.manifest import Effect
from ..core.sandbox import ScopedCallDenied

#: Substrings that mark a tool as irreversible. Checked first — a "delete" that also
#: says "update" is a delete.
DESTRUCTIVE_HINTS = ("delete", "remove", "destroy", "purge", "archive", "cancel",
                     "revoke", "drop", "wipe")

#: Substrings that mark a tool as read-only. Only trusted when no write hint is present.
READ_HINTS = ("get", "list", "search", "read", "fetch", "find", "query", "lookup",
              "describe", "show", "view", "retrieve", "count")

#: Substrings that mark a tool as mutating.
WRITE_HINTS = ("create", "update", "set", "add", "assign", "move", "link", "comment",
               "post", "send", "edit", "change", "close", "reopen", "merge", "upload")


class NoConnectedAccount(Exception):
    """This identifier has no connected account for the connector.

    Distinct from "connected but holds no tools" — the first means the person never
    completed the OAuth flow, the second means they did and were granted nothing. Telling
    a user to authorise when they already have is its own kind of unhelpful.
    """


def _words(text: str) -> set[str]:
    """Split into whole words, across snake_case, camelCase and punctuation.

    Whole words, not substrings: `"get" in "widget"` is true, and matching that way
    classified a `frobnicate_widget` tool as a read. Every hint here is short enough to
    hide inside an unrelated word — `add` in `address`, `set` in `asset`, `view` in
    `review` — and every one of those mistakes points the unsafe way.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return {w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w}


def classify_effect(name: str, description: str = "") -> Effect:
    """Infer an effect class from a tool's name, falling back to its description.

    Deliberately biased: unknown resolves to WRITE. The cost of over-classifying is a
    confirmation prompt nobody needed; the cost of under-classifying is a mutation that
    skipped the gate meant to catch it.
    """
    verb_words = _words(name.rsplit(".", 1)[-1])
    all_words = verb_words | _words(description)

    # Destructive first, and checked in the description too: "set a status, may remove
    # the previous one" is destructive however friendly its name is.
    if (verb_words | all_words) & set(DESTRUCTIVE_HINTS):
        return Effect.DESTRUCTIVE
    if verb_words & set(WRITE_HINTS):
        return Effect.WRITE
    if verb_words & set(READ_HINTS):
        return Effect.READ
    return Effect.WRITE


def normalize(tool_name: str, connection: str) -> str:
    """Scalekit's tool name → the `app.action` form manifests and the static gate expect."""
    cleaned = tool_name.strip()
    for separator in (".", "__"):
        if separator in cleaned:
            head, _, tail = cleaned.partition(separator)
            if head.lower() == connection.lower():
                return f"{connection}.{tail.lower()}"
    lowered = cleaned.lower()
    prefix = f"{connection.lower()}_"
    if lowered.startswith(prefix):
        lowered = lowered[len(prefix):]
    return f"{connection}.{lowered}"


def _looks_like_error(payload) -> str | None:
    """Spot an error-shaped result, for the case where the API returns rather than raises."""
    if isinstance(payload, dict):
        for key in ("error", "error_message", "errorMessage"):
            value = payload.get(key)
            if value:
                return str(value)
        if payload.get("success") is False or payload.get("ok") is False:
            return str(payload.get("message") or "call reported failure")
    return None


@dataclass
class ScalekitActions:
    """Thin wrapper over `scalekit_client.actions`, holding the name translation."""

    actions: object
    connection: str = "linear"
    _wire_names: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, connection: str | None = None) -> ScalekitActions:
        import scalekit  # imported lazily so the fake path needs no SDK

        missing = [k for k in ("SCALEKIT_ENVIRONMENT_URL", "SCALEKIT_CLIENT_ID",
                               "SCALEKIT_CLIENT_SECRET") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"missing credentials: {', '.join(missing)}")

        client = scalekit.client.ScalekitClient(
            env_url=os.environ["SCALEKIT_ENVIRONMENT_URL"],
            client_id=os.environ["SCALEKIT_CLIENT_ID"],
            client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
        )
        return cls(
            actions=client.actions,
            connection=connection or os.environ.get(
                "SKILLFORGE_LINEAR_CONNECTION_NAME", "linear"),
        )

    # --- introspection -----------------------------------------------------

    def list_tools(self, identifier: str) -> list[dict]:
        """The primitives this identifier holds, in our own shape.

        This is both the introspection step and the scope ceiling, so it is the single
        most important call in the adapter: whatever it omits cannot be forged.
        """
        try:
            response = self.actions.tools.list_scoped_tools(
                identifier=identifier,
                filter={"connection_names": [self.connection]},
                page_size=100,
            )
        except Exception as e:
            if type(e).__name__ == "ScalekitNotFoundException" or "not found" in str(e).lower():
                raise NoConnectedAccount(
                    f"{identifier} has no connected account for {self.connection!r} — "
                    "they need to complete the authorisation flow"
                ) from e
            raise

        raw = response[0] if isinstance(response, tuple) else response
        tools = []
        for entry in raw or []:
            definition = _attr(entry, "definition") or entry
            wire_name = _attr(definition, "name")
            if not wire_name:
                continue
            name = normalize(str(wire_name), self.connection)
            self._wire_names[name] = str(wire_name)
            description = _attr(definition, "description") or ""
            tools.append({
                "definition": {
                    "name": name,
                    "description": description,
                    "input_schema": _attr(definition, "input_schema")
                                    or _attr(definition, "inputSchema") or {},
                },
                "effect": classify_effect(name, description).value,
            })
        return tools

    # --- execution ---------------------------------------------------------

    def execute(self, *, primitive: str, identifier: str, tool_input: dict):
        wire_name = self._wire_names.get(primitive)
        if wire_name is None:
            # Never seen in a tool listing for anyone — so not something this identifier
            # can be assumed to hold. Refuse rather than trying a guessed wire name.
            raise ScopedCallDenied(
                f"{primitive!r} is not a known tool on {self.connection!r}"
            )
        try:
            result = self.actions.execute_tool(
                tool_name=wire_name, identifier=identifier, tool_input=tool_input,
            )
        except Exception as e:
            raise ScopedCallDenied(f"{type(e).__name__}: {e}") from e

        data = _attr(result, "data")
        data = data if data is not None else result
        problem = _looks_like_error(data)
        if problem:
            raise ScopedCallDenied(problem)
        return data


class ScalekitScopedClient:
    """Per-speaker client. Interface-identical to the fake, so nothing above it changes.

    `identifier` is set once at construction and is never a parameter of `call()`. That is
    the same guarantee the fake makes and the reason generated code cannot choose who it
    acts as — the adapter simply gives it no way to.
    """

    def __init__(self, actions: ScalekitActions, identifier: str) -> None:
        self._actions = actions
        self._identifier = identifier
        self._tools: list[dict] | None = None

    @property
    def identifier(self) -> str:
        return self._identifier

    def granted_tools(self) -> list[dict]:
        if self._tools is None:
            self._tools = self._actions.list_tools(self._identifier)
        return self._tools

    def granted_primitives(self) -> set[str]:
        return {t["definition"]["name"] for t in self.granted_tools()}

    def call(self, primitive: str, **tool_input):
        if "identifier" in tool_input:
            # The static gate already rejects this in generated code. Refusing here too
            # keeps the guarantee local: reading this class alone tells you identity
            # cannot be chosen by the caller, without having to trust a distant AST pass.
            raise TypeError(
                "a skill may not pass 'identifier' — identity is bound by the host"
            )
        return self._actions.execute(
            primitive=primitive, identifier=self._identifier, tool_input=tool_input,
        )


def _attr(obj, name):
    """Read a field whether the SDK hands back objects or dicts."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
