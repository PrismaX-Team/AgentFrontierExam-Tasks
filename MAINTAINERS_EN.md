# Maintainer guide

[简体中文](MAINTAINERS.md) | **English**

Governance rules for maintainers and reviewers. Contributors should read the [contributing guide](CONTRIBUTING_EN.md) instead.

## Proposal review

Proposals arrive as Discussions in the Proposals category. GitHub accounts identify authors; no private email is collected. Bodies, links and comments are public. The metric explanation is always visible and required for custom, raw-only or unclassified metrics. Both permission confirmations are always required, including after revisions.

A maintainer other than the author runs **Proposal review** on `main`, supplying the Discussion number, the decision and a public reason (20–5000 characters). The Action checks permission and records the actor, decision, exact content fingerprint and run link. Labels display status only and are not approval evidence. Editing approved content, including reverting an edit, requires renewed approval. The original author then opens the task PR; there is no website binding step.

**Force** applies only when the decision is `approve`. It skips form-field checks. Permission, the ban on self-approval, category, an open proposal and the public reason still apply. The decision comment states that form checks were skipped. A professional profile and each reference line may be either `http://` or `https://`. Form parsing converts Windows newlines before reading fields.

Always use **Run workflow** for a new decision. **Re-run jobs** is rejected, even for the original reviewer; after a failure, check the current proposal and start a new dispatch. Re-running a previously successful decision invalidates that run as approval evidence.

## PR types and reviews

| Purpose | Label | Required body line |
| --- | --- | --- |
| [New task](.github/PULL_REQUEST_TEMPLATE/new_task.md) | `type:new-task` | `Proposal: https://github.com/PrismaX-Team/LiveDiscoveryBench-Tasks/discussions/NUMBER` |
| [Task fix](.github/PULL_REQUEST_TEMPLATE/task_fix.md) | `type:task-fix` | `Task: EXISTING_TASK_ID` |
| [Maintenance](.github/PULL_REQUEST_TEMPLATE/maintenance.md) | `type:maintenance` | Describe the scope |

A maintainer confirms exactly one type label; until then the Contribution gate stays **pending** (not failed) and the PR is not yet listed on the public board. New tasks must match an open, currently approved Proposal in this repository with the same author. New-task and fix PRs change exactly one task and cannot mix in repository policy changes. Fixes refer to an existing task without another Proposal. Maintenance uses ordinary GitHub review, cannot introduce tasks, and stays off the task board. Scientific and scoring changes belong in task fixes; do not reclassify them as maintenance.

For task PRs, a maintainer posts exactly:

```text
/reviewers domain=@DOMAIN_LOGIN technical=@TECHNICAL_LOGIN
```

The two reviewers must be different and neither may be the PR author. Use GitHub's **Review changes → Approve** for formal decisions. Both seats must approve the current commit. A commit update or dismissed review invalidates the relevant approval. Edit or delete the assignment comment, or post a newer one, to change seats. An invalid newer assignment fails closed.

## Merge boundary

**Contribution gate** runs the trusted default-branch policy, reloads GitHub state and checks type provenance, file changes, task structure, current Proposal approval and two current reviews. It never runs candidate code or unpacks submitted archives. PR, Proposal, assignment and review changes trigger rechecks; a scheduled reconciliation runs every 15 minutes. API errors do not pass the check.

Protect `main` with this required GitHub Actions status, strict up-to-date checks, at least one native review, stale-review dismissal, last-push approval, conversation resolution, administrator enforcement, and no force pushes or deletions. This prevents normal merges with unmet requirements; it does not prevent creating PRs. Event processing has latency: before merging, maintainers must still inspect the latest scientific content and approved scope and wait for the latest gate run.

## Review checklist for task packages

- Directory name and `meta.json.id` match `^[a-z0-9][a-z0-9._-]{0,127}$`; exactly five top-level parts.
- Submission templates use artifact-relative paths under `input/`; `has_template` defaults to true and is set to `false` only when a template does not fit.
- `environment/environment.json` is `default` with no other files, or `containerfile` with its build file and dependencies.
- Both verifier entries are offline and deterministic; validation and test are self-contained.
- `task_sources` lists the prototype and data URLs; unclassified `metric.type` stays null; unconfirmed limits stay `{}`; no invented transform parameters or baselines.
- Only redistributable material is committed. Data hidden from the agent is not automatically safe to publish; agree a delivery route for nonpublic scoring data.

## Task maintenance and portal

Discussion, PR and Review records are the only contribution state. The portal builds a public snapshot hourly and links back to GitHub; failures retain the last successful site. Task and leaderboard records are typed website source. For a broken published task, record bilingual notes, the `needs_fix`/`fixing` status and its fix PR in the website's `src/data/task-maintenance.ts`; the task is excluded from both leaderboards until a maintainer verifies the repair merge and restores `active`.

Acceptance-only Discussions and PRs must start with `[ACCEPTANCE]` and carry the `acceptance` label; they are excluded from the portal. Close them after checks and never merge fictional tasks. A successful dual review requires two real, different people.

## Contract sync

`contributor/build-sie-task-package/` (skill, schemas, `check_package.py`) and `tasks/_template/` are copied from the framework repository. When the task contract changes, update both together with `.github/scripts/task_structure.py` in one commit.

```sh
python3 -I .github/scripts/test_task_structure.py
python3 -I .github/scripts/test_governance.py
python3 -I .github/scripts/task_structure.py .
```

## Documentation languages

Root documentation is kept as two files per document: Chinese is the default (`README.md`, `CONTRIBUTING.md`, `MAINTAINERS.md`) and English lives in `*_EN.md`. Update both when changing either. The Proposal form, PR templates and bot comments stay single bilingual files and are not split.
