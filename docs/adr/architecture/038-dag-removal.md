# ADR-038：移除固定 DAG 编排，改为 Agent 自主协调

## Status

Accepted (2026-06-18)

## Context

Agent Harness 1.0 模仿 LangGraph/CrewAI 的固定编排模式：Coordinator 编译 DAG → Scheduler 做 Kahn 拓扑排序 → ThreadPoolExecutor 按层并行执行 → Orchestrator 管理 Job 生命周期。这套设计源于"LLM 能力弱，需要框架替 Agent 做决策"的假设。

AgentScope 2.0（2026-05）明确转向：*"We design for increasingly agentic LLMs. Our approach leverages the models' reasoning and tool use abilities rather than constraining them with strict prompts and opinionated orchestrations."* 去掉了固定 DAG 编排，改回事件驱动 + Agent Team（leader 调用工具 spawn workers）。

这一轮 AI coding agent 的演进也验证了同一方向：Claude Code、Codex、OpenClaw 都不带 DAG 编排——Agent 自己决定顺序、并行、重试。Harness 的 APP 层实测也表明 SDK Pipeline 仅提供了 3 行 API 调用的薄封装（`submit_spec` → `status` → `approve`），APP 的 70+ 行 `_execute_with_auto_gates` 才是真正的编排逻辑。

## Decision

**移除固定 DAG 编排层，框架回归"提供原语，APP 决定调度"的定位。**

移除的模块：

| 模块 | 核心功能 | 替代方案 |
|------|---------|---------|
| `coordinator/` | DAG 编译 + 节点调度 | APP 直接 `pool.acquire()` → `agent.execute_task()` |
| `orchestrator/` | Job 生命周期管理 | APP 管理 job 状态（epoch index 等） |
| `orchestration/scheduler/` | Kahn 拓扑排序 | APP 自行排序（或无需排序——三步串行） |
| `orchestration/executor/` | 节点封装（隔离/spawner） | `pool.acquire(spawner=...)` + `agent.execute_task()` |
| `orchestration/gate_manager/` | Human Gate 事件（Phase/Node 级） | 仅保留 Gateway 的 atomic gate |
| `sdk/patterns.py` | Pipeline/Step DAG | APP 直接三步串行调用 |

保留的核心：

| 模块 | 原因 |
|------|------|
| `pool.py` | AgentRuntime 对象缓存 + sandbox_spawn 授权 |
| `agent_runtime/` | Agent loop + 工具调用 + 事件发射 |
| `sandbox/` | 沙箱隔离 + 生命周期 |
| `session/`, `session_store/` | 事件日志持久化 |
| `gateway/` | 统一授权（atomic gate + sandbox_spawn 规则） |
| `tools/` | 工具实现 |
| `sdk/client.py`（简化） | agents、sessions、gateway、sandboxes、tools |

API 调用从三层减到一层：

```
# 旧：Job → Phase → Node → Agent
orchestrator.start_job(job_spec) → coordinator.execute_phase() → pool.acquire()

# 新：Agent 直接
agent = pool.acquire(spec)
agent.execute_task("generate policy.py", session_id)
```

## 2026-09-10 注记：原语暂缓由 app 需求驱动

边界原则的推论——**未被 app 需求证明的原语不实现**（避免猜测性 API）：

- **I-20 A2A 通信原语**：⏸ 暂缓。agent 间已有其它通信手段（共享文件系统 ADR-018、session events、外部服务），且 `events.send` 与 `agent.pre_step` 插话窗口已存在；缺的仅是「专用 A2A 消息语义」——等 app 出现明确跨 agent 消息契约再评估。
- **I-21 OTel 追踪**：⏸ 暂缓。CLI 观测（logs/session show/agents history，agent·human 双模）已覆盖当前观测需求；I-15 计量在 `llm.call` hook 内可用（app 可自建账本）。引入 collector/server 对当前规模过重；跨服务调用链需求出现时再启（hooks 接入点零侵入保留）。
- **I-28 沙箱绑定策略面 / policy 组作用域**：⏸ 等待 app 层。分组维度（tenant/project/mission 节点/角色）属 app 治理定义；触发条件见 issues I-28。

## Consequences

### Positive

- 代码量减少 ~2000 行（coordinator/orchestrator/scheduler/executor/gate_manager + Pipeline）
- SDK API 简化：去掉 `jobs`、`gates`（node/phase 级）、`Pipeline`
- 框架层不再做控制流决策（完全遵循 ADR-017）
- 对齐 AgentScope 2.0 和 Claude Code 的设计方向

### Negative

- APP 层需自行实现编排逻辑（已有 `hl_pipeline.py` 的 `_execute_with_auto_gates`）
- 并行 DAG 节点的自动调度不再可用（APP 用 `threading`/`asyncio` 替代）
- 多 Phase 串行 + Human Gate 不再内置（APP 用 Gateway 的 atomic gate 替代）

## References

- ADR-017：框架不做控制流
- AgentScope 2.0 设计哲学："rather than constraining them with opinionated orchestrations"
- ADR-036：Subagent 推拉分离（agent 自主 spawn_subagent 已取代 coordinator 的 spawn_dynamic_node）

**2026-10-06 注记（边界判定：记忆的「划分」归 app）**：

ADR-038 的原则「未被 app 需求证明的原语不实现」在此收敛出一条具体判据：

> **有界的是「允许哪些路径/位置」，无界的是「用什么方式利用它们」。机制只持有有界的一侧。**

应用到记忆压缩：
- **harness 持有**：SYS 位置、Task 锚点、**两个固定插槽**（Task 前/后）、`<mem>` 占位符、折叠边界、超上限时丢最旧轮次、审计。
- **app 持有**：何时压缩（水位状态机）、压哪一段、摘要内容、链式摘要。
- **harness 不再持有**：`after_event_id` 相对锚点推断区域（这是**策略**，且是无界推断）。

同源先例：pre-call 权限门从"命令分类启发式"退回到"读优先的咨询性检查"（ADR-051 2026-10-03 注记）—— 两者都是**把无界的推断从机制里移除**。
