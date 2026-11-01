---
id: I-72
title: 登记同步后遗留问题 + 工具输出长度限制 e2e
labels: [tooling, test]
author: lead
created: 2026-10-19
status: closed
---

## 背景 / What & Why

A–H + I-59 同步与 Phase 1–4 收口后，需要把项目级遗留问题登记到
`python-harness-issues.md`，并用 e2e 固化「app 层 pi 工具输出 → harness 装配」
的长度限制结论（两层的限制点与数值）。

## 范围

1. `python-harness-issues.md` 追加 2026-10-19 遗留问题登记：
   - I-69 `2>/dev`（`sys_read_deny` 与 `/dev`，open）
   - benchmark 测试集缺失（未立项）
   - 安全：`docs/harness_doc/deepseek_keys_for_test` 已提交 key（待轮换/清理）
   - 已知非 hermetic 测试（hashline / httpbin）
   - Phase 5 与本地同步策略延期
   - 附：工具输出长度限制的 e2e 结论（app 50k chars / harness 8192 chars）
2. `tests/test_pi_runtime.py` 新增 2 个 e2e：
   - bash 输出 200k 字符 → 事件存储 ≤ app `MAX_OUTPUT_CHARS`+marker；装配 prompt 的 tool 消息 ≤ harness `TOOL_RESULT_LIMIT`+marker；
   - read 3000 行 → app 行数上限（`MAX_READ_LINES`）+ harness 装配截断。

## 验收标准

- [ ] 文档登记完整、链接正确（I-67/I-69、ADR-051）
- [ ] 两个测试通过，且能区分 app 层与 harness 层限制（数值断言）
- [ ] OH 全量绿（既有非 hermetic 项除外）

## 关联

- 用户直接指示（2026-10-19）；I-69 / I-67 / ADR-051
