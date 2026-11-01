---
id: I-51
title: 装配成本度量与截断兜底的正确性
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

`_cost` 是「哪些轮次留在预算内」的唯一判据，但它测量的是错的东西：
包含不发送的审计字段（实测 2.46x 高估）、不遵循 `reasoning_passback`
（实测 1.9x 高估）；随后截断作为兜底又被误用为常规路径。三处叠加使截断
时机全面失真，并连带让 app 层 85% 高水位永不可达。

## 验收标准 / Acceptance criteria

- [ ] `pytest tests/test_cost_measurement.py -q` → 13 passed；`_cost` 与装配器共用同一份「事件→消息」映射，对任意事件集 **Σ_cost == Σ(事件消息)**（精确相等，不再用 1.25x/1.35x 容差）
- [ ] 覆盖 5 类边界：`agent_failed`、未覆盖 `tool_called`、末轮 `agent_thought`+`agent_completed` 合并、`memory_summary` 前缀、`agent_started._retry_error`；孤儿 `tool_result` 计 0
- [ ] `pytest tests/test_truncation_is_fallback_only.py -q` → 9 passed；默认 `context_budget_ratio == 1.0`；只有超硬上限时才丢弃最少轮次；每次截断产生 `prompt_truncated` 审计告警
- [ ] P2 披露（保留）：`AgentSpec.max_tokens` 默认 `None`，请求 kwargs **不含** `max_tokens`（`test_spec_max_tokens_defaults_to_unlimited`、`test_unlimited_max_tokens_is_omitted_from_the_request`）
- [ ] P2 披露（保留）：`DeepSeekDriver` 在 `driver/backend=deepseek` 时 `thinking=True/False` 发 `{"type": "enabled"/"disabled"}`，`None` 不发字段（`test_deepseek_driver_renders_thinking_as_an_object`、`test_deepseek_driver_no_opinion_sends_no_thinking_field`、`test_deepseek_driver_never_sends_a_boolean`）
- [ ] P3：`AgentSpec.output_reserve_tokens` 默认 `16384`；`max_tokens=None` 时 `hard_limit < context_window`（`test_output_reserve_applies_when_max_tokens_is_omitted`）
- [ ] P3：显式 `max_tokens` 时用显式值而非预留（`test_explicit_max_tokens_wins_over_output_reserve`）；小窗口预留钳制后 `hard_limit >= 1`（`test_output_reserve_never_makes_hard_limit_nonpositive`）；spec→preview 透传（`test_agent_spec_output_reserve_reaches_the_preview_hard_limit`）
- [ ] `docs/adr/architecture/053-current-task-round-truncation.md` 含 2026-10-04 修订与 2026-10-19「请求输出上限 / 预算输出预留是两个独立旋钮」注记
- [ ] 全量回归由 Lead 在真实工作区执行；本 MR 分支不得引入新的失败类型

## 披露的语义变更（P2，保留，不拆分）

1. **`AgentSpec.max_tokens` 默认 `None`**：AgentSpec 属 app 层配置；未配置 = 不表态，请求中省略 `max_tokens`，由后端默认输出上限生效。影响：依赖旧 `16384` 默认的调用方需显式配置；预算侧由 P3 的 `output_reserve_tokens` 独立保证输出空间。
2. **新增 `DeepSeekDriver`**：`driver/backend = deepseek` 时，`thinking` 必须发 `{"type": "enabled"|"disabled"}` 结构体（布尔值实测 422；字段省略则走 provider 默认）。绑定测试见上。

## 不在范围内 / Out of scope

- app 层高水位状态机（后续 I-53）
- tokenizer 加载器修复（后续 I-58）
- 本地部署脚本、`configs/llm.yaml`、数据与运行产物
- P2 两项的独立 MR（维护者已裁决：保留在本 MR，不拆分）

## 关联

- 相关 ADR：`docs/adr/architecture/053-current-task-round-truncation.md`
- 原始提交：`67bf1db`（`_cost` 度量实际 message）、`2d58693`（遵循 `reasoning_passback`）、`d681809`（截断降级为兜底）
- R1 修复提交：`afd2be9`（_cost 共用映射 + output_reserve_tokens + 测试收紧）
- 阻塞关系：`Blocked by: 无`
