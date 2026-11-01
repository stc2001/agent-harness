---
id: I-56
title: 记忆插槽化（ADR-053 二次修订）+ 审计拆级 + 公开面
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

ADR-038 边界原则 + Pi harness 实测实践共同指向：**harness 不应持有压缩策略**。
本 MR 是 ADR-053 的**二次修订**：

- harness 只保留 **Task 前/后两个固定插槽**（`before_task` / `after_task`）+
  `<mem>` 占位符 + 「每 slot 取最新」的顶替读规则；**prefix/suffix 划分归 app**；
- 插入契约改为 `inject_summary(session_id, content, *, slot, after_event_id=None)`，
  `slot` 必填（harness 不再猜测）；
- 装配顺序：前序任务 → `slot_before` → Task → `slot_after` → 当前轮次；
  SYS 与 Task 永不截断（截断作用域精确为历史）；
- 顶替是**装配期读规则**（日志 append-only；app 无法删除事件，也不需要）。

## 审计事件拆分（supersedes I-51 的 `prompt_truncated`）

I-51 刚合并的单一 `prompt_truncated` 事件被拆为两个**语义不同**的事件：

| 情形 | 事件 | level | 语义 |
|---|---|---|---|
| 丢弃历史后装得下 | `history_context_truncated` | warn | 兜底生效；截的是历史，SYS/Task 未截 |
| 无历史可丢仍超限 | `over_budget_unresolvable` | error | app 固定开销（SYS+插槽）超窗，配置错误 |

两者 detail 均含 `preserved: ["system","task","memory_slots"]`。
**理由**：一个 umbrella 事件把「常态兜底」与「配置错误」混为同一告警，运维无法分级；
`prompt_truncated` 也名不符实（prompt 的 SYS/Task 部分永不截断）。

## 删除死机制（破坏性变更）

删除 `client.memory.compress()` 与程序化注册的 `memory-compressor` agent spec：

- 它是**平行压缩机制**，其模型 id `deepseek-v4-flash` **不存在**（API 只接受
  `deepseek-flash`/`deepseek-v4-pro`）⇒ 一旦调用必然失败；压缩归 app 后无角色。
- 受影响调用方/测试：
  - SDK 公开方法 `client.memory.compress()`（删除；迁移：app 用
    `agents.run` 自己的 summariser spec，见 `scripts/memory_bridge.py`）
  - `memory-compressor` spec 注册（删除）
  - `tests/test_e2e.py::TestSDKMemoryCompressE2E::test_compress_real_events`（删除）
  - `tests/test_sdk_client.py::TestMemoryCompress`（6 项删除）
- 不是覆盖损失：删除的测试只验证已不存在的死机制；替代覆盖为
  `tests/test_memory_slots.py`（插槽/顶替/审计/公开面）与 app 层测试。

## 新增公开面（消除 app 私有调用）

| 新增 | 替代 |
|---|---|
| `client.sessions.events.logical(sid)` | `client._logical_events` |
| `client.memory.count_tokens(text, provider=None)` | `from harness.prompt_assembly import _count_tokens` |
| `client.config.profile(name)` / `client.config.profiles()` | `from harness.sdk.config import resolve_llm_profile` |

app（`scripts/memory_bridge.py`）改走公开面；`tests/test_memory_slots.py` 验证三个公开面。
`client.policy.add/enable` 与 `client.hooks` 本已存在。

## 验收标准 / Acceptance criteria

- [ ] `pytest tests/test_memory_slots.py -q` → 15 passed（含 R1 边界/兼容/profiles）：插槽紧邻 Task、`<mem>` 占位符与 role、同 slot 顶替、`slot` 必填、审计 warn/error 拆级 + `preserved`、三个公开面存在
- [ ] **R1 回归**：per-slot 边界只折叠本区域、Task 锚点永不丢失（before/after × 锚点前后 + 双 slot，共 5 项测试）
- [ ] **R1 回归**：无 slot 的旧 summary 不装配、成本 0，并在 `meta.ignored_legacy_summary_count` 报告（ADR-053 兼容性说明）
- [ ] **R1 回归**：`client.config.profiles()` 可用（`test_config_profiles_lists_project_profiles`）
- [ ] **R1 回归**：guard 断言 emitted event 名（history_context_truncated/over_budget_unresolvable；不再 emit prompt_truncated）
- [ ] `client.memory.compress` 与 `memory-compressor` spec 不再存在（`test_sdk_client.py` 已改为断言删除）
- [ ] audit 事件：丢弃历史 → `history_context_truncated`(warn)；不可满足 → `over_budget_unresolvable`(error)；不再产生 `prompt_truncated`
- [ ] `scripts/memory_bridge.py` 使用公开面并保留 I-53 R2 的 `_observe` 语义（provider 来自 preview params、content+reasoning+tool args）
- [ ] 受影响测试删除已列明；其余相关测试通过；全量由 Lead 在真实工作区执行
- [ ] `docs/adr/architecture/053-...` 含 2026-10-06 二次修订；ADR-038 边界注记更新

## 不在范围内 / Out of scope

- `scripts/translate_longhorizon.py` 改动（已退役 app，OH 无路径；过滤）
- CJK 回退 tokenizer 缺口（H/I-58）
- 单插槽策略/取段顺序（G/I-57）
- 本地数据/路径/`configs/llm.yaml`

## 关联

- ADR：`docs/adr/architecture/053-current-task-round-truncation.md`（二次修订）、`038-dag-removal.md`
- 原始提交：`62d0593`
- 前置：I-51（`prompt_truncated` 被本 MR 取代）、I-53（app memory bridge / `_observe` 语义）
