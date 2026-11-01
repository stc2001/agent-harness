---
id: I-76
title: 整理 TODO/ 历史 issue 文档进 issues/（含 2 个放弃计划与 1 份路线图迁移）
labels: [needs-triage]
author: lead
created: 2026-10-21
status: closed
---

## 背景 / What & Why

TODO/ 下有 6 个文档，与 issues/ 新规范（一 issue 一文件 + frontmatter）双轨并存：
unsolved_issue.md / solved_issue.md 的内容与 issues/、python-harness-issues.md 重叠且已过时；
checkpoint_implementation.md 与 molclaw_port_plan.md 的设计前提已被 ADR 推翻；
TODO.md 是旧索引，enhancements.md 是未迁移的路线图。

用户指示：按新规范整理进 issues/；过时设计计划需给出放弃原因；不删除历史内容（归档）。

## 范围

1. 迁移 4 个未完成项为独立 open issue：I-77（P0-4 运维收尾）、I-78（P1-8 权限字段）、
   I-79（P2-3 SDK 审核）、I-80（P2-4 read_subagent）。
2. 2 个过时设计计划各建一个 wontfix issue 说明放弃原因：I-81（checkpoint）、I-82（MolClaw）。
3. 路线图迁移为 I-83（增强路线图盘点）。
4. 历史原文归档到 docs/archive/（solved_issue、unsolved_issue、TODO 索引、两个计划、
   enhancements 原件），随后删除 TODO/ 目录。
5. 更新引用：python-harness-handoff.md、python-harness-implementation-guide.md、
   python-harness-issues.md、docs/sop/soft-serve-issue-mr-workflow.md、docs/learning/README.md。

## 验收标准

- [ ] TODO/ 目录不再存在；6 个文档内容可在 docs/archive/ 找到
- [ ] issues/ 新增 I-77…I-83，frontmatter 字段/状态/标签合法（triage-labels 六标签）
- [ ] I-81/I-82 明确写出放弃原因并引用 ADR（ADR-039 / ADR-038）
- [ ] 全仓库 grep 不再有指向 TODO/ 的链接
- [ ] repo_audit.py 对新 issue 无 schema WARN

## 不在范围内 / Out of scope

- python-harness-issues.md 本身（I-15…I-43 与 2026-10-19 遗留登记）保持现状，后续单独迁移
- 实现 I-77…I-80 的代码
- 历史 git 提交改写

## 关联

- 用户直接指示（2026-10-21）
- I-74（repo-hygiene-audit 报 docs 漂移）、I-75
