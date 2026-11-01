# ADR-010：原生 tool calling — JSON 文本解析 → OpenAI 兼容函数调用

**日期**：2026-05-14  
**状态**：已采纳  

## 上下文

AgentRuntime 原本通过在系统提示词中描述工具，然后从 LLM 文本输出中用 `_parse_tool_call` 解析 JSON。DeepSeek 有时输出 JSON 格式，有时输出函数调用语法 `spawn_subagent(name="x", task="y")`，导致工具调用不可靠。

## 决策

**切换为 OpenAI 兼容的原生 function calling API。** 删除 `_parse_tool_call` 及其 4 步 fallback。

## 理由

1. **协议保证**。`tools` 参数使 LLM 输出 `tool_calls` 结构化字段，不被文本格式的不确定性影响。DeepSeek 完全兼容 OpenAI 的 function calling API。
2. **特殊 token**。训练阶段 LLM 被 fine-tuned 使用特殊 token 标记工具调用（`[TOOL_CALL_START]` → `[TOOL_CALL_END]`），API 服务端解析后放入 `message.tool_calls[]`。这不是"从文本中找 JSON"，而是"接收结构化数据"。
3. **参数校验**。JSON Schema 定义工具参数类型和必填项，API 层可启用 `strict` 模式强制校验。
4. **KV Cache 友好**。工具定义作为 stable prefix 被缓存，Claude Code 实测 92% prefix reuse rate。
5. **简化代码**。`_parse_tool_call` 的 4 步 fallback（直接 parse → markdown fence → bare fence → brace extraction）全部删除，替换为 3 行 `msg.tool_calls` 读取。

## 后果

- 系统提示词中不再需要工具描述（`tools` 参数承担）
- `_build_messages` 需将 `tool_called`/`tool_result` 事件转为 OpenAI conversation 格式（`assistant` + `tool_calls` + `tool` role）
- `_call_llm` 返回 message object 而非纯文本字符串
- 删除约 30 行 `_parse_tool_call`，新增约 20 行 `_TOOL_DEFINITIONS` + `_build_messages` 格式转换
