---
id: I-81
title: 【wontfix】放弃 Session + Sandbox Checkpoint 实现计划（被 ADR-039 取代）
labels: [wontfix]
author: lead
created: 2026-10-21
status: wontfix
---

## 原计划（2026-05-24）

TODO/checkpoint_implementation.md 提出两层文件系统快照：

- SessionManager.snapshot/restore：把 sessions/<sid>/ + workspace/<sid>/ 打包 tar.gz
- SandboxManager.snapshot/restore：overlayfs 优先、tar 兜底，自动检测支持度

原文归档：docs/archive/plans/checkpoint-implementation.md

## 放弃原因

1. **ADR-039（Accepted 2026-08-03）改写了恢复机制**：任务级崩溃恢复以 append-only
   事件为基础，checkpoint 落为 checkpoint_created 事件 + task 块
   （start_event_id/agent_spec/prompt/max_iterations），不做文件系统快照。
2. **已实现的是事件级 checkpost（I-11）**：SessionManager.checkpoint(task=...) 与
   AgentRuntime._resume_events 的 tool_called↔tool_result 幂等配对已落地；代码中
   没有 snapshot/restore API，也没有 overlayfs/tar 快照实现。
3. **ADR-050 后 Session 已无状态化**：wake = store 读 + 逻辑视图，恢复只需事件流 +
   checkpoint 事件，不再需要 workspace 打包回滚。
4. 计划早于 ADR-038/039/050，Phase 2/3 的集成假设已过时。

## 处置

- 不实现；原文移入 docs/archive/plans/ 保留追溯。
- 若未来确需 workspace 回滚，按新 ADR 重新立项，不复用本计划。

## 关联

- ADR-039、ADR-050、ADR-037（原子追加）
- I-11（历史实现）、I-77（恢复入口剩余项）
