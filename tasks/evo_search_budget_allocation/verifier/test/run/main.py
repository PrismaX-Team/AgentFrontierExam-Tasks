#!/usr/bin/env python3
"""Final score: best fitness reached under a 512-iteration budget, macro-averaged over 41 cells.

For each scored cell (10 recorded branches) and each policy seed 1..30, the submitted
Policy(k=10, seed) spends 512 pulls. A pull advances the chosen branch by one iteration
and returns that branch's running-best fitness. A run's score is the best reward seen.
score(cell) = mean over seeds; primary = mean over cells (exact fractions).

The policy runs in separate processes (policy_host.py) that never receive hidden data:
this process keeps the trajectories and sends one reward per step. Every run is played by
two independent copies of the policy in lockstep; if their choices ever differ (for example
because they depend on object addresses), the policy is not deterministic and the submission
is invalid. policy.py is checked statically first. Any violation, exception, invalid arm or
timeout makes the submission invalid; it never crashes the scorer.
"""
from __future__ import annotations

import argparse
import ast
import gzip
import json
import math
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from science_innovation_exam.verify_context import VerifyContext


TASK_ID = "evo_search_budget_allocation"
HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data" / "cells.json.gz"
HOST = HERE / "policy_host.py"
PRIMARY = "mean_best_fitness"
K, BUDGET, SEEDS = 10, 512, range(1, 31)
MAX_POLICY_BYTES = 256 * 1024
RUN_SECONDS = 30.0         # wall-clock limit for one 512-pull run
TOTAL_SECONDS = 2400.0     # wall-clock limit for the whole evaluation
ALLOWED_MODULES = {"math", "random", "heapq", "bisect", "collections", "itertools",
                   "functools", "statistics", "copy"}
DENIED_NAMES = {"eval", "exec", "compile", "open", "input", "breakpoint", "help", "globals",
                "locals", "vars", "dir", "getattr", "setattr", "delattr", "type", "memoryview",
                "id", "hash",
                "__import__", "__builtins__", "__loader__", "__spec__"}
# getattr/eval by string: str.format, functools.update_wrapper/wraps (attribute names chosen by
# the caller), functools.singledispatch (evaluates annotations); SystemRandom is nondeterministic.
DENIED_ATTRS = {"format", "format_map", "mro", "SystemRandom", "modules", "sys", "os", "builtins",
                "subclasses", "update_wrapper", "wraps", "singledispatch", "singledispatchmethod"}
DENIED_ATTR_PREFIXES = ("f_", "gi_", "co_", "cr_", "ag_", "tb_")


class SubmissionError(ValueError):
    pass


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def check_policy_source(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise SubmissionError("policy.py is missing or is not a regular file")
        if path.stat().st_size > MAX_POLICY_BYTES:
            raise SubmissionError(f"policy.py exceeds {MAX_POLICY_BYTES} bytes")
        source = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SubmissionError("policy.py is not UTF-8 text") from None
    except OSError:
        raise SubmissionError("policy.py cannot be read") from None
    try:
        tree = ast.parse(source, filename="policy.py")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise SubmissionError("policy.py is not valid Python source") from None
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    raise SubmissionError(f"policy.py line {line}: import of {alias.name!r} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module not in ALLOWED_MODULES:
                raise SubmissionError(f"policy.py line {line}: import from {node.module!r} is not allowed")
            for alias in node.names:
                if alias.name == "*" or alias.name.startswith("_") or alias.name in DENIED_ATTRS:
                    raise SubmissionError(f"policy.py line {line}: importing {alias.name!r} is not allowed")
        elif isinstance(node, ast.Attribute):
            name = node.attr
            if ((name.startswith("_") and name != "__init__") or name in DENIED_ATTRS
                    or name.startswith(DENIED_ATTR_PREFIXES)):
                raise SubmissionError(f"policy.py line {line}: attribute {name!r} is not allowed")
        elif isinstance(node, ast.Name):
            if node.id in DENIED_NAMES or (node.id.startswith("__") and node.id != "__name__"):
                raise SubmissionError(f"policy.py line {line}: name {node.id!r} is not allowed")
    if not any((isinstance(n, ast.ClassDef) and n.name == "Policy")
               or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "Policy" for t in n.targets))
               for n in tree.body):
        raise SubmissionError("policy.py does not define a top-level class Policy")
    return source


class Host:
    """The policy process and a line reader with a deadline."""

    def __init__(self, policy_path: Path, jail: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(HOST), str(policy_path), str(jail)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=jail, env={"PYTHONHASHSEED": "0", "LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
            bufsize=0)
        self.buffer = b""

    def send(self, line: str) -> None:
        try:
            self.proc.stdin.write(line.encode() + b"\n")
        except (BrokenPipeError, OSError):
            raise SubmissionError("the policy process exited unexpectedly") from None

    def receive(self, deadline: float) -> str:
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SubmissionError("the policy exceeded its time limit")
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise SubmissionError("the policy process exited unexpectedly")
            self.buffer += chunk
            if len(self.buffer) > 1 << 20:
                raise SubmissionError("the policy process produced malformed output")
        line, _, self.buffer = self.buffer.partition(b"\n")
        text = line.decode("utf-8", "replace")
        if text.startswith("E "):
            raise SubmissionError(text[2:])
        return text

    def close(self) -> None:
        try:
            self.proc.stdin.write(b"Q\n")
            self.proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def choose(hosts: list[Host], deadline: float) -> int:
    arms = []
    for host in hosts:
        reply = host.receive(deadline).split()
        if len(reply) != 2 or reply[0] != "A" or not reply[1].isdigit() or not 0 <= int(reply[1]) < K:
            raise SubmissionError("the policy process produced malformed output")
        arms.append(int(reply[1]))
    if len(set(arms)) != 1:
        raise SubmissionError("policy.py is not deterministic: two identical runs chose different branches")
    return arms[0]


def run_once(hosts: list[Host], arms: list, seed: int, overall: float) -> tuple[float, Fraction | None]:
    deadline = min(time.monotonic() + RUN_SECONDS, overall)
    for host in hosts:
        host.send(f"R {K} {seed}")
    pulls = [0] * K
    best = None
    flops = Fraction(0)
    for step in range(BUDGET):
        arm = choose(hosts, deadline)
        generation = pulls[arm]
        pulls[arm] += 1
        reward = arms[arm]["fitness"][generation]
        best = reward if best is None else max(best, reward)
        cost = arms[arm]["flops"][generation] if arms[arm]["flops"] is not None else None
        flops = None if flops is None or cost is None else flops + Fraction(cost)
        for host in hosts:
            host.send(("F " if step == BUDGET - 1 else "W ") + repr(float(reward)))
    for host in hosts:
        if host.receive(deadline) != "D":
            raise SubmissionError("the policy process produced malformed output")
    return best, flops


def evaluate(policy_source: str, cells: list) -> dict[str, Fraction | float]:
    work = Path(tempfile.mkdtemp(prefix="policy_"))
    policy_path = work / "policy.py"
    policy_path.write_text(policy_source, encoding="utf-8")
    hosts = []
    for copy in ("a", "b"):
        jail = work / f"jail_{copy}"
        jail.mkdir()
        hosts.append(Host(policy_path, jail))
    overall = time.monotonic() + TOTAL_SECONDS
    try:
        cell_scores, flops_scores = {}, []
        for cell in cells:
            runs = [run_once(hosts, cell["arms"], seed, overall) for seed in SEEDS]
            cell_scores[cell["cell"]] = sum((Fraction(best) for best, _ in runs), Fraction(0)) / len(runs)
            if all(flops is not None for _, flops in runs) and cell["flops_complete"]:
                flops_scores.append(sum((flops for _, flops in runs), Fraction(0)) / len(runs))
    finally:
        for host in hosts:
            host.close()
        shutil.rmtree(work, ignore_errors=True)
    mean = sum(cell_scores.values(), Fraction(0)) / len(cell_scores)
    variance = sum(((s - mean) ** 2 for s in cell_scores.values()), Fraction(0)) / (len(cell_scores) - 1)
    values: dict[str, Fraction | float] = {PRIMARY: mean, "spread_across_cells": math.sqrt(variance)}
    if flops_scores:
        values["mean_cumulative_flops"] = sum(flops_scores, Fraction(0)) / len(flops_scores)
    values.update({f"cell:{name}": score for name, score in cell_scores.items()})
    return values


def load_cells() -> list:
    cells = json.loads(gzip.decompress(DATA.read_bytes()))
    for cell in cells:
        cell["flops_complete"] = all(a["flops"] is not None and None not in a["flops"] for a in cell["arms"])
        if len(cell["arms"]) != K or any(len(a["fitness"]) != BUDGET for a in cell["arms"]):
            raise RuntimeError(f"hidden data for {cell['cell']} is malformed")
    return cells


def result(values: dict | None, error: str | None) -> dict:
    valid = error is None
    units = {"mean_cumulative_flops": "FLOPs"}
    metrics = ([{"name": n, "value": float(v), "unit": units.get(n)} for n, v in values.items()]
               if valid else [{"name": PRIMARY, "value": 0.0, "unit": None}])
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "primary_metric": {"name": PRIMARY, "value": float(values[PRIMARY]) if valid else 0.0,
                           "direction": "maximize"},
        "metrics": metrics,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    del args.input  # the scorer uses only its own hidden cells
    cells = load_cells()
    try:
        source = check_policy_source(args.submission.resolve() / "policy.py")
        output = result(evaluate(source, cells), None)
    except SubmissionError as error:
        output = result(None, str(error))
    VerifyContext.current().log({"phase": "test", "valid": output["valid"],
                                 "primary_metric": output["primary_metric"], "error": output["error"]})
    atomic_json(args.result.resolve(), output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
