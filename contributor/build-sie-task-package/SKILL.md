---
name: build-sie-task-package
description: >-
  Builds and checks a Science Innovation Exam five-part task package from an
  approved proposal and contributor materials. Use when the user @-mentions
  this skill, asks to prepare a formal SIE task package, fill the task-package
  template, explain the task contract, or run local package checks.
disable-model-invocation: true
---

# Build a Science Innovation Exam task package

`contract_revision`: 2026-09-17

You are assisting a contributor in building a **formal task package** (the five
files/folders the benchmark actually runs). The package must stand up to the
task contract. Materials that are not enough for the next write are asked for;
independent parts keep moving. Write tests for anything easy to get wrong,
especially the verifier (the scoring program).

On load, read every sibling file in this skill before touching the workspace.
Do not search the rest of the repository for a second copy of the rules.

In this repository, the Chinese file
[`任务包合同.md`](../../../任务包合同.md) is the package-contract
authority. `task-contract.md` is the English working copy used when this
skill is loaded alone. If they disagree, follow the Chinese file and update
this skill in the same change.

| File | What it is |
|---|---|
| [project-background.md](project-background.md) | What this exam is, the four Live meanings, admission vs package-ready vs score-now |
| [task-contract.md](task-contract.md) | English working copy of the five-part field rules |
| [field-guide.md](field-guide.md) | What each field is for, in everyday language |
| [verifier-api.md](verifier-api.md) | How validation and test are called, shared state, result JSON |
| [proposal-mapping.md](proposal-mapping.md) | Proposal form fields to package fields |
| [template/](template/) | Empty but structurally valid package |
| [base-images.md](base-images.md) | What the default CPU/GPU base images already contain |
| [examples/environment-containerfile/](examples/environment-containerfile/) | How to add extra software (Containerfile) |
| [scripts/check_package.py](scripts/check_package.py) | Local structural check |

If a later framework commit changes the contract, this skill and its bundled
schemas must change in the same commit. State `contract_revision` at the start
of the conversation. If the contributor copy is older than the public
repository, tell them to replace the skill directory.

## Workspace

**One approved proposal is one exam and one `task_package/`.** Count
proposals first. Three proposals means three packages. Do not merge them.

One proposal:

```text
workspace/
  proposal/          that approved proposal
  materials/         papers, data, code, licenses, notes
  task_package/      the five-part package only
  build/             notes, gap list, tests — never inside the package
```

More than one proposal — one four-folder tree each.

```text
workspace/
  <task_id>/
    proposal/
    materials/
    task_package/
    build/
```

On the first turn, **copy** contributor files into those trees. Do not
move or delete the originals, even when the filename already says they
are proposals. Ask before any later move. Do not rewrite files while
tidying. If two texts might be the same proposal, ask before splitting
or joining.

The official package top level must be exactly:

`instruction.json`, `input/`, `environment/`, `verifier/`, `meta.json`

No README, tests, checksums, baselines, or construction history in that
directory.

## How to work

1. Inspect the workspace. List every distinct approved proposal. Create
   one four-folder tree per proposal. Copy each proposal text into that
   tree’s `proposal/`. Leave the contributor’s originals where they are
   until they say the originals may be moved.
2. If a tree has no `task_package/`, copy [template/](template/) into it.
   If the template is also missing, recreate that same empty package from
   this skill.
3. Classify each material before using it:
   - **Agent-visible**: goes in `input/` and must be declared in `instruction.json`
   - **Hidden scoring**: goes under `verifier/test/` only if the scorer needs private labels; there is no required `data/` folder
   - **Construction-only**: stays in `proposal/`, `materials/`, or `build/`
4. Fill what the current materials actually support. Ask for a missing fact
   when that fact would change the science, the score, safety, or the contract.
   Keep filling unrelated parts in the same turn.
5. Do not invent hidden labels, a scoring formula, a scientific claim, a
   baseline number, or a license. Put reversible guesses in `build/` notes,
   not in the package. Follow the proposal on validation. If it already
   includes mid-run checks but the request format is not frozen, keep
   those checks in the English write-up and change the template reject
   from “this task has no mid-run check” to “mid-run check request
   schema is not frozen”. Do not invent the schema. See
   [verifier-api.md](verifier-api.md).
6. After each meaningful write, run the local check on that package. Fix
   structural errors before treating it as format-ready.
7. Keep a short `build/gaps.md` per tree so the next turn does not
   overwrite finished work. This is a memory aid, not a state machine.

When the contributor asks what a contract part means, answer from the files
in this skill.

## Checks and tests

Structural check (same rules as `sie check` when the framework is installed):

```bash
python3 scripts/check_package.py /path/to/task_package
```

or, if the framework is on `PATH`:

```bash
sie check /path/to/task_package
```

A green structural check means the directory can be loaded. It does **not**
mean the question is scientifically admitted, that an experiment can run, or
that a hidden score can be computed now. Always report those three states
separately.

Write tests under `build/tests/`, not inside the package. Verifier tests
should compare the program to the contract and to the contributor scoring
materials, not to the code you just wrote. Prefer:

- a known-good fixture yields the expected raw score
- missing, extra, or malformed submission files are invalid
- two identical invocations write the same result
- a rejected validation request does not spend the scientific budget
- test reads measurement history from `VerifyContext`, not from files the
  agent could have rewritten
- hidden labels never appear in `input/`

## Language and identity

The official package is English. `instruction.json`, `meta.json` title and
metric text, `requirement` lines, and file descriptions are English even
when the proposal is Chinese. Translate the agent-facing parts. If you
invented the English title, ask the contributor to confirm it. Do not
leave Chinese in the package.

`meta.json.id` is a short slug: lowercase start, then lowercase / digits /
`.` `_` `-`, max 128 characters. The test stub’s `task_id` must stay equal
to `meta.json.id`.

The template `environment/` is the default image (`type: default`); what
that image already contains is listed in [base-images.md](base-images.md).
Keep that, and keep `instruction.json.environment` as `[]`, until **both** the
package name and a frozen version are known. A proposal that only names
RDKit or a git repo is not enough: write the names in `build/`, ask for
pins, and do not add a Containerfile. When every extra package has a
pin, replace `environment/` with
[examples/environment-containerfile/](examples/environment-containerfile/)
and fill `instruction.json.environment` with the same names and versions.
Do not leave `type: containerfile` with an empty install list or an empty
`environment` array.

Template `limitation` and `baseline` numbers are shape placeholders. Do not
leave them in a finished package. For `linear_minimize` / `log_ratio_minimize`,
`baseline.score` must be strictly smaller than `bottom_line_score`.
