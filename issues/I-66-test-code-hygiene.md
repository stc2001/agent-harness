---
id: I-66
title: 测试/代码卫生 — 删误加弱断言 + PiMemory 常量清理
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

I-58 的源提交在 `test_runtime_emits_an_audit_warning_on_truncation` 中误加回两条
弱/自相矛盾断言；`pi/memory.py` 还留有被 placements 取代的 `SLOT` 常量与
`_provider_hint` 的 getattr 兜底。纯卫生，无行为变更。

## 验收标准 / Acceptance criteria

- [ ] 删除 `assert "prompt_truncated" in src`（与上方 `event="prompt_truncated"' not in src`
      自相矛盾）与 `assert "over_budget_unresolvable" in src or "truncated" in src`
      （弱：`history_context_truncated` 即满足）；保留三条 emitted-event 断言
- [ ] `grep -n 'prompt_truncated" in src\|over_budget_unresolvable" in src or'
      tests/test_truncation_is_fallback_only.py` → 空
- [ ] `pi/memory.py`：删除未使用 `SLOT`；`__init__` 显式
      `self._provider_hint: str | None = None`；`_summarise_span` 不再 getattr 兜底
- [ ] `grep -n 'SLOT' pi/memory.py` → 空；`_provider_hint` 初始化行存在
- [ ] OH 全量绿，测试数与 main 相同（只删断言/常量，不删测试）
- [ ] `harness/`、translate、merge_TODO、部署物零 diff

## 关联

- 前置：I-58（误加断言）、I-65
