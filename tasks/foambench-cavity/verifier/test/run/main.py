#!/usr/bin/env python3
"""Final score: execution success rate (M_exec) over the 10 FoamBench Basic Cavity cases.

Each submitted case is written to a scratch directory and run unchanged with
OpenFOAM v10: its own Allrun when present, otherwise `runApplication blockMesh`
followed by `runApplication $(getApplication)`. Cases run one at a time. A case
scores 1 when all of the following hold, and 0 otherwise:

  1. the run exits with status 0 within the case's wall-clock limit;
  2. `application` in the submitted system/controlDict is the solver given for
     the case;
  3. at least one field file is written in a time directory after the initial
     time, and every value of every such field file is finite.

A missing or empty case scores 0. The submitted case may contain at most one
time directory (the initial conditions); results must come from the run.
The primary metric is the mean over the 10 cases, higher is better.

Diagnostics, never ranked: FoamBench's NMSE against the reference run
(nmse.py, a verbatim port), FoamBench's NMSE score and success ratio
(executed and NMSE < 0.1).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from science_innovation_exam.verify_context import VerifyContext

import foamfile
import runner
from submission import SubmissionError, load_submission

TASK_ID = "foambench-cavity"
HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
PRIMARY = "execution_success_rate"
NMSE_FAILURE = 9999.0
NMSE_TIMEOUT = 900


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def invalid_result(error: str) -> dict:
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": False,
        "status": "invalid",
        "primary_metric": {"name": PRIMARY, "value": 0.0, "direction": "maximize"},
        "metrics": [{"name": PRIMARY, "value": 0.0, "unit": None}],
        "error": error,
    }


def load_spec() -> dict[str, dict]:
    spec = json.loads((DATA / "spec.json").read_text(encoding="utf-8"))["cases"]
    if len(spec) != 10:
        raise RuntimeError("verifier spec must list the 10 FoamBench Basic Cavity cases")
    for case_id in spec:
        if not (DATA / "reference" / f"{case_id}.tar.gz").is_file():
            raise RuntimeError(f"reference data missing for {case_id}")
    return spec


def check_fields(case_dir: Path, submitted_times: dict[str, float]) -> str | None:
    """Return why criterion 3 fails, or None when it holds."""
    after = runner.time_directories(case_dir)
    if submitted_times:
        initial = next(iter(submitted_times.values()))
    elif after:
        initial = min(after.values())
    else:
        return "no time directory exists after the run"
    later = sorted((value, name) for name, value in after.items() if value > initial)
    fields = [
        path
        for _, name in later
        for path in sorted((case_dir / name).iterdir())
        if path.is_file() and not path.is_symlink() and foamfile.is_field(path)
    ]
    if not fields:
        return "no field file was written after the initial time"
    for path in fields:
        try:
            if not foamfile.field_is_finite(path):
                return f"non-finite value in {path.relative_to(case_dir)}"
        except (OSError, foamfile.FieldError) as error:
            return f"unreadable field {path.relative_to(case_dir)}: {error}"
    return None


def compute_nmse(case_id: str, case_dir: Path, work: Path) -> tuple[float, float]:
    """Run the FoamBench NMSE port in a child process; return (nmse, nmse score)."""
    reference = work / "reference" / case_id
    if reference.exists():
        shutil.rmtree(reference)
    reference.mkdir(parents=True)
    with tarfile.open(DATA / "reference" / f"{case_id}.tar.gz", "r:gz") as archive:
        archive.extractall(reference, filter="data")
    home = work / "nmse-home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": runner.BASE_PATH,
        "HOME": str(home),
        "MPLCONFIGDIR": str(home),
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        process = subprocess.run(
            [sys.executable, "-B", str(HERE / "nmse.py"), str(reference), str(case_dir)],
            cwd=work, env=env, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=NMSE_TIMEOUT, start_new_session=True,
        )
        lines = process.stdout.decode("utf-8", "replace").strip().splitlines()
        reply = json.loads(lines[-1]) if process.returncode == 0 and lines else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        reply = {}
    finally:
        shutil.rmtree(reference, ignore_errors=True)
    value, score = reply.get("nmse"), reply.get("nmse_score")
    if not isinstance(value, float) or not isinstance(score, float):
        return NMSE_FAILURE, 0.0
    return value, score


def evaluate_case(case_id: str, files: dict[str, str], spec: dict, work: Path,
                  context: VerifyContext) -> tuple[int, float, float]:
    """Return (executed 0/1, NMSE, NMSE score) for one case."""
    record = {"phase": "test", "case": case_id}
    if not files:
        context.log({**record, "executed": 0, "reason": "no files submitted"})
        return 0, NMSE_FAILURE, 0.0
    case_dir = work / "cases" / case_id
    home = work / "home" / case_id
    logs = work / "logs"
    logs.mkdir(exist_ok=True)
    runner.materialize(files, case_dir)
    submitted_times = runner.time_directories(case_dir)
    ran = False
    if len(submitted_times) > 1:
        reason = "the submitted case contains more than one time directory"
    else:
        application = runner.read_control_entry(
            case_dir, "application", home, logs / f"{case_id}.application.log"
        )
        if application != spec["solver"]:
            reason = f"controlDict application is {application!r}, expected {spec['solver']!r}"
        else:
            limit = spec["wall_clock_limit_seconds"]
            start = time.monotonic()
            code, timed_out = runner.run_case(case_dir, limit, home, logs / f"{case_id}.run.log")
            record.update(exit_code=code, timed_out=timed_out,
                          wall_seconds=round(time.monotonic() - start, 1))
            ran = True
            if timed_out:
                reason = f"run exceeded the {limit} s wall-clock limit"
            elif code != 0:
                reason = f"run exited with status {code}"
            else:
                reason = check_fields(case_dir, submitted_times)
    nmse, nmse_score = compute_nmse(case_id, case_dir, work) if ran else (NMSE_FAILURE, 0.0)
    executed = int(reason is None)
    context.log({**record, "executed": executed, "reason": reason, "nmse": nmse})
    shutil.rmtree(case_dir, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)
    return executed, nmse, nmse_score


def score(submission: Path, work: Path, context: VerifyContext) -> dict:
    spec = load_spec()
    case_ids = list(spec)
    cases = load_submission(submission / "cases.json", case_ids)
    runner.check_openfoam()
    executed, nmse, nmse_scores = {}, {}, {}
    for case_id in case_ids:
        executed[case_id], nmse[case_id], nmse_scores[case_id] = evaluate_case(
            case_id, cases[case_id], spec[case_id], work, context
        )
    count = len(case_ids)
    rate = sum(executed.values()) / count
    successes = sum(1 for c in case_ids if executed[c] == 1 and nmse_scores[c] == 1.0)
    metrics = [
        {"name": PRIMARY, "value": rate, "unit": None},
        {"name": "cases_executed", "value": sum(executed.values()), "unit": "cases"},
        {"name": "nmse_score", "value": sum(nmse_scores.values()) / count, "unit": None},
        {"name": "nmse_success_ratio", "value": successes / count, "unit": None},
    ]
    metrics += [{"name": f"executed/{c}", "value": executed[c], "unit": None} for c in case_ids]
    metrics += [{"name": f"nmse/{c}", "value": float(f"{nmse[c]:.10g}"), "unit": None} for c in case_ids]
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": True,
        "status": "valid",
        "primary_metric": {"name": PRIMARY, "value": rate, "direction": "maximize"},
        "metrics": metrics,
        "error": None,
    }


def scratch_directory(result: Path) -> Path:
    try:
        return Path(tempfile.mkdtemp(prefix="foambench-"))
    except OSError:
        return Path(tempfile.mkdtemp(prefix=".foambench-", dir=result.parent))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    del args.input  # the verifier uses its own copy of the case specification
    result_path = args.result.resolve()
    context = VerifyContext.current()
    work = scratch_directory(result_path)
    try:
        try:
            result = score(args.submission, work, context)
        except SubmissionError as error:
            result = invalid_result(str(error))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    context.log({"phase": "test", "valid": result["valid"],
                 "primary": result["primary_metric"]["value"], "error": result["error"]})
    atomic_json(result_path, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
