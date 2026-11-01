---
id: I-47
title: 本地/自托管后端支持 — tokenizer 计数修正、temperature 与 thinking 开关可配置
labels: [ready-for-agent]
author: admin
created: 2026-10-02
status: closed
---

## 背景 / What & Why

把 py harness 接到**本地/自托管 OpenAI 兼容后端**（llama.cpp `llama-server`）时，
暴露出三项默认值层面的缺陷。它们**在云端路径下都不可见**（云端 tokenizer 与模型一致、
无 thinking 模板差异），换成本地小模型后才会静默产生错误结果。
完整排查与实测证据见 `issues/local-backend-issues.md`。

### L1-01（正确性，最严重）tokenizer 与模型不匹配，CJK 文本计 0 token

`_count_tokens` 固定走 `tokenizer_dir/<provider>`，而 `tokenizer_dir/deepseek/` 的 BPE
词表**不含任何 CJK 字符**；本地模型是 gpt2 系 BPE（vocab 131072）。实测（在当前 main 上）：

```
纯中文        deepseek=   0   _count_tokens=   0      ← 整段算 0
Python       deepseek=  11   _count_tokens=  11
JSON schema  deepseek=  13   _count_tokens=  13
```

而 `memory_summary` 折算是按 `_count_tokens` 判定预算的 ⇒ **中文语料的预算形同虚设**，
真实 prompt 会冲破 `budget_ratio` 甚至冲破 `n_ctx`，服务端返回 400。

**修法**：`_count_tokens` 优先询问**服务端自己的 tokenizer**——
`POST <origin>/tokenize {"content": …} → {"tokens":[…]}`，精确、离线、无需 tokenizer 文件；
失败回退原 HF 路径。带结果 memo + 30s 负缓存（服务不可达时不逐次重试）。

**两个实现坑**（已实测）：`/tokenize` 在**根路径**，`POST /v1/tokenize` 返回 **404**；
该端点**不支持批量**（传 JSON 数组返回空 tokens）。

### L1-02 temperature 被硬编码 `0.0` 覆盖

`_call_llm_once` 硬编码 `temperature=0.0`，`LLMConfig.temperature`（默认 0.7）
与 profile 里配的 `temperature:` **完全不生效**。本地模型卡推荐 0.6，配了不起作用。

**修法**：新增 `LLMConfig.temperature_override: float | None = None`；
`None` 保持上游贪心行为（不破坏向后兼容），非 None 时生效。

### L1-03 无通道关闭 chat template 的 thinking 模式 ⇒ 空 `content`

Spark X2.5 等推理模型模板 `enable_thinking` **默认 true**，思维链占用**输出预算**，
预算不足时 `/v1/chat/completions` 返回**空 `content`**（`finish_reason="length"`），
看起来像模型坏了。harness 原来只有 `AgentSpec.thinking=True` 能**打开**，
没有任何地方能**关闭**模板默认值。实测（同一问题，只改 `max_tokens`）：

| max_tokens | finish_reason | completion | content |
|---|---|---|---|
| 16 / 64 / 256 | length | 16 / 64 / 256 | **空** |
| 512 | stop | 78 | 正常 |

**修法**：新增 `LLMConfig.chat_template_kwargs: dict`，经 `extra_body` 透传
（llama.cpp 接受 `chat_template_kwargs`），profile 里写 `enable_thinking: false`。

## 验收标准 / Acceptance criteria

- [ ] **L1-01 精度**：`python -m pytest tests/test_local_backend_config.py -q` 通过 —— 含
      「纯中文文本的计数 **> 0**」（当前 main 为 0，是回归防线）
- [ ] **L1-01 回退**：服务端不可达 / `HARNESS_TOKENIZE_URL=off` 时 `_count_tokens`
      **回退到 HF tokenizer 且不抛异常**（同一测试文件覆盖）
- [ ] **L1-02**：`LLMConfig.temperature_override` 存在；`None` 时行为与上游一致（贪心），
      非 None 时该值被用于请求（测试覆盖字段与透传）
- [ ] **L1-03**：`LLMConfig.chat_template_kwargs` 存在且经 `extra_body` 透传；
      测试断言请求体里带上 `chat_template_kwargs`（用 `tests/test_mock_llm.py` 的
      `MockTransport` 拦截，**无需真实服务**）
- [ ] **回归**：`python -m pytest tests/ -q` 无新增失败（相对同环境 main 基线）
- [ ] **组合正确性**：本改动与已合并的 I-45（ADR-053 按轮截断）叠加后，`pytest tests/`
      仍无新增失败 —— 两者都改 `prompt_assembly.py` / `agent_runtime/impl.py`

## 不在范围内 / Out of scope

- **不改云端路径行为**：`temperature_override` 默认 `None`（保持贪心）、
  `chat_template_kwargs` 默认空 dict ⇒ 未配置时字节级行为不变
- **不改 `estimate_tokens` 的统计口径**（属 I-45 交付时记录的 L1-04，另开）
- **不处理 L3-01**（`max_iterations` 耗尽语义）与 **L3-02/L1-05**（容量校验）
- **不含部署专属配置**：`harness/config/models.yaml`、`sandbox.yaml` 与
  `configs/llm.yaml` 含本机绝对路径，**不入库**（作为交付包另附）

## 关联

- 证据：`issues/local-backend-issues.md`（14 条 issue 的完整清单 L1/L2/L3）
- 相关 ADR：ADR-053（I-45，同文件改动，需叠加验证）；ADR-038（原语 vs app 边界）
- 阻塞关系：无。**依赖已合并的 I-45 作为叠加基线**（两者改同两个文件）
