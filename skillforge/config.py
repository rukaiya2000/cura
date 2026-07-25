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


CAPABILITIES = (
    Capability(
        name="Code generation",
        unlocks="real generation instead of canned attempts "
                "(scripts/forge_demo.py --live)",
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
        optional=("SKILLFORGE_LINEAR_CONNECTION_NAME",),
        fallback="FakeScalekitActions — in-memory Linear with invented per-user grants",
        notes=("The connection name is case-sensitive; a mismatch returns an empty "
               "tool list with no error.",),
    ),
    Capability(
        name="The two-mouths beat",
        unlocks="the same sentence from two speakers producing opposite outcomes",
        required=("SKILLFORGE_IDENTIFIER_FULL", "SKILLFORGE_IDENTIFIER_LIMITED"),
        optional=("SKILLFORGE_IDENTIFIER_PEER",),
        fallback="priya@co / sam@co in the fake adapter",
        notes=("These two must have genuinely different Linear permissions. Two "
               "accounts with the same grants make the beat silently meaningless.",),
    ),
    Capability(
        name="Live call",
        unlocks="joining a call with streaming transcript and speaker labels",
        required=("MEETSTREAM_API_KEY",),
        optional=("MEETSTREAM_BOT_NAME",),
        fallback="the scripted transcript in scripts/call.py",
        notes=("Nothing is built against MeetStream yet — this key unlocks work that "
               "does not exist, so it is the least urgent of these.",),
    ),
)


def report() -> str:
    load_env()
    dim, bold, green, amber, grey, off = (
        "\033[2m", "\033[1m", "\033[32m", "\033[38;5;179m", "\033[38;5;244m", "\033[0m",
    )
    out = [f"\n{bold}SkillForge configuration{off}",
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
            state = f"{grey}{value}{off}" if value else f"{grey}default{off}"
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
