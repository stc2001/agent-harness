# Agent Harness — 增强计划

> 本文档记录 Phase 2+ 的设计与功能规划。
> 2026-06-18 更新：移除所有 Pipeline/DAG/Orchestrator 相关内容（ADR-038）。

---

## 工具生态

- [ ] `read_subagent` 工具 — APP 层拉取子 agent session 结果（SessionStore 原语已有）
- [ ] `web_search` / `web_fetch` 工具
- [ ] MCP 协议支持
- [ ] `hashline_read` 结构摘要（AST/标题树）— 方案见 `docs/plans/hashline-structure-read-plan.md`

## Agent 日志与可观测性

- [ ] Agent 执行 metrics（token 消耗、工具调用统计）
- [ ] Context rot 检测 — 上下文窗口事件数/token 数超阈值时触发警告
- [ ] 进程崩溃恢复
- [ ] 可观测性：metrics / tracing

## 上下文工程增强

- [x] 推模式 token 截断 ✅
- [x] Skills 系统 ✅
- [x] Prompt 组装 SDK ✅
- [ ] **拉模式上下文策略** — Agent 通过工具主动拉取需要的上下文（替代全量推送）
- [ ] 记忆层升级：上下文压缩、跨 session 查询、长周期实验断点续传

## SDK API 补充

- [ ] `events.find_by_parent(parent_event_id)` — 按父事件查所有子事件
- [ ] `sessions.rewind(sid, event_id)` — SDK 暴露 rewind
- [ ] `client.sessions.fork(old_sid, new_sid)` — SDK 暴露 fork

## 安全升级

- [ ] Sandbox cgroup 资源限制
- [ ] OpenSandbox 集成（本地 venv 已安装）
- [ ] **信息流控制（Parallax IFC）** — 数据敏感度标签传播 + 出口边界检查（~100 行）
- [ ] MemPrivacy — 边缘-云隐私保护（P3，依赖云端架构）

## 生产运维

- [ ] 进程崩溃恢复
- [ ] 可观测性：metrics / tracing / 结构化日志
- [ ] Docker 后端集成测试
- [ ] 安全审计面板（查询 `audit_log.jsonl` 的 API）

## P3 远期

- [ ] 分布式调度
- [ ] 多租户、配额、全局审计
- [ ] AI4S 验证框架（实验数据契约、验证规则版本化、可重复性审计）
