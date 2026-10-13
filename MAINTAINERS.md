# 维护者指南

**简体中文** | [English](MAINTAINERS_EN.md)

面向维护者与审核者的治理规则。共建者请阅读[共建指南](CONTRIBUTING.md)。

## Proposal 审核

Proposal 以 Proposals 分类中的 Discussion 提交，以 GitHub 账号确认身份，不收集私人邮箱。正文、链接和评论均公开。指标补充说明始终显示，自定义、仅原始分和未分类指标时必填；两项授权确认始终必需，修订后同样需要。

非作者的维护者在 `main` 上运行 **Proposal review**，填写 Discussion 编号、决定及 20–5000 字符的公开理由。Action 校验权限并记录操作者、决定、正文摘要及运行链接。标签仅用于展示，不能作为批准凭据。修改获批正文（包括修改后还原）须重新批准。随后由原作者创建任务 PR，无需回网站绑定。

**Force / 硬性通过** 只在决定为 `approve` 时生效：跳过表单字段校验，权限、非自审、分类、提案仍开放和公开理由仍然检查。决定评论会写明本次跳过了校验。职业主页和参考文献接受 `http://` 与 `https://`，每行一条。解析表单时会先把 Windows 换行转成 Unix 换行。

每次决定都必须使用 **Run workflow** 新建运行。即使是原审核者，**Re-run jobs** 也会被拒绝；失败后须核对当前提案并重新发起审核。重跑原成功决定会使该运行失去批准效力。

## PR 类型与审核

| 用途 | 标签 | 正文必需行 |
| --- | --- | --- |
| [新增任务](.github/PULL_REQUEST_TEMPLATE/new_task.md) | `type:new-task` | `Proposal: https://github.com/PrismaX-Team/LiveDiscoveryBench-Tasks/discussions/NUMBER` |
| [任务修复](.github/PULL_REQUEST_TEMPLATE/task_fix.md) | `type:task-fix` | `Task: EXISTING_TASK_ID` |
| [仓库维护](.github/PULL_REQUEST_TEMPLATE/maintenance.md) | `type:maintenance` | 说明维护范围 |

维护者确认唯一类型标签；确认前 Contribution gate 保持 **pending**（不是失败），PR 也暂不进入公开看板。新增任务须关联本仓库仍开放、有效获批且作者相同的 Proposal。新增/修复 PR 只修改一道任务，不混入仓库策略变更。修复关联已有任务，无需再提 Proposal。维护采用普通 GitHub 审核，不得新增任务且不进入任务看板。科学内容及评分变更应走任务修复，不要归为维护。

对任务 PR，维护者发布恰好如下格式的评论：

```text
/reviewers domain=@DOMAIN_LOGIN technical=@TECHNICAL_LOGIN
```

两席须由不同且非 PR 作者的账号担任。正式结论使用 GitHub 的 **Review changes → Approve**；两席须批准当前提交。更新代码或撤回审核会使相应批准失效。可编辑、删除指定评论或发布更新的指定评论改派；最新指定不合法时检查不通过。

## 合并门槛

**Contribution gate** 从默认分支加载可信策略，重新读取 GitHub 状态，校验分类来源、文件变化、任务结构、当前 Proposal 批准及双审。它不会执行候选代码或解压上传文件。PR、Proposal、审核席及 Review 的变化均触发重查，另每 15 分钟定时对账一次；API 错误不判通过。

`main` 应设置此必需状态检查、分支最新要求、至少一份原生审核、旧审核失效、最后推送批准、讨论解决、管理员同样受限，并禁止强推及删除。这保证条件未满足时不能正常合并，但不阻止创建 PR。事件处理存在延迟；维护者合并前仍须核对最新科学内容与获批范围，并等待最新一次检查完成。

## 任务包审核要点

- 目录名与 `meta.json.id` 一致并匹配 `^[a-z0-9][a-z0-9._-]{0,127}$`；顶层严格五部分。
- 提交模板按产物相对路径存于 `input/`；`has_template` 默认为 true，不适合提供模板时才写 `has_template: false`。
- `environment/environment.json` 为 `default` 且目录内无其他文件，或为 `containerfile` 并附构建文件及依赖。
- 双 verifier 入口离线且确定性；validation 与 test 自包含。
- `task_sources` 列出原型与数据来源；未分类 `metric.type` 保持 null，未确认上限保持 `{}`，不编造转换参数或 baseline。
- 仅提交可再分发材料。对 Agent 隐藏的数据不等于可以公开；非公开评分数据须另议交付方式。

## 任务维护与官网

Discussion、PR 与 Review 记录是贡献状态的唯一来源。官网每小时构建公开快照并链接回 GitHub；失败时保留上一成功版本。任务记录由网站的类型化源文件维护。已发布任务出现问题时，在网站 `src/data/task-maintenance.ts` 中记录中英文说明、`needs_fix`/`fixing` 状态和修复 PR；修复合并并经维护者核对后才恢复 `active`。

仅用于验收的 Discussion 和 PR 标题须以 `[ACCEPTANCE]` 开头并带 `acceptance` 标签，始终不进入官网；验收后关闭，不合并虚构任务。双审成功路径须由两位不同的真实审核者完成。

## 合同同步

`contributor/build-sie-task-package/`（Skill、schema、`check_package.py`）与 `tasks/_template/` 从框架仓库同步。任务合同变更时，与 `.github/scripts/task_structure.py` 在同一提交中一起更新。

```sh
python3 -I .github/scripts/test_task_structure.py
python3 -I .github/scripts/test_governance.py
python3 -I .github/scripts/task_structure.py .
```

## 文档语言

根目录说明文档采用中英两份文件：中文为默认（`README.md`、`CONTRIBUTING.md`、`MAINTAINERS.md`），英文为 `*_EN.md`。修改任一份时同步更新另一份。Proposal 表单、PR 模板与机器人评论保持单份双语，不拆分。
