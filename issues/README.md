# issues/ — Issue 与 Merge 请求追踪

> Soft Serve **没有** issue / PR 功能（已核实：二进制无相关实现，SSH 命令仅 `repo`/`settings`/`pubkey`/`token`，
> README 只文档化 webhooks 与 hooks）。因此 issue 与 merge 请求以 **仓库内文件** 表达，随 git 推送，
> 由服务端 `pre-receive` 钩子强制流程。
>
> 完整操作指南：`docs/sop/soft-serve-issue-mr-workflow.md`

## 目录约定

| 路径 | 内容 |
|------|------|
| `issues/<ID>-<slug>.md` | 一个 issue 一个文件。`<ID>` 沿用项目既有编号 `I-NN`（当前最大 I-83，下一个是 I-84） |
| `issues/TEMPLATE.md` | issue 模板，复制后改名使用 |
| `merge-requests/<ID>.md` | 与 issue **同名 ID** 的 merge 请求记录（评审决定、head SHA、合并结果） |
| `merge-requests/TEMPLATE.md` | MR 模板 |
| `issues/python-harness-issues.md` | 遗留 backlog（I-15…I-43 + 2026-10-19 遗留），逐步迁入 I-NN 文件 |

分支名：`mr/<ID>-<slug>`（一个 MR 一条分支）。

## 为什么 tracker 可以直接推 main

服务端策略只保护 `main` 上的**代码/文档**变更。`issues/**` 与 `merge-requests/**` 属 tracker 记账，
允许直接推送 main——所以「先发起 issue」不需要为 issue 本身再开一个 MR。

## 状态机

issue 的 `status`：
```
open ──→ in-review ──→ closed
  └──────────────────→ wontfix
```

MR 的 `status`：
```
open ──→ approved ──→ merged
  ├──→ changes-requested ──→ (作者补提交) ──→ open
  └──→ rejected
```

标签沿用 `docs/agents/triage-labels.md`（`needs-triage` / `needs-info` / `ready-for-agent` /
`ready-for-human` / `wontfix`），不新增自定义标签。
