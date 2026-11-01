---
id: I-49
title: 回退 tokenizer 应按 profile 显式配置 — 不再硬编码 deepseek（LAN/自托管下 CJK 计数为 0 的根因）
labels: [ready-for-agent]
author: admin
created: 2026-10-02
status: closed
---

## 背景 / What & Why

I-47 让 `_count_tokens` **优先**走本机 `/tokenize`（准确），I-48 让降级**可见**（告警）。
两者都没有修**降级本身**：当服务端点不可用（例如 llama.cpp 在**同局域网另一台机器**）时，
回退到 `tokenizer_dir/deepseek`，而该词表**不含任何 CJK 字符** ⇒ 中文仍计 0。

**根因经实证确定：不是「中文无法被 tokenize」，而是「回退选错了分词器」。**

实测对比（同一模型 Spark X2.5-4B）：

| | 回退用的 `tokenizer_dir/deepseek` | 被服务的真模型 Spark X2.5 |
|---|---|---|
| 词表大小 | 128,000 | 131,072 |
| **含 CJK 词表项** | **0** | **33,986** |
| `encode("中文")` | `[]`（空列表） | 正常 |
| `encode("推理引擎")` | `[]` | 2 token |
| 词表内容样例 | — | `的 一 是 不 有 在 了 人 中 和 为 以` … `四川省` |
| BPE 族 | DeepSeek | gpt2 系，`pre=spark2_5` |

**代码层面的根因**：`prompt_preview.py:65` 把 provider 硬编码为 `"deepseek"`：

```python
provider = ov.get('provider', 'deepseek')   # 从不读 profile
```

而 `LLMConfig` **本来就有 `provider: str = "deepseek"` 字段**，profile 可覆盖——
只是这条链路没被接上。所以 `_count_tokens` 永远取 deepseek 的词表，
**与当前实际服务的是哪个模型无关**。

> 说明：DeepSeek 模型本身当然支持中文——正常的 DeepSeek tokenizer 不会对「中文」编出空列表。
> 该目录下的词表文件很可能不完整。这**进一步说明**不该由一个硬编码的 provider 决定回退目标。

## 决策（已由维护者裁决）

**采纳「显式配置」方案**：`configs/llm.yaml` 的 profile **显式声明 tokenizer**；
未声明则按现状默认 `deepseek` 计数。

维护者原话要点：

> 「显式配置 tokenizer 是 **app 层开发者的事情**」；回退本身有差异，
> 但**该差异不在精确度考虑范围以内**——即本 issue **不追求**回退路径的计数精度，
> 只要求**能够被正确配置**。

因此**不做**本地启发式估算（如按字符数估 token）；那属于另一类方案，未采纳。

## 验收标准 / Acceptance criteria

- [ ] **接线**：provider 从**解析后的 profile** 读取，而非硬编码。实测断言：profile 设
      `provider: spark2_5` 时，`compose_llm_context` 产出的 `params["provider"] == "spark2_5"`
- [ ] **默认不变**：profile 未声明 `provider` 时仍为 `"deepseek"`（向后兼容，字节级不变）
- [ ] **回退生效**：`HARNESS_TOKENIZE_URL=off` + `provider: spark2_5` + 存在
      `tokenizer_dir/spark2_5/` ⇒ 纯中文计数 **> 0**（当前恒为 0）——用 stub tokenizer 目录覆盖，不依赖真模型
- [ ] **缺目录有明确错误**：配置的 provider 目录不存在时，报错**必须点明期望路径**
      （`tokenizer_dir/<provider>`），不得静默联网重试（见 L2-02）
- [ ] **文档**：`configs/llm.yaml` 模板与 `harness/sdk/config.py` 的 `provider` 字段注释说明
      「此值用于选择 `tokenizer_dir/<provider>` 作为**回退**分词器；服务端 `/tokenize`
      可达时以服务端为准」
- [ ] 回归：`python -m pytest tests/ -q` 无新增失败

## 不在范围内 / Out of scope

- **不追求回退路径的计数精度**（维护者明确）：只做「可配置」，不做本地估算/字频假设
- **不改** loopback 门控（I-47 R2 / I-48）与告警行为（I-48）
- **不为真模型自动下载/生成 tokenizer 文件**：provider 指向的目录须由部署方提供
- 不处理 `estimate_tokens` 口径（L1-04）

## 关联

- 前置：I-47（`/tokenize` 优先 + loopback 门控，已合并 `4184cc1`）、I-48（降级可见，MR 待评审）
- 相关代码：`harness/prompt_preview.py:65`、`harness/prompt_assembly.py`（`_get_tokenizer` /
  `_count_tokens`）、`harness/sdk/config.py`（`LLMConfig.provider`）
- 证据：`issues/local-backend-issues.md`（L2-02 记录了「缺 tokenizer 目录会静默联网重试」）
