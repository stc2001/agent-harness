# ADR-041：reasoning_content 供应商契约与 thinking 显式配置

## Status

Accepted (2026-08-19)

## Context

DeepSeek 思考模式（V4 默认开启）是硬性 API 契约：两个 user 消息之间，若模型**进行了工具调用**，上一轮的 `reasoning_content`（思考内容）**必须回传**给 API，否则报 "must be passed back"（[官方文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)）。未进行工具调用的轮次**无需回传**，传入会被忽略（省略以省 token）。

当前 harness：`agent_thought` 事件只存 content/finish_reason/tool_calls，**未捕获 reasoning_content**；`build_messages` 不把 agent_thought / agent_completed 转成 assistant 消息——模型文本输出与最终答案都不回传，长程工具调用必然触发供应商报错，跨任务续跑失忆。

参考实现：DSH deepseek 适配器 `serializeAssistant`（`dsh-llm-deepseek/lib/index.js:48-61`）——`toolCalls.length > 0 && reasoning.length > 0` 才带 `reasoning_content`，否则省略。

## Decision

### 1. 显式配置，不硬编码

`AgentSpec` 新增两个显式字段（默认值明确，框架不硬编码任何供应商行为）：

| 字段 | 默认 | 语义 |
|------|------|------|
| `reasoning_passback` | `True` | 是否回传 reasoning_content（供应商契约开关） |
| `thinking` | `False` → **`None`**（见 §2b Rev） | 是否开启思考模式（透传 `extra_body` 到供应商） |

### 2. 回传规则（适配层契约）

序列化 assistant 消息时：

```
有 tool_calls 且有 reasoning_content → 带 reasoning_content 字段
有 tool_calls 无 reasoning_content → 不带（省略）
无 tool_calls → 不带（纯 assistant 消息）
```

### 2a. Rev（2026-09-07，V4 thinking 实况修订）

V4 默认 thinking 时**每条 assistant 回复都可能携带 reasoning_content（含最终答复轮）**，provider 要求此类消息在多轮中原样回传——「仅工具轮才回传」的旧规则导致最终轮（无 tool_calls）续跑触发 `reasoning_content must be passed back` 400。修订规则：

```
带 reasoning_content 且 reasoning_passback=true → 无条件带 reasoning_content（工具轮与纯文本/最终轮一致）
无 reasoning_content → 不带
```

实现：`build_messages` 对 agent_thought（任意轮）回传 reasoning；`agent_completed` 最终答复与同轮 agent_thought 的 reasoning **合并为单条 assistant**（跳过前导 content 重复——还原 provider 原始消息形态，避免无 reasoning 副本触发 400）。测试：`tests/test_prompt_assembly.py::TestReasoningPassbackMultiTurn`（含单条合并/无工具轮/工具轮/关断控制）。

### 2b. Rev（2026-10-02，thinking 事实源与三态修订）

**实测推翻 §1 的「默认 `False` 即可表达全部语义」假设。** 本地 llama.cpp 后端上
`AgentSpec.thinking=True` **完全无效**：框架发出 `extra_body {"thinking": true}`，
而该后端**静默忽略**此键（实测 `true`/`false` 输出字节级相同），真正开关是
`chat_template_kwargs.enable_thinking`。更糟的是 profile 的
`chat_template_kwargs.enable_thinking: false` 会**静默压过** spec 的 `True`。

修订（完整规则见 **ADR-054**）：

1. **`thinking` 默认值 `False` → `None`**（三态）：`None`=未表态（按 profile）、
   `True`/`False`=显式（覆盖 profile）。`False` 无法与「未表态」区分，是本次缺陷的机制。
2. **spec 与 profile 的优先级**：显式 spec 覆盖 profile；未表态时 profile 决定。
   推荐用法是**不同 agent 用不同 profile**（如主 agent 与总结器各一个），此时不产生冲突。
3. **§1「不硬编码供应商行为」的落实方式**：厂商键名由 **profile** 声明，框架只做
   「显式则覆盖」的通用优先级处理，不出现厂商名分支。

### 3. 捕获与用途边界

- `agent_thought` payload 增加 `reasoning_content` 字段（非流式取 `msg.reasoning_content`；流式累积 delta）。
- reasoning_content **只用于回传与审计（OTel），绝不塞回普通 content 消息回放**（费 token 且 API 不接受）。
- `agent_completed`（最终答案）序列化为 assistant 消息回传，跨任务续跑不失忆。

### 4. 明确不做

- 不做流式推理的持久化增量存储（仅累积最终值进事件）。
- 不硬编码供应商特定开关（thinking 行为由显式配置控制，对任意 OpenAI 兼容供应商生效）。
  **适配层边界（ADR-054 澄清）**：厂商无关的 `thinking` 字段照发不变；当某后端的渲染器
  需要另一个键（如 llama.cpp 的 `chat_template_kwargs.enable_thinking`）时，由 **profile
  声明该映射**（profile 的 `chat_template_kwargs` 本就是为此设的 escape hatch），
  **不把厂商名写进框架分支**。

### 5. I-13 模型配置与回退契约（2026-08-19，实现注记）

- **backend 声明**：`spec.backend: auto|cloud|local`——auto 取 profile 解析值（`LLMConfig.backend`，yaml 可声明）；显式与 profile 不一致在 `pool.acquire` fail-fast（带修复指引）。profile 是后端单一事实源。
- **extra_body 透传扩展**：`thinking`（ADR-041）+ `adapter`（LoRA）+ `reasoning_effort` 合并进请求 extra_body——框架不硬编码任何供应商语义（延续 §1 原则）。
- **单级故障回退**：白名单瞬时异常（APIConnection/APITimeout/RateLimit/5xx）→ 同 messages/tools 切 `llm_fallback` profile 重试一次；4xx 配置错不触发（fail-fast）；fallback 失败抛合并错误（`primary: ...; fallback: ...`）。audit `agent_llm_fallback` + `agent_thought.llm_backend`（primary|fallback）标记。
- **hook 交互（Q3）**：回退是框架 do_call 层原语；每次尝试独立触发 `llm.call`，payload 带 `backend`/`attempt`（进 ctx.extra）——I-15 计量按 backend 归因、I-21 span 用 attempt 关联同一轮两次调用。
- 请求 model = `llm_config.model or spec.model`（profile 可覆盖 spec 的模型名，本地 Ollama/vLLM 场景）。

## Consequences

- **正面**：思考模式 + 工具调用端到端不再报 "must be passed back"；模型文本输出与最终答案进入后续上下文；通用框架不绑定 DeepSeek 特例。
- **代价**：多一个配置字段（默认开启回传，兼容现有行为）；agent_thought 事件体积增加（reasoning 内容进审计）。
- **依赖**：I-04 锚点保护先行（同文件 `build_messages`，先改截断再改序列化）。

## References

- ADR-034 Rev 2（上下文预算与消息组装）
- ADR-054（SDK 调用中 AgentSpec 是一等公民；本次修订的上位规则）
- DSH deepseek 适配器 `serializeAssistant`（appimg 提取副本）
- DeepSeek thinking mode 官方文档
