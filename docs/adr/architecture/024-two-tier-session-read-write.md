# ADR-024: Dual-Tier Session Architecture — Store vs Manager

**日期**: 2026-05-19  
**状态**: 已接受  

## 上下文

Harness 有两个会话写入者：

| 写入者 | 写接口 | 事件类型 |
|--------|--------|---------|
| **Coordinator / GateManager** | `SessionManager.append()` | phase_started, human_gate_pending, node_result |
| **AgentRuntime** | `SessionStore.append_event()` | agent_started, agent_thought, tool_called, tool_result, agent_completed |

`SessionManager` 维护了一个内存缓存 `_sessions[sid]["events"]`，用于状态机校验和 rewind/fork 操作。`SessionStore` 是纯文件 I/O，无状态。

AgentRuntime 不通过 SessionManager 写入是故意的：AgentRuntime 只需 SessionStore 依赖即可在沙箱内独立运行，不应耦合状态机逻辑。

当测试用 `session_mgr.get_events()` 读取事件时，它读的是 SessionManager 的内存缓存，看不到 AgentRuntime 通过 SessionStore 直接写入的 `tool_called` / `tool_result` 事件。这导致 secret 工具测试（验证 LLM 是否真的调用了工具）持续失败——代理确实调用了工具，但事件不在缓存中。

## 决策

**SessionManager 和 SessionStore 保持分离。不引入缓存同步机制。** 读取者根据事件写入者选择正确的读取接口：

| 要读的事件来自 | 读接口 |
|---------------|--------|
| Coordinator / GateManager | `session_mgr.get_events()` |
| AgentRuntime | `store.read_events(session_id)` |
| 两者都需要 | 两次读取后合并 |

### 拒绝的方案

1. **让 AgentRuntime 走 SessionManager**：增加 AgentRuntime 对 SessionManager 的依赖，破坏沙箱可移植性。AgentRuntime 不应关心 Job 级状态机。

2. **`get_events()` 自动刷新**：`get_events()` 刷新缓存会撤销 `rewind()`（rewind 只截断内存列表，不重写 JSONL 文件），导致语义不一致。同时引入不必要的性能开销。

3. **统一为单一 Session 接口**：SessionManager 的状态机、rewind/fork、事件校验逻辑与 SessionStore 的纯 I/O 职责正交。强行合并违反单一职责原则。

## 后果

- 读取 AgentRuntime 事件的代码（如测试、SDK 查询）必须使用 `store.read_events()`，不能使用 `session_mgr.get_events()`
- `SessionManager.get_events()` 的文档和测试不承诺包含 AgentRuntime 写入的事件
- Coordinator 级事件和 AgentRuntime 级事件分属两个接口，各自独立进化
- 未来如需统一视图，在调用层合并两份事件列表，不侵入 SessionManager 或 SessionStore

### 状态机终端事件语义（2026-05-31 追加）

`SessionManager._infer_status()` 的终端事件仅包含 `job_completed` 和 `job_failed`。`phase_completed` / `phase_failed` 是 per-phase 里程碑，不触发 session 级别的状态转移。

```python
# session/impl.py
_TERMINAL_SUCCESS = {"job_completed"}   # was {"phase_completed"}
_TERMINAL_FAILURE = {"job_failed"}      # was {"phase_failed"}
```

一个 Job 可以包含多个 Phase，Phase 之间可能存在 Human Gate（审批、修改、重试）。如果 `phase_completed` 被当作 session 终端，则 gate resolution 事件（`human_gate_resolved`）会被 `_validate_transition` 拒绝，导致 session 锁死。

这一语义与双写架构紧密相关：Coordinator 绕过 SessionManager 直接写 SessionStore（如 `_run_job` 的 `phase_completed`），而 fresh SessionManager 从 SessionStore wake 时重新推断状态。只有 `job_completed` 作为唯一终端事件，才能确保无论事件写入顺序如何，session 状态机始终合法。

### fork 事件 ID 全局唯一性（2026-08-19 追加，issue I-02）

**问题**：`SessionManager.fork()` 复制事件时复用原 `event_id`，导致同一事件 ID 存在于新旧两个 session。事件 ID 是溯源链（`tool_called.event_id → tool_result.parent_event_id`）与审计的键，跨 session 复用破坏全局唯一性，且崩溃恢复（ADR-039）按事件配对判定幂等时无法区分事件归属。

**决策**：fork 时重写事件 ID，保证全局唯一：

1. **新 event_id**：每个复制事件生成新 `event_id`（同前缀约定，如 `ev_<uuid>`），其余字段（event_type/timestamp/source/payload）不变。
2. **parent_event_id 同步重映射**：payload 中引用旧事件 ID 的字段（`parent_event_id`，以及 `gate_event_id` 等同类引用）按 `旧 ID → 新 ID` 映射表重写——fork 会话内溯源链（tool_called↔tool_result 配对）必须仍然闭合。
3. **origin 审计**：新 session 的 meta 记录 `forked_from`；可选在 payload 写入 `forked_from_event` 映射，便于审计回溯原始事件。
4. **保持 ADR-024 视图语义**：fork/rewind 仍只操作内存逻辑视图 + 追加写文件；重写只发生在 fork 复制路径，不改变 append-only 物理语义（ADR-037）。

**明确不做**：不做跨 session 的事件 ID 去重校验（写入侧不做全局索引）；唯一性靠 fork 重写保证，不做分布式 ID 服务。
