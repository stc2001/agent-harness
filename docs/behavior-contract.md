> **⚠️ Superseded by [ADR-035 (Gateway)](../../adr/architecture/035-gateway-authorization.md)** — retained for historical context only.

# Agent Harness — 行为契约文档

> **生效版本**: 2026-05-19（329 passed）
> **目的**: 记录系统确定性行为，作为未来重构的约束边界。打破这些契约 = 需要 ADR 或显式变更。
> **与 ADR 的区别**: ADR 记录"为什么这样设计"；本文档记录"设计完成后系统如何表现"。

---

## 1. Gate 层级与执行顺序

### 1.1 层级定义

| 层级 | 触发时机 | 阻塞范围 | 配置位置 |
|------|---------|---------|---------|
| **节点级 Gate** | 每个 `level` 执行完成后 | 当前 level 前进到下一 level | `DAGNode.human_gate = True` |
| **Phase 级 Gate** | 所有 level 执行完成后 | Phase 完成 | `Phase.human_gate = True` |
| **原子级 Gate**（待实现） | 单次工具调用前 | 该工具调用 | `AgentSpec.permissions.tool_gates` |

### 1.2 执行顺序

```
Level 0 执行 ──→ Level 0 Gate 检查 ──→ Level 1 执行 ──→ Level 1 Gate 检查 ──→ ... ──→ Phase Gate
        ↑                    ↑
   并行/串行            串行逐个节点
```

**契约**: 节点级 gate 按 level 串行检查，Phase 级 gate 最后检查。双层 gate 同时存在时，两者都触发。

---

## 2. 同一 Level 内节点 Gate 行为

适用场景: `group_by_level` 将节点分到同一 level，执行完成后进入 gate 检查。

| 节点 A gate | 节点 B gate | A 决策 | B 决策 | A 执行阻塞 B 执行 | Level 结果 | 说明 |
|:-----------:|:-----------:|:------:|:------:|:-----------------:|:----------:|------|
| 无 | 无 | — | — | ❌ 否 | 全部通过 | 无 gate，直接前进 |
| 有 | 无 | approve | — | ❌ 否 | 全部通过 | B 已跑完，等 A gate 后一起前进 |
| 有 | 无 | reject | — | ❌ 否 | **FAILED** | A reject → 整个 level 失败，B 结果丢弃 |
| 有 | 有 | approve | approve | ❌ 否 | 全部通过 | 串行 gate 检查，逐个 resolve |
| 有 | 有 | approve | reject | ❌ 否 | **FAILED** | B reject → 整个 level 失败 |
| 有 | 有 | reject | (未检查) | ❌ 否 | **FAILED** | A 先 reject，B 的 gate 不会触发 |

**契约**: 同一 level 内，节点执行并行，gate 检查串行。任一节点 reject → 整个 level 失败，后续 level 不执行。

---

## 3. 串行 Level 节点 Gate 行为

适用场景: `depends_on` 链导致节点分到不同 level（如 A → B → C）。

| 当前 Level | 节点 gate | 决策 | 下一 Level 执行 | 说明 |
|:----------:|:---------:|:----:|:---------------:|------|
| Level 0 [A] | 无 | — | ✅ 继续 | 无 gate，直接进 Level 1 |
| Level 0 [A] | 有 | approve | ✅ 继续 | A gate 通过后进 Level 1 |
| Level 0 [A] | 有 | reject | ❌ 终止 | Phase 失败，Level 1 不执行 |
| Level 0 [A] | 有 | modify | ✅ 继续 | `amend_input` 写入，进 Level 1 |

**契约**: 前一 level 失败 → 后续 level 不执行。`amend_input` 通过 `_amend_inputs` 字典传递到下一 level 的所有节点。

---

## 4. Phase 级 Gate vs 节点级 Gate 交互

| 节点级 gate | 节点决策 | Phase 级 gate | Phase 决策 | 最终状态 | 说明 |
|:-----------:|:--------:|:-------------:|:----------:|:--------:|------|
| 无 | — | 无 | — | COMPLETED | 无任何 gate |
| 有 | approve | 无 | — | COMPLETED | 仅节点级 gate |
| 有 | reject | 无 | — | FAILED | 节点级 reject 直接终止 |
| 无 | — | 有 | approve | COMPLETED | 仅 Phase 级 gate |
| 无 | — | 有 | reject | FAILED | Phase 级 reject 直接终止 |
| 有 | approve | 有 | approve | COMPLETED | 双层 gate，都 approve |
| 有 | approve | 有 | reject | FAILED | Phase 级 gate 在节点级之后 |
| 有 | reject | 有 | (未触发) | FAILED | 节点级 reject 已终止，Phase 级 gate 不检查 |

**契约**: 节点级 gate 先检查（每个 level 结束后），Phase 级 gate 最后检查（所有 level 完成后）。任一 reject → 终止。

**注意**: `execute_phase` 对 Phase 级 gate 不阻塞 poll——emit pending 事件后立即返回，status = WAITING_GATE。调用者（Orchestrator）负责 poll resolve。

---

## 5. 跨 Job 隔离（并发行为）

适用场景: 多个 job 同时运行，是否互相阻塞。

| 调用方式 | Coordinator 实例 | Session 隔离 | Job1 gate 阻塞 Job2 | 说明 |
|:--------:|:----------------:|:------------:|:-------------------:|------|
| CLI 两次 `harness run` | **独立**（每次新进程新 client） | ✅ 是 | ❌ 否 | 进程级隔离 |
| SDK 两个 `HarnessClient()` | **独立**（每次新实例） | ✅ 是 | ❌ 否 | 显式创建独立 client |
| SDK 同一 `client.jobs.submit()` 两次 | **共享**（同一 Coordinator） | ✅ 是 | ✅ **是** | `execute_phase` 同步串行 |
| Orchestrator 内部多线程 | **共享**（同一 Coordinator） | ✅ 是 | ✅ **是** | Phase 串行是设计，非缺失 |

**契约**: Coordinator 实例是并发隔离边界。同一实例内，Phase 串行执行（job2 等 job1 的 `execute_phase` 返回）。

**风险**: `_phase_statuses[phase.name]` 按 name 索引。两个 job 的 phase 同名且共享 Coordinator → 状态互相覆盖。

---

## 6. Session 状态机与 Gate 事件

适用场景: `SessionManager` 对事件 append 的状态校验。

| 当前状态 | 允许 append `human_gate_pending` | 允许 append `human_gate_resolved` | 允许 append `agent_completed` | 说明 |
|:--------:|:--------------------------------:|:---------------------------------:|:-----------------------------:|------|
| RUNNING | ✅ 是 | ❌ 否 | ✅ 是 | 正常执行中 |
| WAITING_HUMAN_GATE | ✅ 是 | ✅ 是 | ❌ 否 | 多 gate 排队（重构后新增） |
| COMPLETED | ❌ 否 | ❌ 否 | ❌ 否 | 终端状态，仅允许 `checkpoint_created` |
| FAILED | ❌ 否 | ❌ 否 | ❌ 否 | 终端状态 |

**契约**: `WAITING_HUMAN_GATE` 状态下允许新的 `human_gate_pending`（支持并行 level 多 gate 排队），但只允许 `human_gate_resolved` 和 `checkpoint_created` 解除等待。

---

## 7. 工具调用三层防线

| 防线 | 层级 | 控制点 | 失败结果 | 配置位置 |
|:----:|:----:|:------:|:--------:|:--------:|
| 第一层 | LLM 可见性 | `_call_llm` 过滤 `tools` 参数 | LLM 看不到工具，不会规划 | `AgentSpec.tools` |
| 第二层 | 执行权限 | `Gateway.authorize(ctx)` | `AuthorizeResult.denied` | `AgentSpec.permissions` |
| 第三层 | **原子级 Human Gate**（待实现） | 工具调用前回调 | `ToolGateRejected` 异常 | `AgentSpec.permissions.tool_gates` |

**当前状态**: 第一、二层已实现；第三层在 Refactor 001 第二步规划中。

---

## 8. 并行执行隔离

适用场景: `ThreadPoolExecutor` 执行同一 level 的多个节点。

| 维度 | 隔离保证 | 实现方式 |
|------|---------|---------|
| 子 session | ✅ 隔离 | 每个并行节点分配独立 child session ID |
| 事件不串扰 | ✅ 隔离 | 子 session 事件写子 session，父 session 仅收 `node_result` |
| Agent 实例 | ⚠️ 共享 | `AgentInstancePool.acquire()` 复用实例，但不同 session |
| Gateway | ✅ 隔离 | 每个工具调用独立授权，scope = node session |

**契约**: 并行节点的执行环境隔离（独立 session + token），但 Agent 实例池复用。

---

## 9. 变更此文档的流程

```
行为变更发现
    ↓
是架构决策变更？ → 写 ADR → 更新本文档
    ↓ 否
是 bug 修复导致行为变化？ → 更新本文档 + 更新测试
    ↓
是新增功能？ → 写测试 → 更新本文档
```

**规则**: 任何破坏上述真值表的 PR 必须同时更新本文档，否则 CI 视为契约破坏。
