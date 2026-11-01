# ADR-022: Prompt 组装提权到 SDK — 框架只做执行

**日期**: 2026-05-19  
**状态**: 已接受  

## 上下文

`AgentRuntime._build_messages` 和 `_call_llm` 承担了三个职责：(1) 事件→消息转换 + token 截断；(2) 提示词组装（vars 替换、prompt_append 追加、Skills 元数据扫描）；(3) 工具定义过滤（`get_definitions()` → 按 `spec.tools` 过滤）。后两者是编排决策，不应由执行引擎负责。

## 决策

**提示词组装和工具过滤提取到 SDK 层。** `_build_messages` 退化为纯事件→消息转换 + token 截断，接收已组装好的 `system_prompt` 字符串。`_call_llm` 接收调用方传入的 `tools` 列表。

SDK 新增 `_PromptsClient` 提供三个方法：
```python
prompts.assemble(base, vars, skills, append) → system_prompt
prompts.build_messages(system_prompt, events, ctx_window) → messages
prompts.describe_tools(registry, filter_names) → tool_defs
```

## 理由

1. **框架只做执行**（ADR-006 延伸）。vars 用在哪、Skills 放在 prompt 什么位置、Agent 该看到哪些工具——都是 APP 层的决策，执行引擎不该越权。
2. **可测试性**。SDK 层可以单测 prompt 组装，不需要起 AgentRuntime。
3. **可组合性**。APP 层可以在 `assemble()` 和 `_call_llm` 之间插入自定义逻辑（如动态 Skills 选择、工具权限分层）。
4. **vars/prompt_append 透传保留**。`DAGNode.config` → `AgentRuntime._config` 的管道仍在——实例复用的基础设施不受影响。

## 后果

- `_build_messages(events, system_prompt)` — 新签名
- `_call_llm(messages, tools)` — 新签名
- `execute_task`/`run_loop` 接受 `system_prompt` 和 `tools` 参数
- `executor` 负责在框架入口处组装 system_prompt 和 tools（框架最后一道门）
- 11 个旧测试删除（vars/append/skills/tools_filter 相关），功能已由 SDK 测试覆盖
- SDK 新增 `client.prompts` 子客户端

### 2026-05-31 更新

`build_messages` 正式统一到 `harness/prompt_assembly.py`：AgentRuntime._build_messages 退化为委托调用（64 行 → 3 行）。6 事件分支（含 `agent_failed`、`_retry_error`）集中于单一纯函数，SDK 和 AgentRuntime 双消费者共享同一实现。
