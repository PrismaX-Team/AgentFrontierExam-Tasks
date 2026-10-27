#!/usr/bin/env python3
"""Gating policy that produced decisions.csv.

Usage: python3 gate.py <data_dir> <decisions_csv>

Replace the body of decide() with your policy. This starter abstains on every
test trial, which is a valid submission that scores 0.
"""
import csv
import sys
from pathlib import Path


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def decide(episode, calibration, test):
    """Return one prediction per test row: a class index to act on, or -1 to abstain."""
    return [-1 for _ in test]


def main(data_dir, out_path):
    data_dir = Path(data_dir)
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["episode_id", "subject_id", "trial_id", "prediction"])
        for episode in read_rows(data_dir / "episodes.csv"):
            episode_id = episode["episode_id"]
            calibration = read_rows(data_dir / episode_id / "calibration.csv")
            test = read_rows(data_dir / episode_id / "test.csv")
            predictions = list(decide(episode, calibration, test))
            if len(predictions) != len(test):
                raise ValueError(f"{episode_id}: decide() returned {len(predictions)} predictions "
                                 f"for {len(test)} test rows")
            for row, prediction in zip(test, predictions):
                writer.writerow([episode_id, row["subject_id"], row["trial_id"], prediction])


if __name__ == "__main__":
    main(*sys.argv[1:3])
