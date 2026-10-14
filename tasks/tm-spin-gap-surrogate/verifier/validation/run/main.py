"""Return the task's fixed response to a mid-run check request."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
for name in ("input", "request", "response"):
    parser.add_argument("--" + name, type=Path, required=True)
args = parser.parse_args()
args.response.parent.mkdir(parents=True, exist_ok=True)
args.response.write_text(
    json.dumps(
        {"status": "error", "error": "this task has no mid-run check"}, sort_keys=True
    ) + "\n",
    encoding="utf-8",
)
