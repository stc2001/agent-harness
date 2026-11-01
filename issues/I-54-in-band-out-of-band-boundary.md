---
id: I-54
title: 带内/带外边界分离（D 组）— 退役翻译 app 专用，wontfix
labels: [wontfix]
author: admin
created: 2026-10-19
status: wontfix
---

## 背景 / What & Why

D 组原计划把「参考译文/观测产物移出 agent 工作区、源只读挂载、产出可写」的
带内/带外边界迁移到 OH。核对两个提交的实际文件范围后确认：它们只改已退役的
翻译 app，不属于 OH 共享契约。

## 提交文件范围

- `4a8ca55`（refactor(boundary)）：
  - 106 个本地数据/产物文件（`agent_ws/`、`reference/`、`app_run/`）
  - `scripts/translate_longhorizon.py`
  - `tests/test_workspace_boundary.py`
- `2c3db49`（refactor(agent)）：仅 `scripts/translate_longhorizon.py`

## 裁定 / Decision

- OH 无翻译 app 路径（ADR-002 已退役该 app；I-52 / I-59 已明确过滤本地脚本与
  翻译专用测试）。
- D 的边界能力已由 **I-52** 的 harness 级 `mode:"ro"` 挂载 + Landlock 内核 e2e
  覆盖（`tests/test_access_boundary.py`、`tests/test_exec_guard.py`）。
- 因此 **D 不产生 MR**，本 issue 以 `wontfix` 关闭。

## 验收标准 / Acceptance criteria

- [x] 已核对 D 组两个提交的文件范围（如上）
- [x] 确认 OH 无对应 app 路径
- [x] 确认边界能力由 I-52 覆盖
- [x] 不产生 `mr/I-54-*` 分支

## 不在范围内 / Out of scope

- 翻译 app 的目录布局、数据与产物
- 翻译专用 `test_workspace_boundary.py`

## 关联

- 覆盖该能力的 MR：I-52（`tests/test_access_boundary.py`、`tests/test_exec_guard.py`）
- 原始提交：`4a8ca55`、`2c3db49`
