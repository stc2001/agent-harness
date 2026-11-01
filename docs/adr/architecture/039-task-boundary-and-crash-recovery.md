# ADR-039：任务边界定义与崩溃恢复机制

## Status

Accepted (2026-08-03)

## Context

宿主 Python 进程可能在任何时刻崩溃（OOM、SIGKILL、异常退出）。当前系统对"沙箱锚点"已解决孤儿清理（`SandboxManager.reap_orphans()`），但**任务级崩溃恢复**（P0-4）尚未实现。

要实现崩溃恢复，必须先回答一个前置问题：**"任务"到底是什么？** 当前系统中存在三个容易混淆的实体：

| 实体 | 形态 | 持久化 |
|------|------|--------|
| Session | 一次执行的完整事件日志，状态机 CREATED→RUNNING→COMPLETED/FAILED | ✅ events.jsonl + meta.json + checkpoints/ |
| agent_loop | AgentRuntime 内的一次循环，跑完工具调用直到 finish_reason | ❌ 内存态 |
| execute_task | pool.acquire 后 agent 执行一次任务 | ❌ 内存态 |

关键事实：**框架中"任务"的持久化单位是 Session，但执行单位是 agent_loop**。两者没有严格的一一对应——一个 Session 可能承载多次 loop（heartbeat 模式、compress 子任务、多次 execute_task）。如果恢复机制建立在错误的"任务"定义上，整个机制都会歪。

## Decision

### 1. 任务定义

**Task（任务）** = 一次 agent_loop 执行，由五元组定义：

```
Task = (session_id, start_event_id, agent_spec, prompt, max_iterations)
```

| 字段 | 含义 | 恢复时的作用 |
|------|------|-------------|
| session_id | 事件流的位置 | 恢复后 wake 它 |
| start_event_id | loop 的输入边界，从哪个事件开始 | 定位恢复点 |
| agent_spec | agent 配置（tools/permissions/llm_profile） | 重建 AgentRuntime |
| prompt | loop 的初始输入 | 重建上下文 |
| max_iterations | 执行上限 | 防止僵尸 loop |

**Session 是任务的容器，不是任务本身**。任务完成后向 Session 追加 terminal 事件（agent_completed / agent_failed）。Session 跨多个任务存活。

### 2. checkpoint 需要补充的字段

当前 `checkpoint()` 保存 `session_id + status + 全量事件流 + checkpoint_timestamp`。崩溃恢复需要在此基础上补充**任务边界字段**：

```json
{
  "session_id": "...",
  "status": "running",
  "checkpoint_timestamp": 1234567890.0,
  "events": [...],
  "task": {
    "start_event_id": "ev_xxx",
    "agent_spec": "worker",
    "prompt": "...",
    "max_iterations": 10
  }
}
```

新增 `task` 块：`start_event_id` 标记任务在事件流中的起点，其余字段用于重建 AgentRuntime。恢复时只需读 checkpoint 的 `task` 块 + 事件流，即可重建任务。

### 3. 恢复重放算法

崩溃恢复的核心问题是**找到"最后一个成功点"并保证不重复执行**。事件追溯链（`parent_event_id`）提供了天然答案：

```
tool_called (event_id=E) → 执行工具 → tool_result (parent_event_id=E)
```

**幂等判定**：`tool_called` 有对应 `tool_result`（parent_event_id 匹配）→ 已完成，跳过；否则未完成，重放。

恢复算法：

```
1. wake(session_id)                        # 从 events.jsonl 重建逻辑视图
2. 读 checkpoint，取出 task 块              # 拿到 start_event_id / agent_spec / prompt
3. 重建 AgentRuntime(spec)                  # pool.acquire 或直接构造
4. 扫描 [start_event_id → 末尾] 的事件：
   - agent_completed / agent_failed 存在 → 任务已结束，无需恢复
   - 否则找出最后一个未完成 tool_called（无匹配 tool_result）
5. 从未完成点继续 agent_loop                # 上游事件已重建上下文
```

**为什么用事件流而非计数器**：事件流是 append-only 且不可变，天然是"执行历史"。崩溃只可能发生在两个安全点之间：
- tool_result 写入**前**崩溃 → 该调用未完成，重放重跑
- tool_result 写入**后**崩溃 → 已完成，重放跳过

这与 ADR-037 的"写入可重放"原则一致：事件日志的追加语义保证任何中断点都不会产生半状态。

### 4. 明确不做

- **不做任务级分布式调度**（P3-1）：本机制是单进程崩溃恢复，不是跨机容错
- **不做 anchor 保活**：见 P0-4 拆分表，execute 每次自建 namespace，锚点死亡不影响沙箱可用性


### I-41 注记（2026-09-30）：resume-safe 投影归属调整

「缺 tool_result 的工具轮」是**模型侧形状约束**（assistant 的 tool_calls 必须跟 tool 结果），不是日志事实：

- 该投影从 `AgentRuntime._resume_events` 迁至 **`prompt_assembly.assemblable()`**（提示词形状层）；runtime 只组合 `logical_view` + `assemblable`。
- **范围修正**：原实现只扫描「最后一个 `agent_started` 之后的 tail」，导致**上一轮任务遗留的 dangling 工具轮不被剥离**（TDD 期间发现）→ 改为**全量扫描**。

## Consequences

- **正面**：宿主崩溃后，任务可从最后一个一致点继续，而非从零重建 Session
- **正面**：复用已有事件追溯链（parent_event_id），不引入新的状态追踪机制
- **代价**：checkpoint 格式变更（新增 task 块），需要一次平滑迁移
- **代价**：恢复重放依赖事件流完整性，rewind/fork 后的事件视图需与 checkpoint 的 task 边界保持一致
- **遗留**：agent_loop 需要支持"从指定事件点继续"，当前 `_agent_loop` 从 session 开头重建上下文，需增加从 start_event_id 起的恢复入口

### 5. 实现状态注记（2026-08-19，I-11）

I-11 落地了本 ADR 的**幂等配对部分**与 **checkpoint task 块**，但**活跃 wake 与崩溃恢复是两种语义**，勿混用：

| 语义 | 上下文组装 | 状态 |
|------|-----------|------|
| **活跃 wake（I-11 已落地）** | `_resume_events`：读**全量历史** + 剔除悬挂 tool_called/agent_thought（无匹配 tool_result），LLM 消息链永远合法 | `agent_runtime/impl.py` |
| **崩溃恢复（P0-4 收尾，未落地）** | 从 checkpoint `task.start_event_id` 起重建 + rewind + 重放未完成事件 | 仅地基：`checkpoint(task=...)` + `_current_task` 五元组 |

- §3 的"恢复重放算法"对应崩溃恢复场景（进程重启）；wake 续跑对干净历史为 no-op（前轮有 terminal 事件即"任务已结束"）。
- **遗留项保持开放**：`_agent_loop` 的 start_event_id 恢复入口未加（P0-4 完成时补）。

### 6. I-31 注记（2026-09-07，Session 去状态机化）

Session = **无状态任务容器**（append-only 日志，ADR-037）——本 ADR 的「Task = 一次 agent_loop、Session 跨多任务存活」由**agent 生命周期（ADR-043）与日志派生持有**承载，而非 SessionManager 状态机（其 job 级终态语义随 ADR-038 删 DAG 已成死代码，见 issues I-31）。`rewind/fork/checkpoint` 保留为**日志级运算**；执行守卫以派生持有状态为事实源（绑定 agent RUNNING 时禁止，IDLE/unbound 放行）。崩溃恢复重放（§3）仍在 checkpoint 基础上推进（P0-4）。

## 参考

- ADR-037：原子追加 vs WAL（事件写入可重放）
- ADR-034：内存压缩（compress 子任务与 session 的关系）
- P0-4：健康检查机制拆分（探活 / 崩溃恢复 / 孤儿清理）
