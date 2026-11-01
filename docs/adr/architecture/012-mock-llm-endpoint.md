# ADR-012：Mock LLM 端点——HTTP 传输层拦截验证上下文组装

**日期**：2026-05-15  
**状态**：已采纳  

## 上下文

AgentRuntime 的消息组装管道分三步：事件→`_build_messages`→`_call_llm`→`openai.chat.completions.create`。现有测试通过替换 `_call_llm` 函数来验证工具执行和循环终止（如 `runtime._call_llm = lambda msgs: _msg_done()`），但此方法跳过了消息体内容验证——无法确认模板变量是否被替换、`tool_call_id` 链路是否匹配、工具是否按 spec 过滤。

## 决策

**在 HTTP 传输层用 `httpx.BaseTransport` 子类拦截请求体，对其进行完整性验证，而非替换 `_call_llm`。**

具体机制：
1. `MockTransport(httpx.BaseTransport)` 实现 `handle_request`，捕获 `json.loads(request.content)` 到列表
2. 通过 `httpx.Client(transport=mock)` → `openai.OpenAI(http_client=client)` 注入
3. 预设响应队列（`[tool_call_response, done_response]`）模拟真实 API 行为
4. 纯函数 `validate_context(requests)` 检查消息体不变式

## 理由

1. **覆盖 `_call_llm` 替换无法覆盖的盲区**。旧方法只验证返回值，不验证 HTTP body 内容——`tool_call_id` 不匹配的 bug 在旧测试中完全不可见。
2. **不启动网络服务**。`BaseTransport` 在进程内运行，无端口竞态，无启动/关闭生命周期。比 Flask/FastAPI 测试服务端轻一个量级。
3. **纯函数验证器可独立测试**。`validate_context` 不依赖 AgentRuntime，可直接用 dict 列表调用。
4. **预设响应队列实现状态机**。外部注入 `[tool_call, tool_call, DONE]` 序列，精确控制 LLM 交互轮次，覆盖多轮对话场景。

## 后果

- 新增 `tests/test_mock_llm.py`（5 个测试），覆盖：tool_call_id 链路、模板变量、prompt_append、工具过滤、多轮上下文累积
- `MockTransport` 不验证 HTTP 序列化（那是 httpx/openai 库的职责），仅验证我们控制的消息体
- 未来添加新工具或提示词功能时，`test_mock_llm.py` 应同步更新验证断
