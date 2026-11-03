# Formal task contract

`contract_revision`: 2026-09-17

In this repository, the Chinese
[`任务包合同.md`](../../../任务包合同.md) is the authority for the
five-part package. This file is the English working copy bundled with the
skill.

The formal package is the smallest runnable contract extracted from construction
materials. One approved proposal is one package. Agent-visible package text
is English. Baselines (a public starting method the framework can re-run),
build scripts, tests, and source evidence stay **outside** the package.
`meta.json.task_sources` lists the construction prototype and the data that
entered the package. `meta.json.baseline`, if present, stores only the name,
address, and two raw anchor scores.

## Top-level shape

```text
task_package/
├── instruction.json
├── input/
├── environment/
├── verifier/
│   ├── validation/run/main.py
│   └── test/
│       ├── run/main.py
│       └── data/                 optional; hidden labels and private score rules
└── meta.json
```

Exactly those five top-level names. No extra files or folders at the root.
No `README`, `TASK.md`, construction history, baseline bundle, tests, or
checksum list. Website copy can be generated from `instruction.json` and
`meta.json`.

Verifier internals besides the two fixed entries are the task’s own business.
Hidden scoring data belongs under `test`, usually `verifier/test/data/`. Do
not invent a third public interface name.

## `instruction.json`

This file is what the agent reads: the problem, the visible inputs, the files
it must submit, the software already provided, extra hard rules, and the
resource cap for a formal run.

```json
{
  "instructions": "English statement of the goal, background, and required actions.",
  "input": [
    {"path": "data", "format": "directory", "description": "What this path contains."}
  ],
  "submission": [
    {
      "path": "submission.json",
      "format": "json",
      "description": "What the frozen file must contain.",
      "has_template": true
    }
  ],
  "environment": [],
  "requirement": [],
  "limitation": {}
}
```

Rules:

- `instructions` is one string. It already includes the objective. There is
  no separate `objective` field.
- `input` may be `[]`. Every file that actually sits under `input/` must be
  covered by an `input` entry or by a submission template path.
- `submission` is an array with at least one item, same shape as a file
  entry plus optional `has_template`. There is no wrapper object and no
  `required` flag; every listed file is required.
- Each item defaults to `has_template: true`. The starter file lives at
  the same relative path under `input/`. Set `has_template` to `false` only
  when a starter file is meaningless (for example a large binary).
- `environment` describes software already installed for the agent. If the
  task adds no extra software and uses the operator’s default image, write
  `[]`. Do not write a sentence that only restates “use the default image”.
  The machine-readable environment is `environment/environment.json`.
- `requirement` is a list of task-specific hard rules, or `[]`. Do not repeat
  isolation rules the framework already enforces.
- `limitation` is the cap a formal run will actually enforce: only `cpu`,
  `memory`, `gpu`, `runtime`. If the operator has not confirmed the cap,
  write `{}`. Construction-time resource estimates stay in `build/`.
- There is no `storage` field. The runner does not enforce a disk quota.

Unknown but non-critical values use typed empties: arrays `[]`, and
`metric.type` `null` when the display transform is not chosen yet. The task
statement, identity, at least one submission artifact,
`environment/environment.json`, both verifier entries, and every input the
scorer needs must not be empty.

`instruction.json` does not carry schema versions, score transforms, network
policy, task version, or checksums. The specific agent, model, seed, and
repeat count belong to a run config, not this file.

## `input/`

Only files the agent may see: scientific inputs and optional submission
templates. No `public/` vs `private/` split. No source metadata, build
scripts, baselines, tests, or checksums.

Checksums do not enter the package. If the scorer needs labels the agent
must not see, put them under the verifier, not in `input/private`.

## `environment/`

Must contain `environment.json`. Two legal forms:

- `{"schema_version":"1.0","type":"default"}` — use the operator default
  image. This directory must then contain **only** that file.
- `{"schema_version":"1.0","type":"containerfile","containerfile":"Containerfile"}`
  — build a task image from that file before the agent starts. Extra
  dependency files may sit beside the Containerfile. A fill-in example is
  in `examples/environment-containerfile/`. The Containerfile must start
  from `ARG SIE_BASE_IMAGE` / `FROM ${SIE_BASE_IMAGE}`.

`instruction.json.environment` tells the agent what is already there.
`environment/` is what the runner actually builds. They do not substitute
for each other. Stay on `type: default` and `[]` until every extra package
has a frozen version. A named tool without a pin is a `build/` gap, not a
Containerfile. `type: containerfile` and the `environment` array must list
the same installed software.

## `verifier/`

Two required programs:

```text
verifier/validation/run/main.py
verifier/test/run/main.py
```

Both files must exist: the framework only accepts the actions `validation`
and `test`.

**validation** has one job: answer a mid-run check when this task needs
one. Follow the approved design. Do not add mid-run checks, do not
advertise them, and do not strip them from the write-up. Package text
states the task; it does not tell the agent that it should or should not
call validation.

If the design has no mid-run check, keep the template reject
`this task has no mid-run check`. If the design includes mid-run checks
but the request format is not frozen, keep those checks in
`instruction.json` and change the program error to
`mid-run check request schema is not frozen`. Do not leave the template
“no mid-run check” string, and do not invent the schema.

The framework calls it with `--input`, `--request`, `--response`. Request
and response are JSON objects with a byte cap. Scientific fields, budget,
and exhaustion rules are defined by the task, not by the framework.
Validation and test are separate programs. Only test writes the official
raw score. A validation reply must not replace or rewrite that score.

**test** is the one logical final score after the submission is frozen. The
framework calls it with `--input`, `--submission`, `--result`. The framework
may repeat the same frozen inputs to check determinism. The agent does not
get a second submit.

Both programs share one `VerifyContext`: a private JSON key/value store for
this run. The agent cannot see it. Validation and test open it with
`VerifyContext.current()`. A process crash, timeout, or illegal JSON rolls
the context back to the snapshot taken before that call. A normal “this
request is scientifically invalid” response must not spend the scientific
budget.

`context.log(value)` writes one JSON line to the process log. It is not
returned to the agent and is not a score.

The verifier must be fixed before the agent starts, run locally offline and
deterministically, and fail closed on missing files, illegal output, bad
numbers, and its own errors. The framework never mounts undeclared scoring
materials into the agent box.

## `meta.json`

Compact index. No construction history.

Required: `id`, `title`, `domain`, `task_sources`, `metric`. There is no
`source_id`.

- `id` matches `^[a-z0-9][a-z0-9._-]{0,127}$`.
- `task_sources` lists the construction prototype and the datasets or
  repositories that entered `input/` or the verifier. Each item is
  `{name, url}`. Papers used only to argue importance stay in `build/` or
  the proposal.
- `metric.description` states the raw measurement and which direction is
  better, in words.
- `metric.type` is the V1 display transform, or `null` (not classified yet),
  or `raw_only` (reviewed, no V1 transform). Allowed types: `linear_maximize`,
  `linear_minimize`, `log_ratio_maximize`, `log_ratio_minimize`,
  `upper_residual_log`, `lower_residual_log`, `power_maximize`,
  `power_minimize`, `monotone_piecewise_maximize`,
  `monotone_piecewise_minimize`, `custom`, `raw_only`, `null`.
- `linear_*` and `log_ratio_*` may omit `transform`. The framework then
  builds the display map from `baseline.bottom_line_score` → 0 and
  `baseline.score` → 1000. Those two types therefore require `baseline`
  when `transform` is absent.
- `upper_residual_log`, `lower_residual_log`, `power_*`,
  `monotone_piecewise_*`, and `custom` still require a complete
  `transform` (direction, parameters, anchors, invalid score, precision,
  rounding). `null` and `raw_only` must not have `transform`.
- `custom` requires `verifier/matrix/transform.py` with a pure
  `to_utility(raw_value, parameters)` that returns a finite `Decimal`
  (larger = better).

`baseline` is optional. If present it must contain exactly
`existing_baseline`, `name`, `source`, `score`, `bottom_line_score`.

- `score` maps to display 1000 (the reference level the task author set).
- `bottom_line_score` maps to display 0 (a valid result that shows no skill,
  usually a no-skill choice or the worst accepted configuration).
- Both are **raw** scores, same direction as the metric. They are not
  display scores. The mapping is not capped: better than `score` may exceed
  1000; worse than the bottom line may go below 0.
- `score` must be strictly better than `bottom_line_score` when
  `metric.type` has a direction. Minimize: `score` < `bottom_line_score`.
  Maximize: `score` > `bottom_line_score`. For `null` / `raw_only` the
  loader does not check order.
- `existing_baseline: true` means a real, reproducible public method.
  `name` and `source` are non-empty. `score` is that method’s measured raw
  score. The runnable baseline files stay outside the package.
- `existing_baseline: false` means an expert estimate. `name` and `source`
  are empty strings. The two numbers are estimates, not measured results.
- If there is neither a measured method nor an expert estimate, **omit**
  the whole `baseline` object.

Display scores are a single-task reading aid. They are not a cross-task
ability score. The official scientific result is always the verifier’s raw
primary metric.

## What the loader rejects

`sie check` / `TaskPackage.load` fail closed on:

- any top-level name other than the five
- schema-invalid JSON
- missing verifier entries
- `custom` metric without `verifier/matrix/transform.py`
- declared input path that does not exist
- `has_template: true` (the default) without the matching file under `input/`
- any file under `input/` that is not declared
- overlapping submission paths
- `type: containerfile` without that Containerfile
- `baseline.score` not strictly better than `bottom_line_score` when the
  metric type has a direction
- symlinks or special files anywhere in the package
