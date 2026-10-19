## New task / 新增任务

Proposal: https://github.com/PrismaX-Team/Science-Innovation-Exam-Tasks/discussions/NUMBER

Use your own currently approved Proposal. Editing the approved content invalidates approval. Title the PR `[task] <task-id>` from a branch such as `task/<task-id>`. / 必须关联本人当前有效获批的 Proposal；修改获批内容须重新批准。PR 标题写 `[task] <task-id>`，分支名建议 `task/<task-id>`。

### Implementation / 实现说明

Describe the task package, how the verifier was validated, and the scientific scope. / 说明任务包、verifier 的验证方式及科学范围。

### Checklist / 自查

- [ ] I read the [task contract](https://prismax-team.github.io/Science-Innovation-Exam-Tasks/en/contribute/task-contract/) and this PR changes only `tasks/<task-id>/`. / 已读任务合同；本 PR 只改动 `tasks/<task-id>/`。
- [ ] `python3 contributor/build-sie-task-package/scripts/check_package.py tasks/<task-id>` passes. / 本地结构检查通过。
- [ ] The package is English; `input/` holds only agent-visible data; validation and test are self-contained. / 任务包为英文；`input/` 只含 Agent 可见数据；validation 与 test 自包含。
- [ ] All committed material is redistributable. / 提交的材料均可再分发。

### Review / 审核

A maintainer sets `type:new-task` and comments `/reviewers domain=@LOGIN technical=@LOGIN`. Both distinct reviewers must approve the current commit using GitHub Review. / 维护者确认类型并指定两位不同且非作者的审核者；两席须通过 GitHub Review 批准当前提交。
