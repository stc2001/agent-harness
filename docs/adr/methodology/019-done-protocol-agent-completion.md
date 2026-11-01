# ADR-019: Agent 完成信号 — finish_reason 为主，DONE 移至 APP 层

**日期**: 2026-05-17（初版），2026-05-19（修订）  
**状态**: 已修订  

---

## 背景（2026-05-17）

Agent 循环中需要一个信号来判断 LLM 是否完成了一项任务。DONE 协议被采用：Agent 被告知任务完成时输出 "DONE"，`_check_done` 检查 `content.upper() == "DONE"`。

## 修订（2026-05-19）

实践中 DONE 协议的致命缺陷：LLM 在同一个 prompt 里收到"调用工具"和"完成后输出 DONE"两条指令，模型倾向于走捷径——直接输出 "DONE" 而不调用工具。在秘密工具测试（Agent 必须调用 `reveal_secret` 才能获得答案）中，DeepSeek 多次直接输出 "DONE" 跳过工具调用。

**修订决策**：`finish_reason="stop"` 成为主完成信号。DONE 移至 APP 层。

## 当前机制

```
_agent_loop:
    msg, finish_reason = _call_llm(messages, tools)
    
    if msg.tool_calls:
        execute tools → continue       # 还有工作
    
    if finish_reason == "stop":
        return completed                # API 层面生成完毕
    
    continue                            # 非 stop（如 length）
```

**API 的 `finish_reason` 含义**：

| finish_reason | 含义 | Agent 行为 |
|:--|:--|:--|
| `"tool_calls"` | LLM 请求调用工具 | 执行工具，继续循环 |
| `"stop"` | LLM 自然停止生成 | 任务完成，退出 |
| `"length"` | Token 超限 | 继续循环，让模型恢复 |

## DONE 的新位置

DONE 不再是框架机制。APP 层如需显式确认：
- 在 system_prompt 或 task_input 中追加 "当任务完成时输出 DONE"
- Agent 的最终响应自然包含 DONE，但循环不依赖它退出

## 删除的内容

- `_check_done()` 方法
- `AgentSpec.completion` 字段（`"done_signal"` / `"single_turn"`）
- executor 默认 system_prompt 中的 `"When finished, respond with DONE"`

## 后果

- 简化了 Agent 循环：仅框架保护终止循环（`finish_reason == "stop"`、`timeout`、`max_iterations`）
- 消除了 DONE 与工具调用竞争导致的过早终止
- APP 层自行决定是否需要显式完成确认
- "幻觉成功"问题由 Human Gate 和 Validator Agent 防御

### 2026-05-31 修订

工具错误不再终止 Agent Loop（ADR-033）。Loop 的终止条件完整列表：

| 终止条件 | 类型 |
|---------|------|
| `finish_reason == "stop"` | LLM 自然完成 |
| `timeout` 超时 | 框架保护 |
| `max_iterations` 耗尽 | 框架保护 |

工具异常、权限拒绝、未知工具、原子 Gate 拒绝、结构化返回 `ok=False` 均不终止循环——仅将错误信息写入 LLM 上下文和审计日志。
