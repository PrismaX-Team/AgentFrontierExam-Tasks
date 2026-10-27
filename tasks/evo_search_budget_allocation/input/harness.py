#!/usr/bin/env python3
"""Replay harness: run a branch-allocation policy over the visible example cell.

This is the scorer's protocol without its process isolation. For one cell (10 recorded
branches) and each seed, Policy(k=10, seed) spends a budget of 512 pulls. A pull advances
the chosen branch by one iteration; the reward is that branch's running-best fitness after
the pull. A run's score is the best reward seen. The cell score is the mean over seeds
1..30. The real score is the mean of that cell score over the 41 hidden cells.

    python3 harness.py                       # the five published allocators
    python3 harness.py --policy policy.py    # your policy
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import random
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
K, BUDGET, SEEDS = 10, 512, range(1, 31)


def load_example() -> list[dict]:
    """The example cell's 10 branches in seed order (40..49): fitness and per-iteration FLOPs."""
    rows = [json.loads(line) for line in gzip.open(HERE / "example_cell.jsonl.gz", "rt")]
    rows.sort(key=lambda r: r["seed"])
    arms = []
    for r in rows:
        flops = next((r[key] for key in ("flops_exact", "flops_ub")
                      if r.get(key) is not None and any(v is not None for v in r[key])), None)
        arms.append({"fitness": r["fitness"], "flops": flops})
    return arms


def run(policy_class, arms: list[dict], seed: int) -> tuple[float, float | None]:
    """One run: -> (best reward seen, cumulative FLOPs or None if any bought iteration lacks FLOPs)."""
    random.seed(seed)                    # the scorer seeds the module-level generator per run
    policy = policy_class(K, seed)
    pulls = [0] * K
    best, flops = None, 0.0
    for _ in range(BUDGET):
        arm = policy.select()
        if type(arm) is not int or not 0 <= arm < K:
            raise ValueError(f"select() must return an int in [0, {K}), got {arm!r}")
        reward = arms[arm]["fitness"][pulls[arm]]
        cost = arms[arm]["flops"][pulls[arm]] if arms[arm]["flops"] is not None else None
        pulls[arm] += 1
        best = reward if best is None else max(best, reward)
        flops = None if flops is None or cost is None else flops + cost
        policy.update(arm, reward)
    return best, flops


def score(policy_class, arms: list[dict]) -> float:
    return statistics.fmean(run(policy_class, arms, seed)[0] for seed in SEEDS)


def load_policy(path: Path):
    spec = importlib.util.spec_from_file_location("policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Policy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, help="a policy.py to score on the example cell")
    args = parser.parse_args()
    arms = load_example()
    if args.policy:
        print(f"{args.policy}: {score(load_policy(args.policy), arms):.6f}")
        return
    import allocators
    for name, policy_class in allocators.ALLOCATORS.items():
        print(f"{name:12s} {score(policy_class, arms):.6f}")


if __name__ == "__main__":
    main()
