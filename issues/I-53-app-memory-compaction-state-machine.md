---
id: I-53
title: app 层记忆压缩状态机（ADR-001）
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

app 层长程记忆压缩经过 6 轮真实失败演进，最终收敛为 ADR-001 状态机：

1. `anchor_idx=0` 真值陷阱：`if anchor_idx and ...` 对 0 为假 ⇒ 两条压缩路径从未触发；
   `step()` 漏 `return` 返回 `None`。
2. 触发判据重复（站点内又有一份旧判据）：改了一处，实际门锁未改 ⇒ 42 步零压缩。
3. 用 drop 比例 / near-budget 作触发信号：截断会封顶 assembled cost，信号在最需要时
   恰好为假；`kept_rounds` 按事件单元计数，比例低估压力（实测 25 units dropped 但 ratio 0.202）。
4. 云 profile 注册 model service：`models.register()` 会健康检查 `<origin>/health`，云端点
   没有该端点 ⇒ 每次压缩 FAILED。
5. 摘要器 model/backend 取错 profile：与运行 profile 不匹配，模型池查不到模型。
6. 缺少迟滞：同一压力下每步都压缩（实测 40 步压缩 23 次，约 58% 的步）。

**新机制（ADR-001）**：高水位 85% 触发 → 一次性 drop 到 25% → 整段一次摘要；
链式摘要（新摘要合并旧摘要）；`before_task` 单插槽；锚点永不压缩；尺寸守卫（摘要
不得放大上下文）；摘要器跟随任务引擎但保持不思考/低温度。

## 验收标准 / Acceptance criteria

- [ ] `pytest tests/test_memory_state_machine.py -q` → 16 passed：85/25 水位、一次 drop、单摘要、单链、单插槽、锚点保护、尺寸守卫、失败降级
- [ ] **R1 回归**：`pytest tests/test_memory_observe.py -q` → 4 passed：provider 跟随 preview `params.provider`、usage 含 tool_call arguments/reasoning、tool-heavy 非 0、CJK 不因硬编码 provider 计 0
- [ ] 旧站点方法（`_compact_prefix`/`_compact_suffix`/`_should_compact`/`_do`/`_unused_done_boundaries`）已删除，无 `_last_size` 未初始化路径
- [ ] `pytest tests/test_memory_bridge.py -q` → 5 passed：摘要器 spec（cloud 不注册 model service、local 注册、model 来自自己的 profile、跟随任务引擎、thinking=False）
- [ ] 存在 `scripts/memory_bridge.py`，使用 app 层公开面（`sessions.preview_llm_messages`、`memory.inject_summary`；`_logical_events` 为当前基线已有私有读面）
- [ ] 存在 `docs/adr/app/001-memory-compaction-state-machine.md`（状态机/取段顺序/单插槽/尺寸守卫/失败策略）
- [ ] 触发判据单一权威：`dropped_rounds > 0` 为必要条件；无重复站点判据
- [ ] 不在 app 代码里写死 `deepseek` provider/model service；摘要器跟随任务 profile 的引擎
- [ ] 全量测试由 Lead 在真实工作区执行；本 MR 不引入新的失败类型

## 不在范围内 / Out of scope

- `scripts/translate_longhorizon.py` 装配点改动：本地翻译 app 已弃用（ADR-002），OH 无此路径；本 MR 过滤
- `scripts/run_harness.sh`：本地部署脚本，OH 无对应路径；本 MR 过滤
- `tests/` 中引用本地翻译目录/数据的断言（如有）：过滤
- harness 公开面补齐（`sessions.events.logical` / `memory.count_tokens` / `config.profile`）：属 F/I-56 范围，本 MR 不补框架代码
- 单插槽策略（G/I-57）及后续测量修正（H/I-58）

## 关联

- 相关 ADR：`docs/adr/app/001-memory-compaction-state-machine.md`
- 原始提交：`060446c`、`82fbb61`、`d70944d`、`017ae9b`、`57e38e4`、`af14beb`、`8a02a74`、`cc9e4e8`
- 阻塞关系：`Blocked by: 无`（I-51/I-52 已合并）
