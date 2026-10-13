# Contributing a task

[简体中文](CONTRIBUTING.md) | **English**

[Official portal](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/en/) · [Submit Proposal](https://github.com/PrismaX-Team/LiveDiscoveryBench-Tasks/discussions/new?category=proposals) · [Task contract](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/en/contribute/task-contract/) · [Contribution board](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/en/contribute/#contribution-activity)

A task enters Science Innovation Exam in two steps: a public **Proposal** that is reviewed by a domain expert, then a **task PR** that adds the five-part task package and is reviewed by a domain expert and a technical expert. This page explains the PR step. Maintainer and reviewer rules live in [MAINTAINERS_EN.md](MAINTAINERS_EN.md).

## 1. Before you start

You need an **approved Proposal of your own** in this repository. Open one with the Proposals Discussion form if you have not yet. Bodies, links and comments are public, so do not include sensitive material or anything you cannot publish. When a maintainer approves it, the bot posts a comment with the next steps and a prefilled PR link. Editing an approved Proposal suspends its approval.

One approved Proposal becomes one task package and one PR.

## 2. Understand the task contract

A task package has exactly five top-level parts:

```text
tasks/<task-id>/
├── instruction.json   what the agent reads before starting
├── input/             agent-visible data and submission templates
├── environment/       environment.json (default image or Containerfile)
├── verifier/          validation/run/main.py and test/run/main.py
└── meta.json          identity, task sources, raw metric, baseline
```

The portal's [task contract page](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/en/contribute/task-contract/) walks through an annotated example and explains what every field is for and how to fill it. Read it once before touching the template. The working copy of the field rules is [contributor/build-sie-task-package/task-contract.md](contributor/build-sie-task-package/task-contract.md).

## 3. Get the template and the skill

Fork this repository and clone your fork. Everything you need is already inside it:

| What | Path |
| --- | --- |
| Empty, structurally valid package | [`tasks/_template/`](tasks/_template/) |
| Skill for an AI coding agent (Cursor, Codex, Claude Code) | [`contributor/build-sie-task-package/`](contributor/build-sie-task-package/) |
| What the default image already contains | [`contributor/build-sie-task-package/base-images.md`](contributor/build-sie-task-package/base-images.md) |
| Extra-software example (Containerfile) | [`contributor/build-sie-task-package/examples/environment-containerfile/`](contributor/build-sie-task-package/examples/environment-containerfile/) |
| Local structural check | [`contributor/build-sie-task-package/scripts/check_package.py`](contributor/build-sie-task-package/scripts/check_package.py) |

```sh
git clone https://github.com/<you>/LiveDiscoveryBench-Tasks.git
cd LiveDiscoveryBench-Tasks
git checkout -b task/<task-id>
cp -r tasks/_template tasks/<task-id>
```

To let an agent help, copy the skill directory into your agent's skills folder (for example `.cursor/skills/build-sie-task-package/` or `~/.codex/skills/build-sie-task-package/`), put the approved Proposal text and your materials (papers, data, code, licences) next to the package, and ask: "Use `build-sie-task-package` to prepare the task package for `tasks/<task-id>` from the proposal and materials." The skill fills what your materials support and asks for the facts that would change the science, the score or the contract. Keep construction notes and tests outside `tasks/<task-id>/`.

## 4. Build and check the package

- `<task-id>` is a short slug: lowercase start, then lowercase, digits, `.`, `_`, `-`; at most 128 characters. Directory name and `meta.json.id` must match.
- The package is English, even if the Proposal is Chinese.
- `input/` holds only agent-visible data and submission templates. Hidden scoring data lives under `verifier/test/`; validation and test must be self-contained.
- Both `verifier/validation/run/main.py` and `verifier/test/run/main.py` are required and must run offline and deterministically.
- The default image's contents are listed in [base-images.md](contributor/build-sie-task-package/base-images.md); do not redeclare them in the package. Keep the default image unless every extra package has a pinned version; then switch `environment/` to the Containerfile example.
- Do not invent hidden labels, scoring formulas, baseline numbers or licences. Leave `metric.type` as `null` and `limitation` as `{}` when not confirmed.
- Commit only redistributable material; the repository and every PR are public.

Run the same checks CI runs before pushing:

```sh
python3 contributor/build-sie-task-package/scripts/check_package.py tasks/<task-id>
python3 -I .github/scripts/task_structure.py .
```

A green check means the directory loads. Scientific validity and whether the verifier actually scores correctly are judged in review.

## 5. Open the pull request

1. Push your branch to your fork.
2. Open the PR against `PrismaX-Team/LiveDiscoveryBench-Tasks` `main`. The easiest way is the **Create task PR** link in the bot's approval comment on your Proposal: click **compare across forks**, pick your fork and branch, and the description already contains the required `Proposal:` line.
3. If you open the PR another way, use the [new task template](.github/PULL_REQUEST_TEMPLATE/new_task.md) (`?template=new_task.md`) and make sure the body contains exactly one line:

   ```text
   Proposal: https://github.com/PrismaX-Team/LiveDiscoveryBench-Tasks/discussions/NUMBER
   ```

4. Title the PR `[task] <task-id>` and describe what the package contains and how you validated the verifier.

One PR changes exactly one task directory and nothing else.

## 6. What happens next

1. **CI** checks the package structure and that the PR links your currently approved Proposal. It never runs your code.
2. A maintainer sets the `type:new-task` label and assigns two reviewers with `/reviewers domain=@… technical=@…`. Until the label is confirmed the Contribution gate shows pending, not failure.
3. The **domain reviewer** checks the science and the metric; the **technical reviewer** checks the implementation, environment and verifier determinism. Both must approve the current commit; pushing new commits asks for renewed approval.
4. After merge the task appears on the portal with you credited as contributor. Later repairs go through task-fix PRs.

Progress is visible on the [portal's contribution board](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/en/contribute/#contribution-activity) and in the PR itself.

<a id="templates"></a>
## Other PR types

| Purpose | Template | Required body line |
| --- | --- | --- |
| New task | [`new_task.md`](.github/PULL_REQUEST_TEMPLATE/new_task.md) | `Proposal: https://github.com/PrismaX-Team/LiveDiscoveryBench-Tasks/discussions/NUMBER` |
| Task fix | [`task_fix.md`](.github/PULL_REQUEST_TEMPLATE/task_fix.md) | `Task: EXISTING_TASK_ID` |
| Maintenance | [`maintenance.md`](.github/PULL_REQUEST_TEMPLATE/maintenance.md) | Describe the scope |

Task fixes touch one existing task and need no new Proposal. Maintenance PRs cannot add tasks. Review rules for each type are in [MAINTAINERS_EN.md](MAINTAINERS_EN.md).
