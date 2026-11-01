---
id: I-84
title: 迁移 python-harness-issues.md 到 issues/ 并修正 agents 文档漂移
labels: [needs-triage]
author: lead
created: 2026-10-21
status: closed
---

## 背景 / What & Why

用户指示：
1. python-harness-issues.md 是未完成 issue 清单，移动到 issues/ 下即可；
2. docs/agents/issue-tracker.md 仍在写 .scratch/<feature>/issue.md 旧约定，应换成 issues/ 新约定；
3. docs/agents/domain.md 需修订（ADR 数量/类型已漂移）。

## 范围

1. git mv python-harness-issues.md -> issues/python-harness-issues.md（内容不变）。
2. 重写 docs/agents/issue-tracker.md：issues/<ID>-<slug>.md + merge-requests/<ID>.md，
   frontmatter/状态机/标签/分支/推送策略，legacy 文件说明。
3. 修订 docs/agents/domain.md：ADR 实际 46 篇、9 个非空类型目录、编号缺口与 002 跨类型重复。
4. 更新 issues/README.md：当前最大 I-83（下一个 I-84），新增 legacy backlog 行。
5. 更新活跃文档引用（docs/sop、docs/developer-sdk-contracts.md、docs/learning/README.md、
   python-harness-handoff.md、python-harness-implementation-guide.md、issues/I-77）。
   历史记录（已关闭 issue/MR、docs/archive/）保持原样。

## 验收标准

- [ ] python-harness-issues.md 不再位于根目录；issues/python-harness-issues.md 存在且内容不变
- [ ] docs/agents/issue-tracker.md 不再出现 .scratch 旧约定，改为 issues/ + merge-requests/
- [ ] docs/agents/domain.md 写明已创建 46 个 ADR（与 find docs/adr -name '*.md' 一致）
- [ ] repo_audit.py 的 docs-drift WARN 清零（issue-tracker 与 ADR 计数两项）
- [ ] 活跃文档无指向根目录 python-harness-issues.md 的引用

## 不在范围内

- 把 issues/python-harness-issues.md 内容拆成 I-NN 文件（后续按需迁移）
- 历史记录中的旧路径引用
- 代码变更

## 关联

- 用户直接指示（2026-10-21）
- I-76（TODO 迁移）、I-83（路线图）
