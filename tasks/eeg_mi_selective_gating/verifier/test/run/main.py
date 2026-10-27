#!/usr/bin/env python3
"""Final score: coverage at target risk r = 0.20, macro-averaged over episodes.

Per subject, with N test trials, A acted on and W wrong among those:
    score = A / N  if A > 0 and W <= 0.20 * A,  else 0.
Subject scores are averaged within an episode, then unweighted over episodes.
All arithmetic is exact (fractions); floats are produced only for the result.
The submitted gate.py is checked for presence and syntax but never executed.
"""
from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import os
from fractions import Fraction
from pathlib import Path

from science_innovation_exam.verify_context import VerifyContext


TASK_ID = "eeg_mi_selective_gating"
DATA = Path(__file__).resolve().parents[1] / "data"
PRIMARY_RISK = Fraction(1, 5)
RISK_GRID = [Fraction(k, 100) for k in (10, 15, 20, 25, 30, 35, 40, 45, 50)]
F_BETA_SQ = Fraction(1, 4)  # F0.5
HEADER = ["episode_id", "subject_id", "trial_id", "prediction"]
ABSTAIN = "-1"
MAX_DECISIONS_BYTES = 16 * 1024 * 1024
MAX_GATE_BYTES = 1024 * 1024


class SubmissionError(ValueError):
    pass


def risk_name(risk: Fraction) -> str:
    return f"coverage_at_risk_{float(risk):.2f}"


PRIMARY = risk_name(PRIMARY_RISK)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_answer_key() -> tuple[dict, dict]:
    episodes = json.loads((DATA / "episodes.json").read_text(encoding="utf-8"))
    labels: dict[tuple[str, str, str], int] = {}
    with open(DATA / "answer_key.csv", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        if next(reader) != HEADER[:3] + ["label"]:
            raise RuntimeError("answer key header is malformed")
        for episode_id, subject_id, trial_id, label in reader:
            labels[(episode_id, subject_id, trial_id)] = int(label)
    for episode_id, info in episodes.items():
        count = sum(1 for key in labels if key[0] == episode_id)
        if count != info["n_subjects"] * info["test_trials_per_subject"]:
            raise RuntimeError(f"answer key is incomplete for {episode_id}")
    if len(labels) != sum(i["n_subjects"] * i["test_trials_per_subject"] for i in episodes.values()):
        raise RuntimeError("answer key has rows outside the declared episodes")
    return episodes, labels


def read_regular(path: Path, limit: int) -> bytes:
    name = path.name
    try:
        if path.is_symlink() or not path.is_file():
            raise SubmissionError(f"{name} is missing or is not a regular file")
        if path.stat().st_size > limit:
            raise SubmissionError(f"{name} exceeds {limit} bytes")
        return path.read_bytes()
    except OSError:
        raise SubmissionError(f"{name} cannot be read") from None


def check_gate(submission: Path) -> None:
    raw = read_regular(submission / "gate.py", MAX_GATE_BYTES)
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SubmissionError("gate.py is not UTF-8 text") from None
    if not source.strip():
        raise SubmissionError("gate.py is empty")
    try:
        ast.parse(source, filename="gate.py")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # CPython 3.11 reports parser stack overflow on deep nesting as MemoryError.
        raise SubmissionError("gate.py is not valid Python source") from None


def read_decisions(submission: Path, episodes: dict, labels: dict) -> dict:
    raw = read_regular(submission / "decisions.csv", MAX_DECISIONS_BYTES)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SubmissionError("decisions.csv is not UTF-8 text") from None
    allowed = {
        episode_id: {ABSTAIN, *(str(k) for k in range(info["n_classes"]))}
        for episode_id, info in episodes.items()
    }
    decisions: dict[tuple[str, str, str], int] = {}
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        header = [cell.strip() for cell in next(reader, [])]
        if header != HEADER:
            raise SubmissionError("decisions.csv header must be " + ",".join(HEADER))
        for row in reader:
            line = reader.line_num
            if not any(cell.strip() for cell in row):
                continue
            if len(row) != len(HEADER):
                raise SubmissionError(f"decisions.csv line {line}: expected {len(HEADER)} fields")
            episode_id, subject_id, trial_id, prediction = (cell.strip() for cell in row)
            key = (episode_id, subject_id, trial_id)
            if key not in labels:
                raise SubmissionError(f"decisions.csv line {line}: unknown test trial")
            if key in decisions:
                raise SubmissionError(f"decisions.csv line {line}: duplicate test trial")
            if prediction not in allowed[episode_id]:
                raise SubmissionError(
                    f"decisions.csv line {line}: prediction must be -1 or a class index below "
                    f"{episodes[episode_id]['n_classes']}"
                )
            decisions[key] = int(prediction)
    except csv.Error as error:
        raise SubmissionError(f"decisions.csv is not valid CSV: {error}") from None
    if len(decisions) != len(labels):
        missing = min(key for key in labels if key not in decisions)
        raise SubmissionError(
            f"decisions.csv has {len(decisions)} of {len(labels)} test trials; first missing: "
            + ",".join(missing)
        )
    return decisions


def subject_counts(decisions: dict, labels: dict) -> dict:
    """-> {episode: {subject: [N, acted, wrong]}}"""
    counts: dict[str, dict[str, list[int]]] = {}
    for key, label in labels.items():
        row = counts.setdefault(key[0], {}).setdefault(key[1], [0, 0, 0])
        prediction = decisions[key]
        row[0] += 1
        if prediction != -1:
            row[1] += 1
            row[2] += prediction != label
    return counts


def coverage_at_risk(n: int, acted: int, wrong: int, risk: Fraction) -> Fraction:
    if acted == 0 or wrong > risk * acted:
        return Fraction(0)
    return Fraction(acted, n)


def f_beta(n: int, acted: int, wrong: int) -> Fraction:
    correct = acted - wrong
    if correct == 0:
        return Fraction(0)
    precision, recall = Fraction(correct, acted), Fraction(correct, n)
    return (1 + F_BETA_SQ) * precision * recall / (F_BETA_SQ * precision + recall)


def score(counts: dict) -> dict[str, Fraction]:
    per_subject = {
        **{risk_name(r): (lambda n, a, w, r=r: coverage_at_risk(n, a, w, r)) for r in RISK_GRID},
        "decision_rate": lambda n, a, w: Fraction(a, n),
        "decisive_accuracy": lambda n, a, w: Fraction(a - w, a) if a else Fraction(0),
        "f0.5": f_beta,
    }
    values: dict[str, Fraction] = {}
    per_episode: dict[str, Fraction] = {}
    for name, metric in per_subject.items():
        episode_means = {
            episode_id: sum((metric(*c) for c in subjects.values()), Fraction(0)) / len(subjects)
            for episode_id, subjects in counts.items()
        }
        values[name] = sum(episode_means.values(), Fraction(0)) / len(episode_means)
        if name == PRIMARY:
            per_episode = {f"{e}_{PRIMARY}": episode_means[e] for e in sorted(episode_means)}
    return {**values, **per_episode}


def valid_result(values: dict[str, Fraction]) -> dict:
    metrics = [
        {"name": name, "value": float(value), "unit": None if name == "f0.5" else "fraction"}
        for name, value in values.items()
    ]
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": True,
        "status": "valid",
        "primary_metric": {"name": PRIMARY, "value": float(values[PRIMARY]), "direction": "maximize"},
        "metrics": metrics,
        "error": None,
    }


def invalid_result(message: str) -> dict:
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": False,
        "status": "invalid",
        "primary_metric": {"name": PRIMARY, "value": 0.0, "direction": "maximize"},
        "metrics": [{"name": PRIMARY, "value": 0.0, "unit": "fraction"}],
        "error": message,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    del args.input  # scoring reads only the verifier's own copy of the episode layout
    episodes, labels = load_answer_key()
    submission = args.submission.resolve()
    try:
        decisions = read_decisions(submission, episodes, labels)
        check_gate(submission)
        result = valid_result(score(subject_counts(decisions, labels)))
    except SubmissionError as error:
        result = invalid_result(str(error))
    VerifyContext.current().log({
        "phase": "test",
        "valid": result["valid"],
        "primary_metric": result["primary_metric"],
        "error": result["error"],
    })
    atomic_json(args.result.resolve(), result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
