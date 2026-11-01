---
id: I-57
title: app 单插槽策略 + 取段顺序 + 尺寸守卫（ADR-001 修订）
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

ADR-001 原设计为 prefix/suffix 两个 region、各一条链、各一个插槽。实测与
DSH/pi harness 源码复核后收敛为：

- **一个插槽**（`before_task`）：此前内容整合为一份摘要，新摘要必须**合并**旧摘要，
  而不是覆盖；否则早期事实会从 prompt 消失。
- **取段顺序**：prefix（更早、已完成任务的历史）最旧优先；prefix 不足以降到
  低水位时，再取 suffix（当前任务）最旧轮次；当前任务至少保留 1 轮。
- **尺寸守卫**（采纳 DSH）：`<mem>` 摘要的 token 数若 >= 被遮蔽内容，则拒绝——
  “压缩”不得放大上下文。
- 删除无调用方的旧站点逻辑（`_compact_*`/`_should_compact`/`_last_size`/
  `threshold` 等）；watermark 固定为 ADR-001 的 85/25。

## 验收标准 / Acceptance criteria

- [ ] 单链单摘要：`previous_summary()`/`slot_count()` 无 region 依赖；位置按 span 区域写：prefix→`before_task`、suffix→`after_task`、混合→两处同一份摘要（harness 零改动）
- [ ] 取段顺序：prefix 最旧优先；prefix 不足再 suffix 最旧；当前任务至少保留 1 轮（`prefix_count` / `foldable` 逻辑）
- [ ] 链式合并：新摘要的输入包含上一份摘要（PREVIOUS SUMMARY），输出为一份合并摘要
- [ ] 尺寸守卫：`framed >= shadowed` 时拒绝、不注入、计数失败；`framed < shadowed` 接受
- [ ] **R1 回归**：suffix/mixed span 真正释放（混合写 before+after，真实 `build_messages` 断言 Task 保留、两 `<mem>` 参与；低水位投影与实际一致）
- [ ] **R1 回归**：`test_memory_slots` 两个 registry 用例 HOME/XDG 隔离，不再写 Lead 不可写 HOME
- [ ] 旧站点死代码仍不存在（`_compact_*`/`_should_compact`/`_do`/`_last_size`/`threshold`）
- [ ] `scripts/memory_bridge.py` 保留 I-53 R2 的 `_observe` 语义（provider 来自 preview params、content+reasoning+tool args）与 F 公开面
- [ ] `tests/test_memory_bridge.py`、`tests/test_memory_state_machine.py`、`tests/test_memory_slots.py` 通过（相关子集）
- [ ] `docs/adr/app/001-memory-compaction-state-machine.md` 与实现一致（单插槽/取段顺序/尺寸守卫）
- [ ] harness 侧零改动；全量由 Lead 在真实工作区执行

## 不在范围内 / Out of scope

- `scripts/translate_longhorizon.py`（已退役 app，OH 无路径；过滤）
- `merge_TODO.md`（本地追踪文档；过滤）
- harness 侧任何改动（`harness/` 应零 diff）
- CJK tokenizer 缺口（H/I-58）

## 关联

- ADR：`docs/adr/app/001-memory-compaction-state-machine.md`
- 原始提交（拓扑序）：`bb61303`（过滤）→ `b838981`、`e50d594`、`5239eaf`
- 前置：I-53（状态机/`_observe`）、I-56（公开面/插槽机制）
