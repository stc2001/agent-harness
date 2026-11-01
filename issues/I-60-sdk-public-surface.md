---
id: I-60
title: SDK 公开面补齐 + denial 回灌 + tokenizer 路径定位
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

构建 app 层（pi 模式 CLI）时发现 SDK 公开面缺口与 denial 反馈问题（源提交
`fa2b440`）：app 只能 import 私有符号、运行时 denial 未回灌给模型、相对
`tokenizer_dir` 在非 checkout cwd 下解析失败、缺少 event/span 成本 API。
本 issue 把这些 harness 侧修复带入 OH，并与已合并的 I-51/I-56 R2/I-58 对账。

## 验收标准 / Acceptance criteria

- [ ] `from harness.sdk import AgentSpec, SandboxConfig, ToolRegistry, HookMode, points` 可用
- [ ] `HarnessClient(project_dir=..., tool_registry=...)` 可用；工具注册表传入 pool，实例隔离、deps 不串扰，默认仍 `load_builtins()`
- [ ] `client.hooks.points`、`client.policy.enable(..., cfg=...)` / `policy.guard()` 可用
- [ ] `client.memory.event_cost(event, provider=None)` / `span_cost(events, provider=None)` 可用，provider 默认解析可工作（无硬编码）
- [ ] denial/unknown-tool 回灌：runtime 标记 `represented_by_tool_result` 并发出配对 `tool_result`；`_event_messages` 跳过重复 user `[tool error]`；`tests/test_denial_feedback.py`（含 Phase 1 新增 2 项）通过
- [ ] tokenizer 相对路径：`HARNESS_TOKENIZER_DIR` 优先、否则 checkout 根兜底；保留 I-58 fast loader + CJK 零值守卫；错误信息含两种定位方式
- [ ] profiles 对账：恢复 I-56 R2 `available_llm_profiles` 原样（含 YAMLError 容错），仅调用点改为 `project_dir=self._client._project_dir`；坏 YAML 时 `client.config.profiles()` 不抛（`TestProfilesRobustness`）；I-56 profiles 测试仍绿
- [ ] 保留 OH 既有修复：`_event_cost`/`_emit_context`/`_apply_fold_boundary`、legacy summary 忽略+meta、`output_reserve`、ratio 1.0、CJK 守卫、I-55 的 ADR-051 注记
- [ ] `git diff main...HEAD -- harness/` 仅含预期文件；`pi/`、`scripts/translate_longhorizon.py`、`merge_TODO.md` 零 diff
- [ ] 相关子集 + OH 全量绿（6 项已知 401 环境失败除外）

## 不在范围内 / Out of scope

- `pi/` app 层（I-61）
- `244678f` 删除/迁移翻译 app（I-62）
- `tests/test_truncation_is_fallback_only.py` 的 OH 弱断言（Phase 4 修）
- 本地部署物/路径

## 关联

- 源提交：`fa2b440`
- 前置：I-51 R1、I-56 R2、I-58 R1、I-55（ADR-051 注记）
