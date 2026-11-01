> **⚠️ Superseded by [ADR-035 (Gateway)](035-gateway-authorization.md)** — retained for historical context only.

# ADR-033：工具错误软继续 — Agent Loop 不因工具调用失败而终止

**日期**: 2026-05-31  
**状态**: 已采纳

## 上下文

AgentRuntime 的 `_execute_single_tool` 在工具调用失败时原设计为硬终止：返回 `{"status": "failed"}` → `_agent_loop` 退出 → `run_loop` 抛 `RuntimeError`。这意味着一次工具调用异常就会杀死整个 Agent 会话。

此设计有三个问题：

1. **违背 LLM 自修正机制**。OpenAI/Anthropic 的函数调用范式都假设工具失败时 LLM 能看到错误并重试。杀死 loop 等于剥夺了 LLM 的纠错机会。

2. **与框架分层原则冲突**。ADR-006（显式编程）规定框架不做控制流判断；ADR-017（SDK 编排模式）规定框架只执行一次静态 DAG。工具结果的好坏应由 Gate 裁决，不由框架裁决。

3. **与 ADR-013（工具可见性 ≠ 执行授权）矛盾**。权限拒绝时 Gateway 拦截了执行但不应杀进程——Agent 应能看到 `[tool error]` 并尝试其他工具。

## 决策

**所有工具级错误路径改为软继续（soft-continue）。Agent Loop 的终止条件缩减为三个框架级保护：**

| 终止条件 | 性质 |
|---------|------|
| `finish_reason == "stop"` | LLM 自然完成 |
| `timeout` 超时 | 框架保护 |
| `max_iterations` 耗尽 | 框架保护 |

### 六条软继续路径

| 路径 | 事件 | 审计 | LLM 上下文 |
|------|------|------|-----------|
| 工具成功 | `tool_called` + `tool_result` | `tool_result` (info) | tool result |
| 工具异常 | `tool_called` + `tool_result` + `agent_failed` | `tool_error` (error) | `[tool error] Tool error: ...` |
| 权限拒绝 | `tool_called(allowed=False)` + `agent_failed` | `tool_permission_deny` (warn) | `[tool error] Cap token denies 'xxx'` |
| 未知工具 | `tool_called` + `agent_failed` | `tool_error` (error) | `[tool error] Unknown tool: xxx` |
| 原子 Gate 拒绝 | `tool_called(reason=atomic_gate_rejected)` + `agent_failed` | `atomic_gate_rejected` (warn) | `[tool error] Human gate rejected...` |
| 结构化返回 `ok=False` | `tool_called` + `tool_result(content)` + `agent_failed(error)` | `tool_result` (warn) | tool result + `[tool error]` |

### 结构化返回约定

工具可以选择返回 `dict` 携带 `ok` 字段。框架识别此模式：

```python
# ok=True → 仅 tool_result
{"ok": True, "content": "all good", "exit_code": 0}

# ok=False → tool_result + agent_failed（loop 继续）
{"ok": False, "content": "Missing files", "error": "Only 2 files, expected 3", "exit_code": 1}
```

非 dict 返回和 dict 不含 `ok` 字段时，行为不变（`str()` 进 `tool_result`）。

## 理由

1. **LLM 自修正**。工具失败 → `[tool error]` 在上下文中 → LLM 分析原因 → 调整参数重试。这是 Agent 区别于传统脚本的核心能力。

2. **框架不判断语义**。`ok=False` 不代表"任务失败"，只代表"工具认为结果不符预期"。最终判断由 Phase/Node 级 Gate 完成。框架只负责把信息无损传递给 LLM 和 Gate。

3. **审计可追溯**。每条工具路径都有审计事件，即使 loop 继续运行，事后可完整回溯每次工具调用的成败。

4. **防御仍在**。`max_iterations` 和 `timeout` 确保 LLM 不会无限自修正。极端情况下框架仍会终止。

## 后果

- `agent_failed` 事件语义变更：从"终止信号"降级为"LLM 上下文线索"。不再触发 `run_loop` 的 `RuntimeError`。
- `_build_messages` 新增第六分支（`agent_failed`），输出 `[tool error]` 格式消息。
- `run_loop()` 成功时返回 `None`（未变），失败时仅由上述三个框架保护触发。
- APP 层可通过 `_retry_error` 机制（ADR 未编号，见 Issue 2 修复）向重试 Agent 注入失败上下文。
- 所有现有测试改为验证事件序列而非异常抛出。

### 2026-10-19 注记（denial / 未知工具 → tool_result 回灌，soft-continue 真正闭环）

上表「权限拒绝 / 未知工具」路径原先只发 `tool_called(allowed=False)` +
`agent_failed`；`_build_messages` 会把 `agent_failed` 映射为 `[tool error]` 的
**user** 消息。模型虽能读到错误，但函数调用协议不完整（`assistant.tool_calls`
没有配对的 `tool` 消息），不同 provider 对孤立 tool_call 的处理并不一致；
ADR-002 的 pi agent 依赖 `read_before_edit` veto，实测模型会重复被拒调用直到
`max_iterations`。

修复（行为）：

- denial / unknown tool / guard-compile-failure 三条路径在发事件后**追加
  `tool_result`**（`parent_event_id = tool_call id`，内容 `[tool error] …`），
  既有 `tool_called` + `agent_failed` 观测事件保留；
- 这些 `agent_failed` 事件带 `represented_by_tool_result: true`，
  `_build_messages` 跳过其 user 映射，避免同一条错误在上下文中出现两次；
- 结果：`assistant.tool_calls` 与 `tool` 消息成对，模型看到一次错误即可自纠；
  事件序列与审计字段不变。

测试：`tests/test_denial_feedback.py`（denial / unknown tool 成对且仅出现一次）、
`tests/test_pi_security.py::test_denial_feedback_lets_the_model_self_correct`
（真实 sandbox + `read_before_edit` 闭环）。
