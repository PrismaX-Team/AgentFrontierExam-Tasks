# Verifier API

`contract_revision`: 2026-09-17

The framework starts each entry as a temporary offline process. The agent
never sees this process. The working directory is `/verifier`.

Both `validation/run/main.py` and `test/run/main.py` must exist.

Validation exists so a task that needs a mid-run check can answer that
check. Follow the approved design. Do not add a protocol, and do not write
agent-facing advice about calling or skipping validation.

The template program returns
`{"status":"error","error":"this task has no mid-run check"}`.
Keep that string **only** when the approved design has no mid-run check.

If the design includes mid-run checks but the request format is not
frozen, keep those checks in `instruction.json` and change the program
error to `{"status":"error","error":"mid-run check request schema is not frozen"}`.
Do not leave the template “no mid-run check” string. Do not invent the
schema.

A validation reply is never the official raw score and must not rewrite
it. The two programs may share helpers or not; that is not a design
recommendation.

## Validation

```text
python3 -B /verifier/validation/run/main.py \
  --input /input \
  --request /request/request.json \
  --response /output/response.json
```

Mounts: `/input` (read-only), `/verifier` (read-only), `/verify-context`
(read-write), `/output` (read-write), `/request` (read-only).

Write any JSON object to `--response` and exit 0. A scientific “no” is still
a successful action. Non-zero exit, timeout, or illegal JSON ends the
experiment as `validation_failed` and rolls `VerifyContext` back.

The request envelope the agent sends is defined by the framework. The
scientific payload inside it is defined by the task. Typical pattern:

```python
request = json.loads(Path(args.request).read_text(encoding="utf-8"))
context = VerifyContext.current()
# reject without writing budget-consuming state
# accept: write measurements into context, then write response
```

Rejected requests must not consume the scientific budget (rounds, queries,
or other task-defined limits).

## Test

```text
python3 -B /verifier/test/run/main.py \
  --input /input \
  --submission /submission \
  --result /output/result.json
```

Mounts: `/input`, `/verifier`, `/verify-context`, `/output`, `/submission`
(all scoring inputs read-only except context and output).

`--result` must be an object that matches `verifier_result.schema.json`:

```json
{
  "schema_version": "1.0",
  "task_id": "untitled_task",
  "valid": false,
  "status": "invalid",
  "primary_metric": {"name": "primary_score", "value": 0.0, "direction": "maximize"},
  "metrics": [{"name": "primary_score", "value": 0.0, "unit": null}],
  "error": "scoring algorithm is not implemented"
}
```

Hard rules the framework also checks:

- `task_id` equals `meta.json.id`
- `valid` is true if and only if `status` is `"valid"`
- `error` is a string when invalid, and `null` when valid
- every metric value is finite
- metric names are unique
- `primary_metric.name` and `primary_metric.value` also appear in `metrics`

Exit 0 after writing the object. A scientifically invalid submission is
`valid: false`, not a crash. A crash becomes `scorer_failed`.

`primary_metric.direction` is `"maximize"` or `"minimize"` and must match
the science, not a leftover template default.

## Shared state

```python
from science_innovation_exam.verify_context import VerifyContext

context = VerifyContext.current()
context["key"] = value          # JSON-serializable, finite numbers
value = context.get("key")
context.log({"phase": "test", "note": "operator diagnostic"})
```

`SIE_VERIFY_CONTEXT` is set by the framework. Task code must not choose the
directory. Logs go to the operator’s run folder; they are not a score and
are not shown to the agent.

Test should treat `VerifyContext` as the trusted history of accepted
validation. Do not replay agent-editable CSVs as if they were that history.

## Environment

Both processes receive `PYTHONDONTWRITEBYTECODE=1`, `PYTHONHASHSEED=0`,
`LANG=C.UTF-8`. They have no network. Import only what the task image
already contains. The framework injects the small `VerifyContext` SDK.

## What to test (outside the package)

Put tests in `build/tests/`. Compare the programs to the contract and to
the contributor’s scoring tables, not to the implementation you just wrote.

Minimum useful cases:

1. Known fixture → expected raw primary value.
2. Missing / extra / malformed artifact → `valid: false`, deterministic error.
3. Same frozen inputs twice → byte-identical or value-identical result.
4. Illegal validation request → error response, context budget unchanged.
5. Hidden labels absent from `input/` and from validation responses except
   the measurements the task is allowed to return.
6. `task_id` in the result equals `meta.json.id`.
