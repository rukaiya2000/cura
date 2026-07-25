"""Reading `.env`, and reporting honestly about what is and isn't configured.

Python does not read `.env` on its own, so `load_env()` does it — deliberately without a
third-party dependency, and deliberately without overriding anything already exported.
A key already in the real environment wins, because that is where CI and production put
credentials and a checked-in file must not quietly shadow them.

    .venv/bin/python -m skillforge.config

prints which capabilities are unlocked and which are still stubbed. It reports whether a
key is *set*, never its value.

Nothing here is required. Every demo and all tests run with an empty `.env`, against the
in-memory adapter — that is the point of the fakes, and it stays true.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def load_env(path: Path | str | None = None, *, override: bool = False) -> dict[str, str]:
    """Load `KEY=value` lines into `os.environ`. Returns what was applied.

    Blank values are treated as absent — an unfilled template line must not shadow a real
    exported variable with an empty string, which would look configured and fail oddly.
    """
    path = Path(path) if path else ENV_PATH
    applied: dict[str, str] = {}
    if not path.is_file():
        return applied

    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if not key or not value:
            continue
        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        applied[key] = value

    return applied


def get(key: str, default: str | None = None) -> str | None:
    """An env var, treating blank as absent."""
    value = os.environ.get(key)
    return value if value else default


@dataclass(frozen=True)
class Capability:
    """One thing the project can do, and what it needs before it can."""

    name: str
    unlocks: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    fallback: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def missing(self) -> list[str]:
        return [k for k in self.required if not get(k)]

    @property
    def ready(self) -> bool:
        return not self.missing


#: Name fragments that mean a value must never be echoed, however convenient it would be.
#: Matched on the key name rather than the value, because a secret is identified by what
#: it is for, not by what it looks like.
SECRET_MARKERS = ("SECRET", "KEY", "TOKEN", "PASSWORD", "CREDENTIAL")


def is_secret(key: str) -> bool:
    return any(marker in key.upper() for marker in SECRET_MARKERS)


CAPABILITIES = (
    Capability(
        name="Code generation",
        unlocks="Claude writing skills for real instead of canned attempts "
                "(scripts/consult_demo.py --live)",
        required=("ANTHROPIC_API_KEY",),
        optional=("SKILLFORGE_EFFORT",),
        fallback="ScriptedGenerator — canned attempts, including the failed first one",
        notes=("ClaudeGenerator has never made a real call; its request shape is "
               "verified against a stub in tests/test_llm_wire.py, nothing more.",),
    ),
    Capability(
        name="Scoped execution",
        unlocks="real per-user execution through Scalekit — the governance layer",
        required=("SCALEKIT_CLIENT_ID", "SCALEKIT_CLIENT_SECRET",
                  "SCALEKIT_ENVIRONMENT_URL"),
        optional=("SKILLFORGE_CONNECTIONS",),
        fallback="FakeClinicActions — in-memory HubSpot, Calendar and Gmail",
        notes=("Connection names are case-sensitive; a mismatch returns an empty tool "
               "list with no error.",
               "A connected account can exist while carrying no token — check for "
               "PENDING_AUTH with scripts/check_connection.py.",),
    ),
    Capability(
        name="Acting as the clinician",
        unlocks="tool calls made under the doctor's own connected accounts",
        required=("SKILLFORGE_IDENTIFIER_FULL",),
        optional=("SKILLFORGE_IDENTIFIER_LIMITED", "SKILLFORGE_IDENTIFIER_PEER"),
        fallback="the in-memory clinic in adapters/fake_clinic.py",
        notes=("This must be the SAME string the connected accounts are filed under. "
               "Signing in yields it from the verified token, so the two cannot drift "
               "— but a token with no email claim falls back to an opaque sub, and "
               "then nothing matches.",),
    ),
    Capability(
        name="Live call",
        unlocks="joining a call with streaming transcript and speaker labels",
        required=("MEETSTREAM_API_KEY",),
        optional=("MEETSTREAM_BOT_NAME", "MEETSTREAM_WEBHOOK_SECRET"),
        fallback="the scripted transcript in scripts/call.py",
        notes=("The transcript webhook needs a public URL; MeetStream cannot reach "
               "127.0.0.1. Use `cloudflared tunnel --url http://127.0.0.1:8770`. "
               "Without MEETSTREAM_WEBHOOK_SECRET the server rejects every event.",),
    ),
)


def report() -> str:
    load_env()
    dim, bold, green, amber, grey, off = (
        "\033[2m", "\033[1m", "\033[32m", "\033[38;5;179m", "\033[38;5;244m", "\033[0m",
    )
    out = [f"\n{bold}Cura configuration{off}",
           f"{dim}reading {ENV_PATH}{off}",
           f"{grey}values are never printed — only whether a key is set{off}\n"]

    for cap in CAPABILITIES:
        mark = f"{green}ready{off}" if cap.ready else f"{amber}not configured{off}"
        out.append(f"  {bold}{cap.name}{off} — {mark}")
        out.append(f"    {dim}unlocks:{off} {cap.unlocks}")
        for key in cap.required:
            state = f"{green}set{off}" if get(key) else f"{amber}missing{off}"
            out.append(f"      {key:<34} {state}")
        for key in cap.optional:
            value = get(key)
            # Optional keys show their value because "SKILLFORGE_CONNECTIONS=gmail" is
            # exactly what you need to see. Secrets are the exception and must be named
            # rather than shown: this printed a live webhook secret in full, directly
            # under a header promising it never would.
            if not value:
                state = f"{grey}default{off}"
            elif is_secret(key):
                state = f"{grey}set{off}"
            else:
                state = f"{grey}{value}{off}"
            out.append(f"      {key:<34} {state} {dim}(optional){off}")
        if not cap.ready:
            out.append(f"    {dim}falling back to:{off} {cap.fallback}")
        for note in cap.notes:
            out.append(f"    {grey}note: {note}{off}")
        out.append("")

    ready = [c.name for c in CAPABILITIES if c.ready]
    out.append(f"{bold}{len(ready)}/{len(CAPABILITIES)} configured{off}"
               + (f" {dim}— {', '.join(ready)}{off}" if ready else ""))
    out.append(f"{grey}everything runs without any of these; the fakes are the "
               f"default path, not a degraded one{off}\n")
    return "\n".join(out)


if __name__ == "__main__":
    print(report())
