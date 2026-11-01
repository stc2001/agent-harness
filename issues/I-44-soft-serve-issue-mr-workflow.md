---
id: I-44
title: 在 soft-serve 上建立 issue → merge 请求工作流
labels: [ready-for-human]
author: admin
created: 2026-10-02
status: closed
---

## 背景 / What & Why

`OH_workspace` 已推送到本地 soft-serve（`ssh://localhost:23231/OH_workspace.git`）。后续会有**其他会话**
在该 server 上协作：先发起 issue，再提交 merge 请求等待评审合并。

但 soft-serve 本身**没有** issue tracker，也**没有** PR/merge request 功能。已核实：

- 二进制内无 issue/PR 相关字符串（`Issues`/`PullRequest`/`MergeRequest` 均不存在）
- SSH 命令面只有 `repo` / `settings` / `pubkey` / `token` / `user` 等，无 issue/PR 子命令
- README 只文档化 repository webhooks 与 git hooks
- 仓库历史 427 个 commit，**0 个 merge commit**（此前纯线性直推 main，无分支流程）

因此需要在 git 协议之上自建一层约定，并用服务端钩子把它变成**强制**而非口头规范。

## 验收标准 / Acceptance criteria

- [x] `issues/` + `merge-requests/` 文件约定成文，供其他会话直接遵循
- [x] 服务端 `pre-receive` 钩子：直接推 `main` 的代码变更被拒绝，`mr/*` 分支可推
- [x] `main` 只接受合并提交（评审后合并）与 tracker-only 提交（issue 登记）
- [x] 拒绝时给出可执行的修复指引（含违反策略的文件清单）
- [x] 每次推送决策落审计日志（时间/用户/仓库/ref/decision）
- [x] 指南文档 `docs/sop/soft-serve-issue-mr-workflow.md`，含评审方无克隆读取 MR 的命令
- [x] 端到端演示：本 issue 自身经 issue → MR → 合并 全流程落地

## 不在范围内 / Out of scope

- 改造 soft-serve 源码以原生支持 issue/PR（成本远超收益）
- CI/自动化合并（当前由人/会话显式执行合并）
- 把已有的 `.scratch/<slug>/issue.md` 约定迁移进 `issues/`（`.scratch` 为未跟踪草稿区，保持原样）

## 关联

- 服务端配置与部署：`.soft-serve/README` 与 `.soft-serve/start.sh`
- 标签词汇：`docs/agents/triage-labels.md`
- 既有 tracker 说明：`docs/agents/issue-tracker.md`
