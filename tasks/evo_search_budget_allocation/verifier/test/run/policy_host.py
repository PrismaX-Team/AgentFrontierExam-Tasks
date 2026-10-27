#!/usr/bin/env python3
"""Child process that runs the submitted Policy. It never sees the hidden data.

The parent (main.py) streams one reward at a time over stdin and reads the chosen arm
from the protocol pipe. This process only ever holds the policy source and the rewards
it has been sent. Before running anything it applies resource limits. It then executes
policy.py with a restricted builtins table and an import hook that hands out read-only
proxies of a few standard-library modules. If running as root it also chroots into an
empty directory and drops privileges, so the filesystem is unreachable.

Protocol (text lines):
  parent -> child   "R <k> <seed>"      start a run: Policy(k, seed)
  child  -> parent  "A <arm>"           select() result
  parent -> child   "W <reward>"        update(arm, reward) with the arm just selected, then select()
  parent -> child   "F <reward>"        the last update of the run; the child answers "D"
  parent -> child   "Q"                 quit
  child  -> parent  "E <message>"       the policy failed; the child then exits
"""
from __future__ import annotations

import os
import sys


def fail(proto, message: str) -> None:
    proto.write("E " + " ".join(message.split())[:300] + "\n")
    proto.flush()
    os._exit(0)


def main() -> None:
    import builtins
    import resource
    import types

    proto = os.fdopen(os.dup(1), "w", buffering=1)
    sys.stdout = sys.stderr = open(os.devnull, "w")       # policy output is discarded

    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (3600, 3600))

    allowed = ("math", "random", "heapq", "bisect", "collections", "itertools",
               "functools", "statistics", "copy")
    banned_names = {"SystemRandom", "update_wrapper", "wraps", "singledispatch", "singledispatchmethod"}
    modules = {}
    for name in allowed:
        module = __import__(name)
        public = {key: getattr(module, key) for key in dir(module)
                  if not key.startswith("_") and key not in banned_names
                  and not isinstance(getattr(module, key), types.ModuleType)}
        modules[name] = types.SimpleNamespace(**public)
    rng_module = __import__("random")

    source = open(sys.argv[1], encoding="utf-8").read()
    code = compile(source, "policy.py", "exec")

    if os.geteuid() == 0:                       # strongest isolation when available
        try:
            os.chroot(sys.argv[2])
            os.chdir("/")
            os.setgid(65534)
            os.setuid(65534)
        except OSError:
            pass

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or name not in modules:
            raise ImportError(f"import of {name!r} is not allowed")
        proxy = modules[name]
        for item in fromlist or ():
            if not hasattr(proxy, item):
                raise ImportError(f"cannot import {item!r} from {name!r}")
        return proxy

    safe = {name: getattr(builtins, name) for name in (
        "abs", "all", "any", "bool", "callable", "chr", "classmethod", "complex", "dict", "divmod",
        "enumerate", "filter", "float", "frozenset", "hasattr", "int", "isinstance",
        "issubclass", "iter", "len", "list", "map", "max", "min", "next", "object", "ord", "pow",
        "property", "range", "repr", "reversed", "round", "set", "slice", "sorted", "staticmethod",
        "str", "sum", "super", "tuple", "zip",
        "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError",
        "ArithmeticError", "OverflowError", "RuntimeError", "StopIteration", "AssertionError",
        "NotImplementedError", "LookupError", "True", "False", "None", "NotImplemented",
        "__build_class__")}
    safe["print"] = lambda *args, **kwargs: None
    safe["__import__"] = guarded_import

    namespace = {"__builtins__": safe, "__name__": "policy"}
    try:
        exec(code, namespace)
    except BaseException as error:  # noqa: BLE001 - any failure is the submission's
        fail(proto, f"policy.py failed to load: {type(error).__name__}: {error}")
    policy_class = namespace.get("Policy")
    if not isinstance(policy_class, type):
        fail(proto, "policy.py does not define a class named Policy")

    stdin = sys.stdin
    policy = arm = None
    k = 0
    for line in stdin:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "Q":
            break
        try:
            if parts[0] == "R":
                k, seed = int(parts[1]), int(parts[2])
                rng_module.seed(seed)            # module-level random is seeded per run
                policy = policy_class(k, seed)
                arm = None
            elif parts[0] in ("W", "F"):
                policy.update(arm, float(parts[1]))
                if parts[0] == "F":
                    proto.write("D\n")
                    continue
            else:
                fail(proto, "protocol error")
            if parts[0] in ("R", "W"):
                choice = policy.select()
                if type(choice) is not int or not 0 <= choice < k:
                    fail(proto, f"select() must return an int in [0, {k}), got {choice!r:.40}")
                arm = choice
                proto.write(f"A {arm}\n")
        except BaseException as error:  # noqa: BLE001
            fail(proto, f"policy raised {type(error).__name__}: {error}")
    proto.flush()


if __name__ == "__main__":
    main()
