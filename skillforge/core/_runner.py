"""Child-process half of the sandbox. Never imported by the host — it is executed.

This process is where forged code actually runs, and its defining property is what it
*lacks*: no credentials, no environment, no network client, no filesystem helpers. The
only way it can affect the world is to ask its parent to make a scoped call on its
behalf, and the parent is the one holding the identity.

Kept dependency-free and self-contained on purpose: it is launched with `-I`, so it can
import nothing from the project. Configuration arrives in the payload.

Protocol — newline-delimited JSON, host ⇄ child:
    host  → child   {"code":…, "kwargs":{…}, "allowed_imports":[…]}   (once, first line)
    child → host    {"t":"call","primitive":"linear.get_issue","input":{…}}
    host  → child   {"t":"result","data":…}  |  {"t":"denied","message":…}
    child → host    {"t":"done","result":…}  |  {"t":"raised","error":…}
"""

import builtins
import json
import os
import sys

# fd 1 is the protocol channel; move it somewhere the skill cannot reach, then point
# the real stdout at /dev/null so stray writes can never corrupt the stream.
_proto_fd = os.dup(1)
os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
_out = os.fdopen(_proto_fd, "w")
_in = sys.stdin

_SAFE_BUILTINS = (
    "abs all any bool bytes callable chr dict divmod enumerate filter float format "
    "frozenset hash hex id int isinstance issubclass iter len list map max min next oct "
    "ord pow range repr reversed round set slice sorted str sum tuple type zip "
    "Exception ValueError TypeError KeyError IndexError AttributeError RuntimeError "
    "StopIteration ZeroDivisionError ArithmeticError LookupError NotImplementedError "
    "True False None __build_class__"
).split()


class Denied(Exception):
    """The host refused a scoped call. Not catchable into a bypass — there is no bypass."""


def _send(obj):
    _out.write(json.dumps(obj) + "\n")
    _out.flush()


def _recv():
    line = _in.readline()
    if not line:
        raise SystemExit(0)  # host went away
    return json.loads(line)


class ScopedClient:
    """The skill's entire window onto the world.

    Note what is absent: no token, no base URL, no session, and no way to name the
    acting user. The host binds identity; the skill can only choose a primitive.
    """

    def call(self, primitive, **kwargs):
        _send({"t": "call", "primitive": primitive, "input": kwargs})
        msg = _recv()
        if msg.get("t") == "result":
            return msg.get("data")
        raise Denied(msg.get("message", "denied"))


def _guarded_import(allowed):
    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            raise ImportError("relative imports are not available in a skill")
        if name.split(".")[0] not in allowed:
            raise ImportError(f"import of {name!r} is not allowed in a skill")
        return real_import(name, globals, locals, fromlist, level)

    return _import


def main():
    payload = _recv()
    allowed = set(payload.get("allowed_imports", ()))

    safe = {n: getattr(builtins, n) for n in _SAFE_BUILTINS if hasattr(builtins, n)}
    safe["__import__"] = _guarded_import(allowed)

    namespace = {"__builtins__": safe, "__name__": "skill"}

    try:
        exec(compile(payload["code"], "<skill>", "exec"), namespace)
        entry = namespace.get("run")
        if entry is None:
            raise RuntimeError("skill defines no run() entrypoint")
        result = entry(ScopedClient(), **payload.get("kwargs", {}))
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            result = repr(result)
        _send({"t": "done", "result": result})
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 - the host decides what a failure means
        _send({"t": "raised", "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
