---
id: I-77
title: 生产运维收尾 — 崩溃恢复重启入口、沙箱周期探活、资源隔离与可观测性（P0-4）
labels: [needs-triage]
author: lead
created: 2026-10-21
status: open
---

## 背景 / What & Why

迁移自 TODO/unsolved_issue.md 的 P0-4（原文见 docs/archive/todo/unsolved-issue-2026-09-07.md）。
2026-08-19 起 checkpoint task 块地基已落地（I-11，ADR-039），但进程重启后的恢复入口、
沙箱周期探活、资源隔离与可观测性仍未完成，生产持续运行能力不足。

## 当前状态（2026-10-21 核实）

- 崩溃恢复：checkpoint(task) 与 _resume_events 幂等配对已有；无重启恢复入口 API。
- 探活：模型服务已有 30s 周期探活（ModelServicePool）；沙箱只有 get_or_create 就绪探针。
- 孤儿清理：已实现（reap_orphans）。
- 资源隔离：无 cgroup 限制。
- 可观测性：无 metrics/tracing/结构化日志（I-21 曾暂缓）。
- Docker 后端：无集成测试（tests 只有 backend 配置测试）。

## 验收标准

- [ ] 恢复入口：给定含 checkpoint(task) 且尾部有未完成 tool_called 的 session，新进程可从最后未完成点续跑；含「子进程崩溃 → 新进程恢复」集成测试
- [ ] 沙箱周期探活：复用 ModelServicePool 模式，unhealthy 标记 + 审计事件
- [ ] cgroup 资源隔离：完成可行性评估并记录决策（实现或明确不做及理由）
- [ ] 可观测性：确定最小范围（至少结构化日志或 metrics），落地并测试
- [ ] Docker 后端集成测试可运行（本地或 CI 标记 skip 条件）

## 不在范围内

- 锁文件/方言层变更；UI

## 关联

- ADR-039（任务边界与崩溃恢复）、ADR-040（沙箱锚点生命周期）、ADR-021? 不适用
- issues/python-harness-issues.md L1–L6（2026-10-19 遗留）
- I-11（历史，checkpoint task 块已落地）
