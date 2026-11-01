---
id: I-46
title: 修正 SOP §5.2 审计范围的表述（钩子只审计 main 的决策）
labels: [ready-for-agent]
author: admin
created: 2026-10-02
status: closed
---

## 背景 / What & Why

`docs/sop/soft-serve-issue-mr-workflow.md` §5.2 写的是「**每次**推送决策都落盘」，但实际不成立。

钩子 `scripts/soft-serve/pre-receive` 对**非受保护分支**的路径是直接 `continue`，
不调用 `audit()`；只有 `STRICT=1` 且分支名不合规时才写一条 DENY。因此 `mr/*` 分支的
正常推送**不产生任何审计记录**，审计日志实际只覆盖 `main`。

实证（2026-10-02）：I-45 评审结论推送到 `mr/I-45-suffix-round-truncation`（`4a31a52..89890bc`，
成功）之后，`push-audit.log` 的最后一条仍是 03:29:30 的 main 记录；`git ls-remote` 可确认
该分支已推进。**文件在说谎，而日志没记**——这正是指南要消除的那类不精确。

## 验收标准 / Acceptance criteria

- [ ] §5.2 明确写出仅 **main** 的推送决策会被记录，并显式说明 `mr/*` 分支推送不入账
- [ ] 给出一条可自证的命令：推一个 `mr/*` 分支后 grep 日志，确认**无新增**记录（验证的是"不记"这一事实本身）

## 不在范围内 / Out of scope

- **不改变钩子行为**。是否把 `mr/*` 推送也纳入审计是一个独立的设计决策
  （会显著增加日志量），应另开 issue 讨论，不在本次修正内夹带。

## 关联

- 指南：`docs/sop/soft-serve-issue-mr-workflow.md` §5.2
- 钩子：`scripts/soft-serve/pre-receive`
- 发现来源：I-45 合并过程中评审方自述
