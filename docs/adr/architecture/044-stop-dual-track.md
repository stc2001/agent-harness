# ADR-044：Stop 双轨制——framework 硬闸门与 app 软裁决

## Status

Accepted (2026-08-19)

## Context

当前终止语义单一且不透明：loop 因 finish_reason/timeout/max_iterations 结束（ADR-033），但"谁有权决定 agent 停"没有分层。编排者需要软裁决（"这个任务可以停吗"），框架需要硬保护（沙箱死/超时/预算熔断不可覆盖）。两者应汇入同一终态，且优先级确定。

## Decision

### 1. 双轨定义

| 轨 | 语义 | hook 点 | 可覆盖性 |
|----|------|---------|----------|
| framework（硬闸门） | 沙箱死亡/超时/资源超限/预算熔断 | `agent.stop.framework`（veto 优先） | **app 不可覆盖** |
| app（软裁决） | 编排者判定"是否可以停"，请求-裁决语义 | `agent.stop.app` | 否决 → 续跑/follow-up |

### 2. 语义

- **stop 是请求不是结论**：app stop 走裁决 hook，可否决；framework stop 是硬闸门，不可被 app 否决。
- 两种来源的终止都落到同一终态（STOPPED / COMPLETED / FAILED，见 ADR-043）。
- app 裁决通过 → 优雅停（收尾事件 + 释放），不是 SIGKILL。
- framework 硬闸门触发时，app hook 无拦截窗口（veto 优先于 app 裁决）。

### 3. 复用现有熔断

framework 硬闸门的触发条件复用 `agent_runtime/impl.py` 现有熔断（max_llm_calls/timeout，ADR-033 三终止条件），迁移为 `agent.stop.framework` veto hook 的触发源。

### 4. 明确不做

- 不做跨进程 stop 信号（同进程 hook 调用）。
- 不做强制 SIGKILL 之外的进程级级联（沙箱回收由 SandboxManager 负责，ADR-040）。

### 5. app 裁决的合法 reason 与 SSE 截断语义（2026-08-19 补充）

**核心原则**：app stop 是请求，必须携带 reason；裁决 hook 按 reason 判合法性；无 reason 或理由不明确 = 默认拒绝（fail-closed）——停止执行中的工作是破坏性操作，不确定就不停。

**合法 reason 谱系（默认裁决表驱动，app 可覆盖）**：

| 类别 | reason | 语义 | 默认裁决 |
|------|--------|------|----------|
| 合法 | `objective_met` / `verify_passed` | 目标达成/验证通过，继续跑浪费 | ✅ 优雅停 |
| 合法 | `user_requested` | 人工/HITL 叫停 | ✅ 优雅停 |
| 合法 | `parent_cancelled` | 父任务取消子任务 | ✅ 优雅停 |
| 合法 | `client_disconnected` | 消费端断开（SSE） | ✅ 停并保留可恢复状态 |
| 合法 | `redirect` | 重定向：先停再 wake 续跑（follow-up） | ✅ 停 → follow-up |
| 需确认 | `budget_policy` | app 软预算（≠framework 熔断） | 🔶 转降级/升级路径 |
| 需确认 | `quality_unsatisfied` | 质量门失败 | 🔶 转云端重试/改参 |
| 非法 | 无 reason / `no_reason` | 空白停止 | ❌ fail-closed 拒绝 |
| 非法 | 状态不符 | 非 RUNNING（IDLE 是 no-op，终态后无效） | ❌ 拒绝 |

**framework 熔断 vs app 软预算的分界**：framework 预算超限 = 硬闸门（无需裁决、app 不可拦）；app `budget_policy` = 编排者基于成本账的软策略（必须走裁决）。两轨不重叠。

**SSE 输出流 truncate 语义（两层）**：

1. **传输/呈现层**：消费端随时可关连接截断流（永远合法，无需 reason）——但造成孤儿执行（agent 继续跑无人消费），不推荐单独使用。
2. **执行层**："truncate 输出"不能作为 reason；client disconnect 事件应自动转为 app stop 请求（reason=`client_disconnected`）。裁决依据可恢复性：agent 可恢复（IDLE+wake，I-11）→ 批准并保留状态（后续 wake 从断点续跑、输出流可重订阅）；不可续跑且任务未完成 → 拒绝或转 follow-up。

**依赖**：本节的"保留可恢复状态"语义依赖 I-11（IDLE+wake，ADR-043）——I-11 是 I-12 的前置，两者共同构成干净的 SSE 截断语义。

> **2026-09-20 注记**：app 批准停后本 run 的 `status=stopped`，而实例状态为 **IDLE**（可续跑）——两条轴的完整矩阵见 ADR-043 §7。

## Consequences

- **正面**：终止权责分层清晰（框架保护 vs 编排者策略）；同一终态保证状态机一致；app 可拦截不合理的 stop 请求。
- **代价**：双 hook 点需在 agent loop 的终止路径上精确触发；veto 优先顺序需测试锁定。
- **依赖**：I-09（hook 点）、I-10（STOPPED 状态）、**I-11（worker/dispose 终止路径——I-11 的 `stop()`→`dispose()`（join worker + 清收件箱）是 I-12 双轨的落点，framework/app 裁决在此路径叠加）**。

### 6. I-12 实现注记（2026-08-19，代码已落地）

**双轨落点 = loop 步窗口**（`_agent_loop` 迭代顶部，先硬闸门后软裁决，均先于 `agent.pre_step`）：

| 轨 | 触发 | 裁决 | 结果 |
|----|------|------|------|
| framework 硬闸门 | `_framework_stop_check`：timeout / max_llm_calls / **sandbox_death**（is_alive 探活，hasattr 守卫）是事实 | `agent.stop.framework` veto 链 + **fail-closed 条件复查**（硬条件是事实，链未 veto 也停） | `agent_failed`{reason: timeout/max_llm_calls_exceeded/sandbox_death} → loop failed |
| app 软裁决 | `request_stop(reason)`（仅置待决请求，线程安全；非 RUNNING 或 reason 非法 → 立即拒绝，不触发 hook） | `agent.stop.app` veto 链（步窗口消费） | 批准 → `agent_completed`{reason:"stopped", stop_reason} → loop **stopped**；否决 → `agent_stop_denied`{reason, verdict} → 续跑 |

- **事件契约**：批准停**复用 `agent_completed`**（reason="stopped"）而非新事件——保证 wake 续跑（I-11）能看到前轮（I-07 回放）；`agent_stop_denied` 为新增控制面事件（build_messages 不映射，不污染 LLM 上下文）。
- **终态映射（同一终态）**：loop 结果 completed / **stopped**（新）/ failed 三种都回 **IDLE**（可恢复——SSE 语义"批准并保留状态，wake 从断点续跑"）；显式 `stop()`/`dispose` 才到 **STOPPED**。`_run_agent_loop` 对 stopped 透出 stop_reason。
- **hook 优先级**：内置 framework veto **最低**（-100）——observe 观察链先跑完，veto 最后必然触发；内置 app 默认裁决表 priority=0。**app 可覆盖**：veto（priority>0）强制否决任何 reason；需确认 reason（budget_policy/quality_unsatisfied）的强制批准用 **around hook**（返回无 veto 的 TriggerResult 绕过内链）。app 无法覆盖 framework：veto 必然触发 + fail-closed 复查双重保证。
- **安装**：`harness/agent_runtime/stop.py` 的 `install_stop_hooks()`（AgentRuntime.__init__ 调用），按**函数身份**幂等——registry.clear()（测试）后可自动重装。
- **§5 reason 谱系数据化**：`STOP_REASONS = LEGAL ∪ NEEDS_CONFIRM`；`validate_stop_reason` 为 request_stop 的 fail-closed 前置校验。
- **SDK 层接线（I-24）**：`client.agents.stop(instance_id, reason=...)`（I-12 已建）+ **`sessions.stop(session_id)` 转调**（原遗留假 API 只写 `session_stopped` 标记；现始终保留标记作审计，且 session 绑定实例且池内 running 时转调 `request_stop("user_requested")`；未绑定/未运行仅标记，向后兼容）——双轨裁决现可从 agent 或 session 两个身份入口发起，CLI `harness stop` 同步生效。

## References

- ADR-042（hooks 模块——agent.stop.framework/app 点名）
- ADR-033（软继续——loop 三终止条件）
- ADR-043（生命周期——STOPPED 终态）
