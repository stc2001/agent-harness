---
id: I-65
title: tokenizer 缓存边界 — fallback 计数期零值守卫
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

I-58 的 CJK 零值守卫只在**加载期**执行；已缓存/注入的坏 tokenizer 仍会让
`_count_tokens` 对非空文本静默返回 0，导致预算/水位系统性低估。`server
/tokenize` 路径是权威，不应加守卫。

## 验收标准 / Acceptance criteria

- [ ] `_count_tokens` fallback：`n = len(tok.encode(text))` 后，`text.strip()`
      非空且 `n == 0` → `ValueError`，信息含 provider、tokenizer 路径、
      「non-empty text counted as 0 tokens」及修复提示（与加载期守卫同风格）
- [ ] 空文本 / 纯空白 → 0，不抛
- [ ] `_server_count_tokens` 返回非 None 时原样返回（权威路径不加守卫；docstring 写明）
- [ ] 测试：缓存/注入 `_Deaf` + `HARNESS_TOKENIZE_URL=off` → 抛 ValueError；
      空/空白 → 0；server 路径 0 仍返回 0；真实 tokenizer CJK > 0 既有测试仍绿
- [ ] `_count_tokens` docstring 写明「加载期守卫 + 计数期守卫」两层
- [ ] 不回退 I-58 加载期守卫 / I-64 cost map/reserve；pi/translate/merge_TODO/
      部署物零 diff
- [ ] 变异：删计数期守卫 → 新测试红；空文本测试仍绿

## 关联

- 前置：I-58（加载期守卫）、I-64（cost map/reserve）
