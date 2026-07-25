"""Host half of the sandbox — runs a forged skill in a process that holds no secrets.

The design goal is that the interesting security property is structural rather than
behavioural: the child process is launched isolated (`-I`), with a scrubbed environment,
in a throwaway working directory. It has no credentials to leak and no client to call.
Every effect it wants has to come back through this module, and this module is the only
place that knows *who* the acting user is.

So "the forge cannot be talked into exceeding the speaker's scope" is not a prompt
instruction. There is nothing in the child to talk to.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .checker import IMPORT_ALLOWLIST

_RUNNER = str(Path(__file__).with_name("_runner.py"))

#: Nothing inherited. No tokens, no API keys, no AWS/GCP metadata hints, no HOME.
_SCRUBBED_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}

DEFAULT_TIMEOUT = 10.0


@dataclass
class CallRecord:
    primitive: str
    input: dict
    ok: bool
    error: str | None = None
    result: object = None


@dataclass
class SandboxResult:
    ok: bool
    result: object = None
    error: str | None = None
    calls: list[CallRecord] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def primitives_called(self) -> set[str]:
        return {c.primitive for c in self.calls}


class ScopedCallDenied(Exception):
    """Raised by a host client to refuse a call. Surfaces inside the skill as Denied."""


def run_skill(
    source: str,
    *,
    client,
    kwargs: dict | None = None,
    allowed_primitives: set[str] | frozenset[str],
    timeout: float = DEFAULT_TIMEOUT,
) -> SandboxResult:
    """Execute `source`'s run() in an isolated child process.

    `client` is the host-side scoped client: it must expose
    ``call(primitive: str, **input) -> object`` and it is responsible for binding the
    acting user's identity. `allowed_primitives` is enforced here as well as statically,
    so a skill whose code changed after checking still cannot widen its own reach.
    """
    kwargs = kwargs or {}
    payload = json.dumps({
        "code": source,
        "kwargs": kwargs,
        "allowed_imports": sorted(IMPORT_ALLOWLIST),
    })

    started = time.monotonic()
    calls: list[CallRecord] = []

    with tempfile.TemporaryDirectory(prefix="forge-") as workdir:
        proc = subprocess.Popen(
            [sys.executable, "-I", _RUNNER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_SCRUBBED_ENV,
            cwd=workdir,
            text=True,
            bufsize=1,
            start_new_session=True,  # a skill cannot signal its way out
        )
        try:
            proc.stdin.write(payload + "\n")
            proc.stdin.flush()

            outcome = _pump(proc, client, allowed_primitives, calls, timeout)
        finally:
            _terminate(proc)

    outcome.calls = calls
    outcome.duration_s = time.monotonic() - started
    return outcome


def _pump(proc, client, allowed, calls, timeout) -> SandboxResult:
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return SandboxResult(ok=False, error=f"skill exceeded {timeout:g}s timeout")
        if not sel.select(remaining):
            return SandboxResult(ok=False, error=f"skill exceeded {timeout:g}s timeout")

        line = proc.stdout.readline()
        if not line:
            stderr = (proc.stderr.read() or "").strip()
            return SandboxResult(
                ok=False,
                error=f"skill process exited without a result{': ' + stderr if stderr else ''}",
            )

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return SandboxResult(ok=False, error=f"malformed message from skill: {line[:200]!r}")

        kind = msg.get("t")
        if kind == "call":
            _handle_call(proc, client, allowed, calls, msg)
        elif kind == "done":
            return SandboxResult(ok=True, result=msg.get("result"))
        elif kind == "raised":
            return SandboxResult(ok=False, error=msg.get("error", "skill raised"))
        else:
            return SandboxResult(ok=False, error=f"unknown message kind {kind!r}")


def _handle_call(proc, client, allowed, calls, msg) -> None:
    primitive = msg.get("primitive")
    payload = msg.get("input") or {}

    def reply(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    if primitive not in allowed:
        reason = f"{primitive!r} is outside this skill's declared primitives"
        calls.append(CallRecord(primitive, payload, ok=False, error=reason))
        reply({"t": "denied", "message": reason})
        return

    try:
        result = client.call(primitive, **payload)
    except ScopedCallDenied as e:
        calls.append(CallRecord(primitive, payload, ok=False, error=str(e)))
        reply({"t": "denied", "message": str(e)})
        return
    except Exception as e:  # noqa: BLE001 - a broken primitive is a skill-visible failure
        reason = f"{type(e).__name__}: {e}"
        calls.append(CallRecord(primitive, payload, ok=False, error=reason))
        reply({"t": "denied", "message": reason})
        return

    calls.append(CallRecord(primitive, payload, ok=True, result=result))
    reply({"t": "result", "data": result})


def _terminate(proc) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
