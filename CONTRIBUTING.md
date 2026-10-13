# 共建指南：提交一道任务

**简体中文** | [English](CONTRIBUTING_EN.md)

[官网](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/zh/) · [提交 Proposal](https://github.com/PrismaX-Team/LiveDiscoveryBench-Tasks/discussions/new?category=proposals) · [任务合同](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/zh/contribute/task-contract/) · [共建看板](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/zh/contribute/#contribution-activity)

任务分两步进入 Science Innovation Exam：先提交公开 **Proposal**，由一位领域专家审核；获批后再提交**任务 PR**，加入五部分任务包，由一位领域专家和一位技术专家审核。本页说明 PR 这一步。维护者与审核者规则见 [MAINTAINERS.md](MAINTAINERS.md)。

## 1. 开始之前

你需要一份**本人已获批的 Proposal**。若还没有，请通过 Proposals 分类的 Discussion 表单提交。正文、链接与评论都会公开，请勿放入敏感或无权公开的材料。维护者批准后，机器人会在讨论串中给出下一步说明和预填好的 PR 链接。修改已获批的 Proposal 会使批准暂停。

一份获批 Proposal 对应一个任务包和一个 PR。

## 2. 了解任务合同

任务包顶层严格五部分：

```text
tasks/<task-id>/
├── instruction.json   Agent 开始前读到的任务说明
├── input/             Agent 可见的数据与提交模板
├── environment/       environment.json（默认镜像或 Containerfile）
├── verifier/          validation/run/main.py 与 test/run/main.py
└── meta.json          任务身份、任务来源、原始指标、baseline
```

官网的[任务合同详情页](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/zh/contribute/task-contract/)用一个带注释的示例逐项说明每个字段是做什么的、该怎么填。动手前先读一遍。字段规则的英文工作副本见 [contributor/build-sie-task-package/task-contract.md](contributor/build-sie-task-package/task-contract.md)。

## 3. 获取模板与 Skill

先 Fork 本仓库并克隆你的 fork。需要的东西都已经在仓库里：

| 内容 | 路径 |
| --- | --- |
| 空的合法任务包模板 | [`tasks/_template/`](tasks/_template/) |
| 给 AI 编程助手（Cursor、Codex、Claude Code）的 Skill | [`contributor/build-sie-task-package/`](contributor/build-sie-task-package/) |
| 默认镜像已包含的软件 | [`contributor/build-sie-task-package/base-images.md`](contributor/build-sie-task-package/base-images.md) |
| 额外软件示例（Containerfile） | [`contributor/build-sie-task-package/examples/environment-containerfile/`](contributor/build-sie-task-package/examples/environment-containerfile/) |
| 本地结构检查 | [`contributor/build-sie-task-package/scripts/check_package.py`](contributor/build-sie-task-package/scripts/check_package.py) |

```sh
git clone https://github.com/<you>/LiveDiscoveryBench-Tasks.git
cd LiveDiscoveryBench-Tasks
git checkout -b task/<task-id>
cp -r tasks/_template tasks/<task-id>
```

若希望 AI 协助，把 Skill 目录拷到助手的 skills 目录（如 `.cursor/skills/build-sie-task-package/` 或 `~/.codex/skills/build-sie-task-package/`），把已获批的 Proposal 全文和材料（论文、数据、代码、许可）放在任务包旁边，然后说：“按 `build-sie-task-package`，根据 proposal 和材料为 `tasks/<task-id>` 准备任务包。”Skill 会先填材料能支持的部分，缺会改变科学、分数或合同的事实时再问你。构建笔记和测试放在 `tasks/<task-id>/` 之外。

## 4. 构建并检查任务包

- `<task-id>` 为短 slug：小写字母开头，只含小写、数字、`.`、`_`、`-`，最长 128 字符；目录名与 `meta.json.id` 一致。
- 任务包内文本为英文，即使 Proposal 是中文。
- `input/` 只放 Agent 可见数据与提交模板；隐藏评分数据放在 `verifier/test/` 下，validation 与 test 必须自包含。
- `verifier/validation/run/main.py` 与 `verifier/test/run/main.py` 均必需，须离线、确定性运行。
- 默认镜像已包含的软件见 [base-images.md](contributor/build-sie-task-package/base-images.md)，无需在任务包中重复声明。除非每个额外软件都有固定版本，否则保持默认镜像；需要时再把 `environment/` 换成 Containerfile 示例。
- 不编造隐藏标签、评分公式、baseline 数值或许可；未确认时 `metric.type` 写 `null`，`limitation` 写 `{}`。
- 只提交可再分发的材料；仓库和 PR 均公开。

推送前先在本地跑一遍 CI 同样会跑的检查：

```sh
python3 contributor/build-sie-task-package/scripts/check_package.py tasks/<task-id>
python3 -I .github/scripts/task_structure.py .
```

绿灯只表示目录能被加载；科学有效性和 verifier 是否正确评分由审核判断。

## 5. 提交 PR

1. 把分支推到你的 fork。
2. 向 `PrismaX-Team/LiveDiscoveryBench-Tasks` 的 `main` 发起 PR。最简单的方式是点击 Proposal 讨论串中机器人评论里的 **Create task PR** 链接：点 **compare across forks**，选择你的 fork 和分支，正文已带上必需的 `Proposal:` 行。
3. 若从其他入口创建，请选择[新增任务模板](.github/PULL_REQUEST_TEMPLATE/new_task.md)（`?template=new_task.md`），并确保正文含有这一行：

   ```text
   Proposal: https://github.com/PrismaX-Team/LiveDiscoveryBench-Tasks/discussions/NUMBER
   ```

4. PR 标题写 `[task] <task-id>`，正文说明任务包内容和 verifier 的验证方式。

一个 PR 只改动一个任务目录，不改其他文件。

## 6. 之后会发生什么

1. **CI** 检查任务包结构，以及 PR 是否关联了你当前有效获批的 Proposal。CI 不执行你的代码。
2. 维护者设置 `type:new-task` 标签，并用 `/reviewers domain=@… technical=@…` 指定两位审核者。标签确认前，Contribution gate 显示 pending 而非失败。
3. **领域审核者**核对科学与指标，**技术审核者**核对实现、环境与 verifier 确定性。两位都须批准当前提交；再推送新提交需重新批准。
4. 合并后任务出现在官网并署名。后续修复走任务修复 PR。

进展可在[官网共建看板](https://prismax-team.github.io/LiveDiscoveryBench-Tasks/zh/contribute/#contribution-activity)和 PR 中查看。

<a id="templates"></a>
## 其他 PR 类型

| 用途 | 模板 | 正文必需行 |
| --- | --- | --- |
| 新增任务 | [`new_task.md`](.github/PULL_REQUEST_TEMPLATE/new_task.md) | `Proposal: https://github.com/PrismaX-Team/LiveDiscoveryBench-Tasks/discussions/NUMBER` |
| 任务修复 | [`task_fix.md`](.github/PULL_REQUEST_TEMPLATE/task_fix.md) | `Task: EXISTING_TASK_ID` |
| 仓库维护 | [`maintenance.md`](.github/PULL_REQUEST_TEMPLATE/maintenance.md) | 说明维护范围 |

任务修复只改动一道已有任务，无需新 Proposal；维护 PR 不得新增任务。各类型的审核规则见 [MAINTAINERS.md](MAINTAINERS.md)。
