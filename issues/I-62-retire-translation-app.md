---
id: I-62
title: 退役翻译 app 清理 + 记忆测试迁移到 pi/memory
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

ADR-002 已用 pi 模式通用 agent 取代翻译专用长程 agent。源提交 `244678f` 删除
退役 scripts/数据，并把 app 记忆测试迁移到 `pi/memory`（行为不变；I-57 true-release
语义迁移留给 I-63）。OH 侧只保留实际存在的残留文件。

## 验收标准 / Acceptance criteria

- [ ] 删除 OH 存在的：`scripts/memory_bridge.py`、`tests/test_memory_bridge.py`、
      `tests/test_memory_state_machine.py`；OH-only 的 `tests/test_memory_observe.py`
      一并删除并在 MR 说明覆盖（I-63 在 pi/memory 恢复等价测试）
- [ ] 新增 `tests/test_pi_memory_state_machine.py`（27）、
      `tests/test_pi_memory_bridge.py`（5）、`tests/test_pi_workspace_boundary.py`（2）
- [ ] `tests/test_access_boundary.py` 删除翻译脚本静态守卫（保留其余内核/挂载边界测试）
- [ ] `tests/test_fallback_tokenizer.py` 保持 OH 的 I-58 R2 守卫测试与 fixture 路径，不回退
- [ ] ADR-001：旧 scripts 标注已删除；**保留 I-57 R2 释放契约（§10 + 文末 R1）**
- [ ] ADR-002：第 5 条改为已删除/迁移；保留 I-61 pi 描述
- [ ] ADR-051：追加 244678f 翻译 agent 历史配置注记；保留 I-55/I-61 注记
- [ ] 无 `memory_bridge` 残留引用（历史 ADR 说明除外）
- [ ] `harness/` 零 diff；`pi/` 行为语义不动（I-63 更新 true-release）
- [ ] OH 全量绿（6 项既有 401 环境失败除外）

## 不在范围内

- I-63：pi/memory true-release（I-57 语义）迁移
- `scripts/translate_longhorizon.py` 等 OH 不存在文件（纯过滤）
- 部署物/merge_TODO

## 关联

- 源提交：`244678f`
- 前置：I-57 R2、I-60、I-61
