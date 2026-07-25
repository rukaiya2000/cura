"""Tempering — the gate between "code exists" and "this skill may act".

A freshly forged skill is quarantined. To temper it we:

  1. reconcile its code against its manifest (does it reach beyond what it declared?)
  2. execute it in the sandbox against a **simulator**, not production
  3. run its generated test against the observed result and the real call log

Only all three passing moves it to TEMPERED. Failure returns the reason, which is what
the Reflexion retry loop feeds back into the next generation attempt.

Note the simulator: tempering deliberately does not use dry-run-against-production,
because a dry run cannot tell you whether the skill produced the *right* end state. A
throwaway simulator can, so the test asserts on real mutations that no one has to undo.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from typing import Callable

from .checker import reconcile
from .library import Skill
from .sandbox import DEFAULT_TIMEOUT, SandboxResult, run_skill

#: Builtins a generated test may use. Tests are generated code too, so they get a
#: restricted namespace. They run in-process because they only touch the returned data,
#: never a client — but keep the surface small anyway.
_TEST_BUILTINS = (
    "abs all any bool dict enumerate filter float int isinstance len list map max min "
    "next range repr reversed round set sorted str sum tuple type zip "
    "AssertionError Exception KeyError IndexError TypeError ValueError True False None"
).split()


@dataclass
class TemperResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sandbox: SandboxResult | None = None

    @property
    def reason(self) -> str:
        """A single string suitable for feeding back into regeneration."""
        return "; ".join(self.errors)


def temper(
    skill: Skill,
    *,
    client_factory: Callable[[], object],
    kwargs: dict,
    timeout: float = DEFAULT_TIMEOUT,
) -> TemperResult:
    """Try to promote `skill` out of quarantine. Does not write to the library."""
    declared = set(skill.manifest.primitives_used)

    static = reconcile(skill.source, skill.manifest)
    if not static.ok:
        return TemperResult(ok=False, errors=static.errors, warnings=static.warnings)

    sandboxed = run_skill(
        skill.source,
        client=client_factory(),
        kwargs=kwargs,
        allowed_primitives=declared,
        timeout=timeout,
    )
    if not sandboxed.ok:
        return TemperResult(
            ok=False,
            errors=[f"dry run failed: {sandboxed.error}"],
            warnings=static.warnings,
            sandbox=sandboxed,
        )

    if not skill.test_source.strip():
        return TemperResult(
            ok=False,
            errors=["skill has no generated test; a skill without a test cannot be tempered"],
            warnings=static.warnings,
            sandbox=sandboxed,
        )

    call_log = [
        {"primitive": c.primitive, "input": c.input, "ok": c.ok, "error": c.error}
        for c in sandboxed.calls
    ]
    failure = _run_check(skill.test_source, sandboxed.result, call_log)
    if failure:
        return TemperResult(ok=False, errors=[f"test failed: {failure}"],
                            warnings=static.warnings, sandbox=sandboxed)

    return TemperResult(ok=True, warnings=static.warnings, sandbox=sandboxed)


def _run_check(test_source: str, result, calls) -> str | None:
    """Execute a generated test's check(). Returns None on pass, else the failure reason."""
    safe = {n: getattr(builtins, n) for n in _TEST_BUILTINS if hasattr(builtins, n)}
    namespace = {"__builtins__": safe, "__name__": "skill_test"}
    try:
        exec(compile(test_source, "<skill-test>", "exec"), namespace)
    except BaseException as e:  # noqa: BLE001
        return f"test module did not load: {type(e).__name__}: {e}"

    check = namespace.get("check")
    if check is None:
        return "test defines no check(result, calls)"

    try:
        check(result, calls)
    except AssertionError as e:
        return str(e) or "assertion failed"
    except BaseException as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None
