# Agent Harness — 项目待办索引

> **渐进式披露**：本文档是入口索引。根据你的角色选择下一层文档：
> - 开发者 → [`unsolved_issue.md`](./unsolved_issue.md)（当前缺口与阻塞项）
> - 架构师 → [`enhancements.md`](./enhancements.md)（未来设计与 Phase 2+ 规划）
> - 维护者 → [`solved_issue.md`](./solved_issue.md)（历史里程碑与已闭合项）

---

## 项目概览

**代码基准**：2026-08-19，**466 passed / 2 skipped**（python-harness 扩展 I-01…I-11 完成后；网络依赖 1 测试剔除）
**ADR**：41 个（最新 044 Stop 双轨 / 043 生命周期 / 042 hooks / 041 reasoning 契约）
**最新审查**：python-harness I-11 wake 机制（收件箱 + worker + ADR-039 幂等配对 + checkpoint task 块）

---

## 当前状态速览

| 维度 | 状态 |
|------|------|
| **P0 阻塞项** | 1 项未闭合（生产运维缺口：进程恢复/健康检查/metrics） |
| **P1 质量项** | 1 项未闭合（P1-8 Agent Spec 权限字段） |
| **P2 清理项** | 5 项（死代码、SDK 审核、read_subagent 等，见 unsolved_issue） |
| **P3 远期** | 分布式调度、AI4S 验证框架（未启动） |

---

## 子文档导航

### [unsolved_issue.md](./unsolved_issue.md) — 当前未解决
**内容**：P0 阻塞缺口、P1 质量债务、SDK 原子能力缺失、功能依赖链
**何时阅读**：准备编码前，确认当前阻塞项
**更新频率**：每次编码迭代后更新（勾选完成项）

### [enhancements.md](./enhancements.md) — 未来增强
**内容**：P2/P3 特性设计文档、SDK 编排模式库、原子级 Human Gate 方案、MCP 混合架构、MapReview 场景 API
**何时阅读**：规划新特性、设计评审、架构决策前
**更新频率**：设计确认后追加，实现后归档到 solved_issue

### [solved_issue.md](./solved_issue.md) — 已解决
**内容**：Phase 1 MVP 9 模块、Phase 2 架构解耦、7 个工具生态、审计日志 16 点覆盖、CLI 13 命令、SDK 重构、Refactor 001–003（Coordinator/AgentRuntime/Orchestrator 三核心重构）
**何时阅读**：了解项目历史、确认某功能是否已实现、新成员 onboarding
**更新频率**：只追加，不修改（历史记录）

---

## 约定

- **P0**：阻塞 MVP 闭合或影响安全生产环境可用
- **P1**：提升质量、覆盖、安全加固，MVP 可暂时没有但不该拖
- **P2**：Phase 2 功能，部分已提前完成
- **P3**：Phase 3 / AI4S 上层应用（远期）

---

## 建议执行顺序

### 本轮优先（P0/P1 闭合）

```
P1-8  Agent Spec session_write           ~15 行     Validator 只读约束字段
P0-4  生产运维能力                       ~300 行     进程恢复 + 健康检查 + metrics
```

### Phase 2 特性（设计文档已就绪）

```
P2-5  SCP/MCP client 适配器              ~120 行    Per-spec 工具集暴露
P2-6  Gate 即节点重构                    ~60 行     wait_gate 节点替代 Hooks
```

### Phase 3 远期

```
P3-1  分布式 Scheduler + SessionStore
P3-2  AI4S 验证框架（实验数据契约 + 可重复性审计）
```

---

## 功能依赖链

```
~~Skills 系统~~ ← ✅ 已完成（2026-05-19）
~~MCP 工具~~  ← ✅ ToolRegistry 实现（DI + 内置工具 + declare_output_path/install_skill）
~~原子 Human Gate~~ ← ✅ 已实现（Hook + Event + 统一路由 + 10 测试）
~~SDK heartbeat~~ ← ✅ 已实现（patterns.heartbeat + 集成测试）
~~hashline 编辑~~ ← ✅ 已实现（hashline_read + hashline_edit + 6 单元测试）
~~gate_event_id~~ ← ✅ 已实现（Pipeline.run() 返回 gate_event_id，SDK 端到端透出，3 层 TDD）
~~retry_from~~ ← ✅ 已实现（Pipeline.retry_from() 从指定 step 恢复，审计事件 + StepResult.retry_from 字段）
~~audit 日志~~ ← ✅ 已完成（全局 + per-session 双写，16 个审计点）
~~配置系统~~ ← ✅ 已完成（SDK 三层查找 + 空 hmac_secret 拒绝启动）
~~CLI 测试~~  ← ✅ 已完成（13/13 命令全覆盖）
```

---

## 快速定位

| 我想知道... | 去这里 |
|-------------|--------|
| 现在有什么 bug / 缺口？ | [`unsolved_issue.md`](./unsolved_issue.md) |
| 某功能是否已经实现？ | [`solved_issue.md`](./solved_issue.md) |
| 新特性怎么设计？ | [`enhancements.md`](./enhancements.md) |
| 当前该先做哪个？ | 本文档「建议执行顺序」 |
| 项目整体健康度？ | 本文档「当前状态速览」 |
