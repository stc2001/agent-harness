# ADR-055：LLM driver 层 —— 厂商适配从框架迁出到可替换驱动

**日期**: 2026-10-02
**状态**: 已采纳
**关系**: 落实 ADR-041 §1/§4（不硬编码供应商语义）；依赖 ADR-054（spec 一等公民、P/Q 优先级）；延续 ADR-038（原语 vs app 边界）

---

## 上下文

### 触发事实：同一意图在不同后端是**不同字段**，且没人仲裁

ADR-054 落地时，在本地 llama.cpp 后端上实测了「开关 thinking」的**全部请求形状**
（`build b11312-0c1e57098`，temperature=0，同一 prompt；模型 Sharp-Spark-X2.5-4B）：

| 请求体里的位置 | 实际 reasoning 字符 | 是否生效 |
|---|---|---|
| 不发任何开关 | 913 | 模板默认 |
| 顶层 `"enable_thinking": true` | 863 | ❌ 忽略 |
| **顶层 `"enable_thinking": false`** | **863** | ❌ **忽略（本应关闭）** |
| 顶层 `"thinking": true`（框架原实现） | 863 | ❌ 忽略 |
| `"chat_template_kwargs": {"enable_thinking": false}` | **0** | ✅ **唯一能关** |
| `"chat_template_kwargs": {"enable_thinking": true}` | 863 | ✅ 能开 |

**即：该构建只认 `chat_template_kwargs.enable_thinking`。** 上游把「按请求开关」
做成顶层字段是 [PR #22336](https://github.com/ggml-org/llama.cpp/pull/22336) 之后的事
——**我们的构建早于它**。所以这不是"配置写错"或"参数名拼错"，而是
**版本差异造成的真实适配需求**，且**没有任何错误回报**：传错位置时服务端静默按默认走。

同类风险不止一处：

> 复验：同一构建上的 **MiniCPM5-2B** 内嵌模板同样读 `enable_thinking`
> （`{%- if enable_thinking is false %}` / `is true`），所以该 driver 对两个模型
> 都成立 —— 这正是把渲染抽到 driver 的价值：换模型不需要改框架。

| 意图 | llama.cpp | OpenAI/DeepSeek |
|---|---|---|
| 开关 thinking | `chat_template_kwargs.enable_thinking` | `thinking`（透传） |
| 读思考内容 | `message.reasoning_content` | `message.reasoning_content` |
| LoRA 适配器 | `adapter` | 不适用 |

**问题定性**：框架持有一个**意图**（"开启思考"），但把它渲染成**哪个字段名**是
**后端特定知识**。ADR-041 §1/§4 明示「不硬编码供应商语义」，但当时的实现把
"透传一个厂商无关键"当成了"不硬编码" —— 结果是**没有后端读得懂这个键**。

### 为什么不能继续在框架里适配

ADR-054 的修复为了让它工作，在 `agent_runtime/impl.py` 里加了：

```python
if cfg_backend == "local":
    tmpl["enable_thinking"] = bool(thinking_flag)
```

这**违反本仓自己的 ADR-041 §4**，并且：

1. **一个后端一个分支** —— vLLM、Ollama、SGLang 各有各的形状，框架会变成厂商分支集合；
2. **新后端必须改框架** —— app 无法自行接入，只能等框架发版；
3. **不可替换** —— 无法为同一后端换策略（如旧版 llama.cpp vs 新版）。

## 决策

### 1. 引入 driver 层：意图与渲染分离

```
AgentSpec（意图）            LLMConfig.profile（绑定与驱动参数）
        │                              │
        └──────────┬───────────────────┘
                   ▼
            LLMDriver（渲染）
                   │
                   ▼
        OpenAI 兼容请求体 + 响应映射
```

**框架只声明意图，driver 负责渲染。** 框架内**不出现厂商名分支**。

### 2. Driver 契约

```python
@runtime_checkable
class LLMDriver(Protocol):
    name: str            # 驱动标识，如 "passthrough" / "llamacpp"

    def build_extra_body(self, *, spec, cfg, thinking: bool | None) -> dict:
        """把厂商无关的意图渲染成该后端认的 extra_body 字段。"""

    def reasoning_of(self, message) -> str:
        """从响应 message 里取出思考内容（无则返回 ""）。"""
```

- `build_extra_body` **只做渲染**，优先级判定（ADR-054 的 P/Q）仍在框架——**语义留在框架，字段名交给 driver**。
- driver **不做 IO、不碰凭证**，纯函数式，便于测试。

### 3. 内置驱动 + 注册表

| 驱动 | 适用 | 行为 |
|---|---|---|
| `passthrough`（默认） | 任意 OpenAI 兼容后端 | 发厂商无关键（`thinking`），读 `reasoning_content` |
| `llamacpp` | llama.cpp（**含早于 PR #22336 的构建**） | 把 thinking 渲染进 `chat_template_kwargs.enable_thinking` |

**选择规则**：`LLMConfig.driver`（profile 可声明）→ 未声明时按 `backend` 取默认
（`local` → `llamacpp`，`cloud` → `passthrough`）→ 都不可用时 `passthrough`。

**注册表由 SDK 暴露**，app 可注册/覆盖：

```python
client.llm.register_driver("my-vllm", MyVllmDriver())
client.llm.drivers()          # 查看已注册
```

### 4. 与 ADR-054 的关系（不变）

- P 路径（spec 未表态）：profile 的 `chat_template_kwargs` 原样透传，**driver 不改它**；
- Q 路径（spec 显式且矛盾）：spec 赢，并由 driver 渲染到正确字段，**同时记 `agent_spec_override` 审计**（不静默）；
- `backend` 仍是安全边界，冲突 fail-fast。

### 5. 框架侧的清理

`agent_runtime/impl.py` 里的 `if cfg_backend == "local":` 分支**删除**，改为
`driver.build_extra_body(...)`。框架重新符合 ADR-041 §4。

## 后果

1. **新增后端不再需要改框架**：app 实现 `LLMDriver` 并注册即可。
2. **可替换**：同一后端的不同版本可有不同驱动（如 `llamacpp` 与 `llamacpp-new`）。
3. **默认行为**：不声明 driver 时按 `backend` 推断；`passthrough` 保证既有云路径
   **字节级不变**。
4. **文档**：ADR-041 §4 的「不硬编码」从"只发无关键"改为"由 driver 渲染"；
   `CONTEXT.md` 增补 driver 术语。
5. **测试**：driver 契约测试 + 每驱动一组；框架测试改为断言「调用 driver」而非字段名。

## 被否决的方案

| 方案 | 否决理由 |
|---|---|
| **继续在 `impl.py` 里加厂商分支**（ADR-054 临时修复） | 违反 ADR-041 §4；每加一个后端都要改框架；app 无法自行接入 |
| **只发厂商无关键，不做适配** | 实测该键**没有任何后端读取** ⇒ 功能不可用（本次缺陷根源） |
| **把厂商差异写进各 profile（无 driver）** | 能跑，但把"如何表达 thinking"的**代码逻辑藏在 YAML 里**，无法表达条件逻辑、无法测试、每 profile 复制一遍 |
| **让 app 直接改请求体（无 driver 抽象）** | 无统一契约，每个 app 各写一遍；框架失去可观测性（driver 名可进审计/OTel） |

## Rev 注记

- 2026-10-02：首版。触发自查见 §上下文（llama.cpp b11312 穷举实验）。

## 参考资料

- llama.cpp 按请求开关 thinking（顶层字段，晚于本仓构建）：[PR #22336](https://github.com/ggml-org/llama.cpp/pull/22336)
- ADR-041 §1/§4（不硬编码供应商语义）
- ADR-054（spec 一等公民；P/Q 优先级与不静默覆盖）
- ADR-038（原语 vs app 边界）
