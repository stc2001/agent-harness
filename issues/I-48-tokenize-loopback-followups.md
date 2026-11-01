---
id: I-48
title: /tokenize loopback 门控的两项遗留 — docstring 与代码矛盾、非 loopback 自托管静默降级
labels: [ready-for-agent]
author: admin
created: 2026-10-02
status: closed
---

## 背景 / What & Why

I-47 复评通过并合并（`4184cc1`）。其 R2 修复（`_is_loopback()` 门控，防 prompt 原文外发到
`<cloud>/tokenize`）方向正确，评审方已确认作者的 **fail-closed 取舍成立**。
但复核过程中留下两项**不影响 I-47 合并**、却应被记录的遗留问题。

### (a) docstring 与代码/测试三者矛盾（低，但有实际风险）

`harness/prompt_assembly.py:95-96` 写道：

> A non-loopback result is refused (returns None) **unless it came from an explicit override**,
> so cloud profiles fall back to the HF tokenizer instead of shipping prompt text off-box.

但实际行为是：**显式 override 指向远程时同样被拒绝**（代码 `else: url = None  # explicit but remote: still refuse`），
且 `tests/test_local_backend_config.py::test_remote_env_override_is_refused` 正是断言这一点；
同一 docstring 第 89–90 行也写着「operator naming a full URL means it — **but it must still be loopback**」。

即该句与**本文件其余说明、代码、测试**三者都矛盾。运行时零影响，但留着有实际风险：
后来者若照该句把代码「改对」，就会**重新打开 prompt 原文外发**这一已修复的缺陷。

**验收**：该句与代码一致（明确「显式 override 亦须 loopback」）；`grep` 不再出现
"unless it came from an explicit override"。

### (b) 非 loopback 自托管静默降级（中）

issue I-47 标题为「本地/**自托管**后端支持」。但门控只放行 loopback，因此：

| 部署形态 | `_tokenize_url()` | 后果 |
|---|---|---|
| llama.cpp 在**本机** | `http://127.0.0.1:8080/tokenize` | ✅ L1-01 已修 |
| llama.cpp 在**同局域网另一台机器**（如 `http://192.168.1.50:8080/v1`） | `None` | ❌ **静默**回退到 CJK 盲的 HF tokenizer ⇒ **中文仍计 0，L1-01 原始缺陷在该部署下依然存在** |

实测（六组合，见 `mr/I-47` 复评意见）：`LAN self-hosted profile -> None`、`explicit LAN override -> None`，
且**没有任何日志或告警**，使用者无从察觉。

**验收（二选一，需先裁决方向）**：

- [ ] 方案 A：拒绝非 loopback 时输出**一次可见告警**（stderr/log，只报一次，避免刷屏），
      让使用者知道「当前部署下中文计数仍不可信」
- [ ] 方案 B：为**显式声明**的端点提供 opt-in 通道（例如 `HARNESS_TOKENIZE_URL` 已明确指向某主机时
      视为使用者知情同意），同时保留「**从 profile 自动派生**时必须是 loopback」的默认防线

> 评审方倾向：A 无论如何都该做（静默降级是真正的坑）；B 属新增能力，若做应单独评估。

## 不在范围内 / Out of scope

- 不改 I-47 已合并的 loopback 判定逻辑本身（除非方案 B 被采纳）
- 不处理 `estimate_tokens` 口径（L1-04）

## 关联

- 来源：I-47 复评意见（`merge-requests/I-47.md` 的「复评意见」节）
- 相关代码：`harness/prompt_assembly.py`（`_is_loopback` / `_tokenize_url`）
- 相关 issue：I-47（已合并 `4184cc1`）
