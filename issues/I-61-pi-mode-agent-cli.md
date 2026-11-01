---
id: I-61
title: pi 模式通用 agent CLI（app 层，ADR-002）
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

源提交 `cbec3b2` 引入 app 层 `pi/` 包：在 SDK 公开面上复刻 pi harness 的产品面
（headless/TUI、工具、沙箱 guard、记忆状态机、产物），不新增 harness 能力语义。
I-60 已补齐所需公开面，本 issue 只纳入 app 层与入口/文档。

## 验收标准 / Acceptance criteria

- [ ] `pi/` 包按源语义纳入，仅经 `harness.sdk` 公开面（不得 import harness 私有符号）
- [ ] 依赖公开面可用：`HarnessClient(project_dir=)`、`client.tools.register`、
      `client.config.profile/profiles`、`client.hooks.points`、
      `client.policy.enable(cfg=)`、`client.memory.span_cost/event_cost`、
      `sessions.events.logical`、`sessions.preview_llm_messages`
- [ ] `pyproject.toml` 在 OH 现有内容上增补：`pi = "pi.cli:main"`、packages `pi*`、
      `pi` package-data（defaults/*.md、defaults/profiles/*.yaml）；不覆盖既有依赖/配置
- [ ] `python -m pi --help` 可运行；headless 用例通过
- [x] 9 个 `tests/test_pi_*` + `tests/pi_fake_llm.py` 纳入且 hermetic（HOME/PI_HOME→tmp、fake LLM、无真实网络/本地部署物依赖）
- [x] **R1**：fixture tokenizer 直接装缓存（CJK=22 vs 部署=4），部署 tokenizer 存在时 pi 测试仍 43 passed；ADR-002 去除 harness_home/旧 893 基线/3200 不一致
- [ ] 文档：新增 ADR-002；ADR-051 增 pi 边界配置注记（保留 I-55 注记）；
      ADR-001 增 §10 使用点泛化，**保留 I-57 R2 两位置 true-release/尺寸守卫契约**
- [ ] OH 全量绿（6 项既有 401 环境失败除外）；`git diff main...HEAD -- harness/` 为空
- [ ] `pi/` 无本地路径/密钥/部署物；`runs/` 产物不入库
- [ ] `scripts/translate_longhorizon.py`、`merge_TODO.md`、部署物零 diff

## 不在范围内 / Out of scope

- `244678f` 删除/迁移翻译 app（I-62）
- 旧 `tests/test_pi_memory_bridge.py` / `test_pi_memory_state_machine.py` 迁移（I-62）
- 改 harness/（若缺公开面先停下报告）

## 关联

- 源提交：`cbec3b2`
- 前置：I-60（SDK 公开面/denial/tokenizer）
