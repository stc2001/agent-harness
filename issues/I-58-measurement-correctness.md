---
id: I-58
title: 测量正确性 — fast tokenizer/CJK 零值守卫 + budget_ratio 阈值倒置
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

两个测量缺陷叠加，使 app 高水位判断系统性失真：

1. **fallback tokenizer 把 CJK 计为 0**：`AutoTokenizer.from_pretrained` 在这些
   制品（只有 `tokenizer.json`）上返回慢速 `LlamaTokenizer`，未实现 byte-level
   BPE。实测日文 2973 字符 → 0（服务端 2293）、中文 2160 字符 → 0（服务端 1564）。
   用 `PreTrainedTokenizerFast(tokenizer_file="tokenizer.json")` 后误差 <1%
   （日文 2289 vs 2293，-0.2%）。
2. **阈值倒置**：`AgentSpec.context_budget_ratio` 默认 0.8，装配在 80% 处提前
   截断，而 app 高水位在 85% ⇒ 高水位不可达、压缩从不触发。默认改为 1.0
   （截断仅兜底；部署仍可主动收紧）。

另：`e0ae0fa`（app 水位尺子计 content+reasoning+tool_call args）的语义已在
**I-53 R2** 落地（`_observe` 从 preview `params.provider` 取 tokenizer，计完整字段），
本 MR **不回退**硬编码 `"deepseek"`；该提交作为 no-op 记录，行为由现有测试覆盖。

## 验收标准 / Acceptance criteria

- [ ] `pytest tests/test_fallback_tokenizer.py -q` → 通过；CJK 不再编码为 0（无制品时跳过，真实环境应实际通过）
- [ ] `_get_tokenizer` 优先 `tokenizer.json` + `PreTrainedTokenizerFast`（否则 AutoTokenizer），加载后做 CJK 零值守卫：编码为空即 `ValueError`（不再静默返回 0）
- [ ] **R1 回归**：`test_fallback_tokenizer.py` 5 passed；fast/AutoTokenizer 两条加载路径的 `_Deaf` 制品均触发 `ValueError`（含 `CJK as 0 tokens`）；缓存边界用例已注明「守卫仅加载期」
- [ ] `AgentSpec.context_budget_ratio == 1.0`；`prompt_preview` 与 `client.prompts.build_messages` 的默认 ratio 同为 1.0（harness 内 0.8 预算默认值清零）
- [ ] `pytest tests/test_context_round_truncation.py tests/test_truncation_is_fallback_only.py tests/test_local_backend_config.py -q` 通过
- [ ] `e0ae0fa` 语义已由 I-53 R2 覆盖：`tests/test_memory_observe.py::test_observe_usage_counts_content_reasoning_and_tool_args` 断言 usage 含 reasoning/tool args；代码不出现硬编码 provider
- [ ] `scripts/translate_longhorizon.py` 改动过滤（已退役 app，OH 无路径）；harness 侧仅预期文件变更
- [ ] 全量由 Lead 在真实工作区执行（含真实 `tokenizer_dir/deepseek` CJK 计数验证）

## 不在范围内 / Out of scope

- `scripts/translate_longhorizon.py`（退役 app；过滤）
- `agent_ws/` 翻译数据（OH 无；tokenizer 测试用 fixtures/跳过）
- 部署物 `tokenizer_dir/`、`harness/config/models.yaml`
- app 单插槽/尺寸守卫（G/I-57 已合并）

## 关联

- 原始提交：`77a64b4`（harness）、`e0ae0fa`（app，语义已由 I-53 R2 覆盖）
- 前置：I-53 R2（provider/完整字段 `_observe`）、I-57（单插槽）
