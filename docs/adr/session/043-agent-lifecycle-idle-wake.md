# ADR-043：Agent 生命周期 IDLE / wake / dispose

## Status

Accepted (2026-08-19)

## Context

当前 `AgentStatus` 只有 COMPILED（`sdk/models.py`），agent 任务循环结束后实例即失去身份；池的 release/destroy 只是移出字典，没有显式停止语义。编排者对子代理做 follow-up / 重启时缺少机制基础：agent 要么活着跑、要么销毁，没有中间态。

## Decision

### 1. 状态机扩展

`AgentStatus`：`COMPILED → IDLE ⇄ RUNNING → STOPPED`

| 状态 | 语义 | 进入方式 |
|------|------|----------|
| COMPILED | 实例已注册，未运行过 | register |
| IDLE | 任务循环退出后存活，可被唤醒 | loop 退出 |
| RUNNING | 正在执行任务 | 唤醒/启动 |
| STOPPED | 显式停用，资源已释放 | `stop()` / idle timeout |

### 2. IDLE 必须配套 wake + dispose

- **IDLE 不是装饰**：无 wake 的 IDLE 是无意义的中间态。
- **wake**：IDLE agent 收到新消息（`events.send`）→ worker 线程唤醒 → 携带累积事件历史续跑下一轮 loop → 跑完回 IDLE。续跑复用 **ADR-039 恢复重放算法**（从 start_event_id 起、tool_called↔tool_result 幂等配对判定）。
- **dispose/stop**：显式 `stop()` 或 idle timeout（对称沙箱 TTL，参考 ADR-040 生命周期思路）→ STOPPED 并释放资源。回收从隐性（pool 移出）变为编排者显式动作。

### 3. 状态查询 API

`client.agents.status(instance_id)` 返回当前状态。

### 4. 明确不做

- 不做多任务并发（一个 agent 同时只跑一个 loop）。
- 但允许同一个agent模板复用成多个实例同时运行（用户补充）。
  - **注记（2026-09-04，I-25 已落地）**：公共路径已按实例接线——`pool.acquire` 支持 `instance_id` 精确键控（池键 `(name, instance_id)`、`runtime.instance_id` = 注册 inst_id、uuid 互异）；`agents.run`/`events.send`/`stop`/`status` 全部按注册实例精确定位，同模板两实例可并发 run（busy 为实例级）。运行时身份与分派语义详见 ADR-046；测试 `tests/test_instance_dispatch.py`。
- 不做跨进程 wake（单进程 worker 线程）。
- 不做持久化 agent 状态（进程重启后 IDLE 不恢复，需重建实例）。

### 5. I-11 实现注记（2026-08-19，代码已落地）

- **worker 线程懒创建**：首个 wake 时启动（非 acquire 时）——实际内存代价低于 §Consequences 所述"每个 IDLE agent 持有一个线程"；`dispose()` 以 join(timeout=2s) 回收。
- **wake 轮 = 新 Task 轮**：续跑 emit 新 `agent_started(input=msg)`（ADR-039 "Task = 一次 agent_loop"，Session 跨多任务存活）；前轮 `agent_completed` 以 assistant 消息回放（ADR-034 Rev2 / I-07），LLM 看到完整累积历史——`build_messages` 不映射 `user_message` 事件，这是续跑语义的载体。
- **`events.send` = acquire-or-reuse**：session metadata（`instance_id`/`spec_name`）→ `pool.acquire`（I-10 复用守卫）→ `wake()`；首次消息即首轮，未绑定 session 仅追加（向后兼容）。
- **`_loop_lock` 串行化**：worker 轮与直接 `execute_task`/`run_loop` 互斥——"不做多任务并发"的落地，RUNNING 期间来消息排队（FIFO）。
- **`client.agents.status` 池感知**：按 spec_name 查池内实例真实状态，回落 registry 值。

### 6. I-31 注记（2026-09-07，agent 为权威状态层）

Session 不再有独立状态机——**agent 实例状态即事实源**：session 经 metadata 绑定实例，「持有」由池查派生（running/idle → 持有；`stop()`/idle-timeout/destroy 后 → 派生 unbound，日志保留）；任务完成 = loop 退出 → **IDLE（不销毁、可续跑）**，显式 stop/dispose 才回收实例并解绑。观测层（CLI JSON）加 `holder_instance_id/holder_status`。

### 7. RunResult 状态轴注记：`status` × `instance_status`（2026-09-20，I-39 后）

两条轴不可混为一谈：

- `RunResult.status` = **一次 run（一次 task/loop）的结果**：`completed | stopped | failed`
- `RunResult.instance_status` = **实例生命周期状态机的快照**（ADR-043 §1）：`running | idle | stopped`（首次运行前为 `compiled`，该时刻不存在 RunResult）

实现：`_run_agent_loop` 在**锁内、loop 退出（含异常）之后**统一走 `_finish_run()` 置 IDLE 并采集快照；
异常路径同样回 IDLE（I-39 修复——修复前失败任务会把实例永久留在 `running`）。

| `status`（run 结果） | `instance_status`（快照） | 含义 |
|---|---|---|
| `completed` | `idle` | 正常完成；实例可复用（下一次 `run`/`events.send` 会 IDLE→RUNNING） |
| `stopped` | `idle` | app/framework **批准优雅停**（ADR-044 双轨）：本 run 被停，实例**保留在 IDLE**、可续跑（SSE 语义） |
| `failed` | `idle` | loop 异常/失败；实例回 IDLE（可重试）——I-39 修复前会停留 `running` |
| 任意（含 run 进行中） | `stopped` | 显式 `stop()` / dispose 与本次 run 竞争：实例进入 STOPPED（资源释放、不可复用） |
| ——（无 RunResult） | `compiled` | 已注册、尚未运行 |

补充语义：

- `request_stop()` 是**请求**而非结论：在 loop 步窗口由 `agent.stop.app` veto 链裁决；**否决则继续运行**（不会出现 `status=stopped`）。
- `instance_status` 是**返回瞬间的确定性快照**（锁内采集）；需要“实时”状态请用 `agents.status(instance_id)`。
- 与 I-31 一致：session 无状态，状态的唯一权威在 agent 实例（本 ADR）。

## Consequences

- **正面**：编排者获得子代理 follow-up/重启的机制基础；资源回收显式化；生命周期可观测。
- **代价**：每个 IDLE agent 持有一个 worker 线程 + 收件箱（内存占用）；状态机复杂度上升。
- **依赖**：I-11 wake 复用 ADR-039；I-12 Stop 双轨叠加本 ADR 的 STOPPED 终态。

## References

- ADR-039（任务边界与崩溃恢复——wake 重放算法）
- ADR-040（锚点生命周期——idle timeout 对称参考）
- ADR-033（软继续——loop 终止条件）
