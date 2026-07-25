"""The static gate — what a forged skill is *allowed to be*, checked before it runs.

This is where safety actually lives. The sandbox (sandbox.py) removes the credentials
and the network; this module removes the ways generated code could reach around them.

The central rule: the only way out of a skill is `scoped_client.call("app.primitive", ...)`
with a **literal** primitive name. Literal names are what make the manifest checkable —
if the primitive could be computed at runtime, no static reconciliation is possible and
the manifest becomes a suggestion.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from .manifest import CapabilityManifest

#: Modules a skill may import. Deliberately tiny, and deliberately offline.
IMPORT_ALLOWLIST = frozenset({"json", "math", "re", "datetime", "typing"})

#: Builtins that would let code escape the restricted namespace or reach the host.
BANNED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "__import__", "globals", "locals",
    "vars", "getattr", "setattr", "delattr", "dir", "breakpoint", "help",
    "memoryview", "exit", "quit", "print",
})

#: The sole egress symbol injected into a skill's namespace.
CLIENT_NAME = "scoped_client"

#: The only method the injected client exposes to generated code.
CLIENT_METHOD = "call"

#: Every skill is a single entrypoint with this name.
ENTRYPOINT = "run"


@dataclass
class CheckResult:
    primitives_called: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.res = CheckResult()

    def _err(self, node: ast.AST, msg: str) -> None:
        self.res.errors.append(f"line {getattr(node, 'lineno', '?')}: {msg}")

    # --- imports -----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root not in IMPORT_ALLOWLIST:
                self._err(node, f"import of {alias.name!r} is not on the allowlist")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if node.level:
            self._err(node, "relative imports are not allowed in a skill")
        elif root not in IMPORT_ALLOWLIST:
            self._err(node, f"import from {node.module!r} is not on the allowlist")
        self.generic_visit(node)

    # --- name and attribute access ----------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in BANNED_NAMES:
            self._err(node, f"use of {node.id!r} is not allowed in a skill")
        if node.id.startswith("__"):
            self._err(node, f"dunder name {node.id!r} is not allowed")
        # Rebinding the client would let code swap out its own egress.
        if node.id == CLIENT_NAME and isinstance(node.ctx, (ast.Store, ast.Del)):
            self._err(node, f"{CLIENT_NAME!r} may not be reassigned")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__"):
            # __globals__, __class__, __subclasses__ … the usual escape ladder.
            self._err(node, f"attribute {node.attr!r} is not allowed")
        if isinstance(node.value, ast.Name) and node.value.id == CLIENT_NAME:
            if node.attr != CLIENT_METHOD:
                self._err(
                    node,
                    f"{CLIENT_NAME}.{node.attr} is not available; "
                    f"a skill may only use {CLIENT_NAME}.{CLIENT_METHOD}()",
                )
        self.generic_visit(node)

    # --- calls -------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_client_call = (
            isinstance(func, ast.Attribute)
            and func.attr == CLIENT_METHOD
            and isinstance(func.value, ast.Name)
            and func.value.id == CLIENT_NAME
        )
        if is_client_call:
            if not node.args or not isinstance(node.args[0], ast.Constant) \
                    or not isinstance(node.args[0].value, str):
                self._err(
                    node,
                    "the primitive name must be a literal string so it can be "
                    "reconciled against the manifest",
                )
            else:
                self.res.primitives_called.add(node.args[0].value)
            if len(node.args) > 1:
                self._err(node, "pass primitive arguments as keywords, not positionally")
            # `identifier` is bound by the host at injection time and is not the
            # skill's to choose — that is the whole per-user guarantee.
            if any(kw.arg == "identifier" for kw in node.keywords):
                self._err(node, "a skill may not set 'identifier'; identity is bound by the host")
        self.generic_visit(node)


def check_code(source: str) -> CheckResult:
    """Static-check a skill's source. Never executes anything."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return CheckResult(errors=[f"line {e.lineno}: syntax error: {e.msg}"])

    v = _Visitor()
    v.visit(tree)
    res = v.res

    entry = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == ENTRYPOINT),
        None,
    )
    if entry is None:
        res.errors.append(f"a skill must define a {ENTRYPOINT}() entrypoint")
    else:
        params = [a.arg for a in entry.args.args]
        if not params or params[0] != CLIENT_NAME:
            res.errors.append(
                f"{ENTRYPOINT}() must take {CLIENT_NAME} as its first parameter"
            )

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.Import, ast.ImportFrom, ast.Assign,
                                ast.AnnAssign, ast.Expr, ast.ClassDef)):
            res.errors.append(
                f"line {node.lineno}: only imports, assignments and definitions may run "
                "at a skill's module level"
            )

    return res


def entrypoint_params(source: str) -> list[str]:
    """The skill's argument names, excluding the injected client.

    The router uses this to notice a request that named a skill but not everything the
    skill needs — better to ask which issue than to act on a guess.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == ENTRYPOINT:
            params = [a.arg for a in node.args.args]
            return [p for p in params if p != CLIENT_NAME]
    return []


def required_params(source: str) -> list[str]:
    """Entrypoint params with no default — the ones a caller must supply."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == ENTRYPOINT:
            args = [a.arg for a in node.args.args if a.arg != CLIENT_NAME]
            n_defaults = len(node.args.defaults)
            return args[: len(args) - n_defaults] if n_defaults else args
    return []


def reconcile(source: str, manifest: CapabilityManifest) -> CheckResult:
    """Static check, plus: does the code touch anything it did not declare?

    Undeclared reach is a hard error — it is the difference between a manifest that
    describes the skill and a manifest that merely accompanies it.
    """
    res = check_code(source)
    declared = set(manifest.primitives_used)

    for extra in sorted(res.primitives_called - declared):
        res.errors.append(
            f"code calls {extra!r} which is not declared in the manifest"
        )
    for unused in sorted(declared - res.primitives_called):
        res.warnings.append(f"manifest declares {unused!r} but the code never calls it")

    return res
