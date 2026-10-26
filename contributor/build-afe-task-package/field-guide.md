# Field guide

`contract_revision`: 2026-09-17

Use this page when the contributor asks what a field is for. Names stay in
English because they are the contract keys.

## The five parts

**`instruction.json`** — the exam paper the agent reads. It says what to do,
which files it can see, which files it must hand in, what software is already
installed, any extra hard rules, and the resource cap.

**`input/`** — the folder the agent is allowed to open. Public data and blank
answer sheets live here. Hidden answer keys do not.

**`environment/`** — the computer the agent actually runs on. Either “use the
operator default image” or “build this Containerfile first”.

**`verifier/`** — two small programs that must both exist as files. Test
gives one official raw score after the agent has finished and its files have
been frozen. Validation answers a mid-run check when the approved design
already needs one. If it does not, the file stays and returns that this
task has no mid-run check. The two programs are independent. Only test
writes the official score.

**`meta.json`** — the library card: id, title, field, where the package was
built from, which datasets it uses, and how the raw score is described.

## `instruction.json` fields

**`instructions`** — one English write-up of the scientific goal, the
allowed actions, and how to submit. If the agent would need a second file to
understand the job, that text is still missing.

**`input[]`** — a catalogue of paths under `input/`. Each item is
`path`, `format`, `description`. A directory path covers every file under it.
An empty list is legal only if `input/` has no leftover files except
submission templates already listed under `submission`.

**`submission[]`** — the files the framework will freeze and give to test.
Same kind of list as `input[]`, plus optional `has_template`. Every listed
path is required. `has_template` defaults to true: there must be a starter
file at `input/<same path>`.

**`environment[]`** — human-readable list of extra software the agent can
already use. Empty if the task adds none, or if names are known but
versions are not frozen yet. This list does not install anything;
`environment/environment.json` does. Keep `type: default` until every
extra package has a name **and** a frozen version. Then copy
`examples/environment-containerfile/` over `environment/`, write the
install pins, and fill this array with the same names and versions. Do
not switch to a Containerfile that only has commented placeholders.

**`requirement[]`** — extra hard rules that are specific to this task, such
as “do not download the original hidden labels”. Empty if there are none.
Do not restate “the container is isolated”.

**`limitation`** — the official run cap: `cpu.cores`, `memory.gb`,
`gpu.count`, `runtime.seconds`. The template fills these so you can see the
shape; replace them with operator-confirmed numbers. Proposal estimates stay
in `build/` until then. There is no disk-quota field.

## `meta.json` fields

**`id`** — short machine name of the task, used in scores and folders.

**`title`** — human title shown on the site.

**`domain`** — scientific field, for example `biomedicine`.

**`task_sources`** — construction prototype plus the datasets or repositories
that actually appear in `input/` or the verifier. Importance-only citations
stay in the proposal. There is no `source_id`.

**`metric.description`** — in words: what number test writes, and whether
bigger or smaller is better.

**`metric.type`** — how a display score (a 0 / 1000 reading aid on this
single task) is computed from that raw number. Leave `null` until the type
and both anchors are known. `raw_only` means “we reviewed it and we are not
shipping a V1 display transform”.

**`metric.transform`** — extra display settings. Omit it for `linear_*` and
`log_ratio_*`; the framework uses the two baseline raw scores as 0 and 1000.
Required for residual-log, power, piecewise, and custom types. Forbidden on
`null` and `raw_only`. The 1000 anchor is a reference level, not a
scientific ceiling. Stronger results may go above 1000.

**`baseline.score`** — the raw value that maps to display 1000. The template
`1.0` is a shape placeholder, not a scientific claim.

**`baseline.bottom_line_score`** — the raw value that maps to display 0: a
valid no-skill result, not “the agent crashed”. Template `0.0` is the same
kind of placeholder. For a minimize metric, `score` must be smaller than
this number.

**`baseline.existing_baseline`** — `true` if `score` was measured on a real
public method you can re-run; `false` if both numbers are expert estimates
(keep `name` and `source` empty). Delete the whole object only when the
type is still `null` / `raw_only` and you truly have no anchors.

## Verifier and scores

**Raw score** — the scientific measurement test writes in
`primary_metric`. This is the official result.

**Display score** — a linear (or other declared) reading aid on this one
task. Do not average display scores across tasks.

**Arena score** — a later cross-task ranking that uses pairwise wins, not
display scores. A contributor package does not compute it.

**`VerifyContext`** — a private notebook for this one experiment. Validation
can write “we already measured these ids”. Test should read that notebook,
not trust files the agent could have edited after the fact.

**Invalid submission** — the files are missing, extra, malformed, or break a
scientific rule. Test still writes a schema-valid JSON object with
`valid: false` and an `error` string. It must not pretend that is a strong
scientific result.

## Common mistakes

- Putting hidden labels in `input/` so the agent can read the answer key.
- Adding `README.md` at the package root (the loader rejects extra names).
- Putting tests inside the package instead of `build/tests/`.
- Writing `limitation.storage` (rejected).
- Filling `baseline` with placeholder zeros.
- Setting `has_template: true` and forgetting the starter file.
- Making validation the official score. Only test writes the official raw
  score.
- Adding mid-run checks because the validation file exists, or deleting
  them from the write-up because the request schema is not frozen yet.
- Leaving the template error `this task has no mid-run check` after the
  write-up already says the design includes a mid-run check. Change that
  error to `mid-run check request schema is not frozen`.
- Telling the agent it should or should not call validation.
- Switching to `type: containerfile` because the proposal named a tool
  but did not freeze versions. Keep the default image until the pins exist.
- Treating a green `sie check` as scientific admission.
