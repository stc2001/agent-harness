# ADR-036：Subagent 通信——拉模式 vs 推模式

## Status

Accepted (2026-06-17)

## Context

多 Agent 协作涉及两类场景：

1. **DAG 并行节点**：coordinator 编译静态 DAG，按拓扑序并行执行节点。框架内部管理 session 生命周期，`node_result` 事件自动推送到父 session 是合理的框架职责。

2. **Agent 自主 subagent**：主 agent 通过 `spawn_subagent` 工具运行时创建子 agent。此时框架不应替 agent 决定何时读取结果、是否先摘要再推送。

当前实现将两类场景统一为推模式：`_merge_results()` 在子 session 完成后自动将 `node_result` 写入父 session 的 JSONL。OmO（oh-my-openagent）的设计经验表明，agent 自主 subagent 应采用**拉模式**——agent 通过工具主动读取子 session，而非被动接收推送。

## Decision

### 推拉分离

| 场景 | 调用方 | 通信模式 | 机制 |
|------|--------|:---:|------|
| DAG 并行节点 | coordinator | 推 | `node_result` 事件自动推送（框架职责） |
| Agent 自主 subagent | `spawn_subagent` 工具 | 拉 | agent 通过 `read_subagent` 工具主动读取（APP 职责） |

### `push` 参数

给 `spawn_dynamic_node()` → `NodeExecutor.run()` → `_merge_results()` 链增加 `push: bool = True` 参数。默认 `True` 保持向后兼容（DAG 节点），`spawn_subagent` 的 spawner lambda 传 `push=False`。

```
spawn_dynamic_node(push=False)
  → executor.run(isolated=True, push=False)
    → _merge_results(push=False)
      → SubagentStop hook 仍触发（确定性验证）
      → node_result 不写入父 session
```

### APP 层拉取

框架提供 `SessionStore` 原语后，APP 层自行构建 `read_subagent` 工具：

```python
# APP 层工具定义
@client.tools.register("read_subagent", ..., deps=["store", "parent_session_id"])
def read_subagent(child_id, store=None, parent_session_id=None):
    child_sid = f"{parent_session_id}/{child_id}"
    events = store.read_events(child_sid)
    for ev in reversed(events):
        if ev.event_type == "tool_result":
            return str(ev.payload.get("result", ""))
    return "[subagent completed — no tool_result]"
```

`parent_session_id` 通过工具依赖注入（类似 `execute`、`spawner`）。

### OmO 参考

OmO 的 delegation 工具设计遵循相同原则：

| OmO | Harness 等价 |
|-----|------------|
| `call_omo_agent(name, prompt, run_in_background=true)` → agent ID | `spawn_subagent(name, task)` → `session_id` |
| `background_output(agent_id)` → 拉取结果 | `read_subagent(session_id)` |
| `delegate-task-retry` hook | Gateway PostHook |
| `task-resume-info` hook | SubagentStop hook |

### 为什么不重构为两个独立方法

可选方案：删除 `_merge_results` 中的 push 逻辑，改为两个独立方法。

拒绝理由：

1. **改动半径**：`_merge_results` 包含三种职责——读子 session、
   触发 SubagentStop hook、写 `node_result`。重构为两个独立方法需要
   复制前两步（读 + hook），引入重复代码。

2. **参数 vs 方法数**：一个 `push` 参数修改 1 个签名（`NodeExecutor.run`），
   影响可控。拆为两个方法需要修改调用方 + 新增方法体。参数方案的
   总变更量更小。

3. **测试回归**：默认 `push=True` 保证现有测试零改动。

4. **语义清晰**：`push=False` 直观表达"不推送到父 session"。

> 注：2026-06-18 移除 DAG 编排后，`push=True` 路径（合并回父 session）
> 不再被 coordinator 使用。子 agent 通过 `read_subagent` 工具拉取结果。
>
> 注（2026-08-19，issue I-01/I-03）：`node_result` 残留已全部清理——
> `prompt_assembly.py` 的死分支与 `agent_mgmt.py` 工具描述均已改为
> `read_subagent` 拉取语义（ADR-036）。

## Consequences

### Positive

- Agent 自主控制何时、如何消费子 agent 结果
- APP 可选择先摘要再推送（`summarize_subagent` + `memory-compressor`）
- APP 可选择先拉取全部事件做深度分析
- 框架不硬编码编排策略，遵循 ADR-017

### Negative

- `spawn_subagent` 路径不再有自动 `node_result`，需要 agent 显式调用 `read_subagent`
- APP 层需要自行构建 `read_subagent` 工具（框架提供参考实现但不内置）

## References

- ADR-017：框架不做控制流
- ADR-035：Gateway 授权引擎 + Framework/APP 边界
- ADR-018：Agent 间通信——共享文件系统模型
- OmO 工具链设计：`call_omo_agent` + `background_output` + hooks
