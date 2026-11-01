# ADR-037：Session 事件持久化——原子追加 vs WAL

## Status

Accepted (2026-06-18)

## Context

`SessionStore.append_event()` 将单个事件写入 `events.jsonl`。原实现使用临时文件中间步骤（write tmp → fsync → append main → fsync → delete tmp），类似简化版 Write-Ahead Log（WAL）。

WAL 的价值在于崩溃恢复——如果进程在写入主文件时崩溃，临时文件可作为"已确认但未提交"的证据，重启时重放到主文件。

## Decision

**简化为单次原子写入 + fsync，去掉临时文件和 WAL 恢复。**

```
with lock:
    f.write(line_bytes)   # < PIPE_BUF (4KB)，内核原子
    f.flush()
    fsync(f.fileno())
```

### 理由

1. **写入可重放**：Agent session 日志丢失一行不影响业务状态。Orchestrator 检测 job 异常后重新 submit，agent 重新 execute_task()，session 从零重建。session 不是真相源——Job 才是。

2. **内核级原子性**：单行 JSON（< 1KB）的 `write()` 在小于 `PIPE_BUF`（通常 4KB）时由内核保证原子性——要么全写入，要么全不写。不存在"半行 JSON 破坏文件"的风险。

3. **单进程、单文件、per-session Lock**：不存在多进程并发写同一文件的竞态，不需要 WAL 协调恢复顺序。

4. **恢复成本低**：最坏情况丢失最后一行（一个 event），agent 重跑即可。不需要 WAL 扫描、回放、去重逻辑。

### 拒绝的方案

| 方案 | 拒绝理由 |
|------|---------|
| WAL（`events.jsonl.wal` + recovery scanner） | 过度工程：写入可重放，恢复无价值 |
| 临时文件（`.tmp.{pid}.{tid}`） | 与 WAL 同质：无恢复逻辑，tmp 只是写入两遍 |
| 不加 fsync | 进程崩溃丢缓冲区数据，比丢一行更严重 |

## Consequences

### Positive

- 代码从 15 行减到 4 行
- 消除 3 个 I/O 系统调用（一次 write + 两次 fsync → 一次 write + 一次 fsync）
- 无恢复逻辑、无孤儿文件清理、无幂等检查
- 16/16 测试通过

### Negative

- 进程在 `write()` 和 `fsync()` 之间崩溃时，最后一行可能丢失
- 如果未来改为多进程并发写同一 session，需要重新引入协调机制

### I-31 注记（2026-09-07）

Session 进一步明确为**无状态 append-only 日志**：本 ADR 的原子追加即其全部写入契约；「状态/进度」由消费方（AgentRuntime 生命周期 ADR-043、观测层）从日志与 agent 状态**派生**，日志层不承载状态机（rewind/fork/checkpoint 为日志级运算，见 ADR-039/043 注记）。

## References

### event_id 唯一性（Per-Session）

`event_id` 的唯一性保证范围是**单个 session 内部**，非全局。

- `uuid4()` 生成：碰撞概率 ~0，保证 session 内唯一
- `tc.id`（LLM 工具调用 ID）：每次 API 调用唯一，保证 session 内唯一
- `fork()` 复制 session：故意保留相同 event_id，追溯回源 session

### event_id vs parent_event_id

`event_id` 是事件自身的身份标识。`parent_event_id` 是追溯外键——指向触发当前事件的源事件。

```
tool_called:    event_id = tc.id          ← LLM 的工具调用
agent_failed:   event_id = <uuid>         ← 自己
                parent_event_id = tc.id   ← 链回 tool_called
```

同一个 `tc.id` 可以作为多个事件的 `parent_event_id`（一个工具调用可能产生多个结果事件），这不是重复——`parent_event_id` 是引用，`event_id` 是身份。

跨 session 的 event_id 重复是正常的——不同 session 在不同目录下，JSONL 文件相互独立。类似 git：
两个仓库可以有相同的 commit hash，fork 分支保留父分支的 hash。

运行时不做跨 session 去重。如果未来引入跨 session 事件联合查询，需要在查询层去重。

- ADR-005（JSONL vs SQLite）：event 存储格式决策
- ADR-024（Session 读写双层）：SessionStore 是唯一全量真相源
