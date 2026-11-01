---
id: I-45
title: 当前任务（suffix）按轮截断 + mem 事件级定位与计预算 + 装配期 fail loudly
labels: [ready-for-agent]
author: admin
created: 2026-10-02
status: closed
---

## 背景 / What & Why

ADR-034 Rev 3 注记 2 规定：`[旧任务历史(可截断)] + [anchor 起当前任务全部轮次](永不截断)`，
即**当前任务（suffix）受保护、永不截断**。用真实长程任务（20 页翻译 / 42 块 / 本地小模型）
实测，该策略在**单任务长程**下必然硬失败：

| 观测 | 数据 |
|---|---|
| suffix token | **340,929**，预算 104,857 ⇒ **一个事件都没被丢** |
| 后果 | 请求带超长 prompt 发出 → 服务端 `400 exceed_context_size_error` → `BadRequestError`（不在 `_TRANSIENT` 内，不重试） |
| 代价 | 该次 prefill **204 秒全额支付**后才被拒 |

同时暴露三个同源缺陷：

1. **prefix 逐事件截断 ⇒ 悬空 `tool` 消息**：`prompt_assembly.py:330-335` 用
   `reversed` + `break` **逐事件**丢弃，边界落在轮中间时会把 `assistant(tool_calls)`
   丢掉而保留其 `tool` 结果 ⇒ 违反 ADR-052 §5 不变量 2（已实测出 `tool(c_OLD2)` 悬空）。
2. **`mem` 定位错**：`:349` 用 `insert(kept_prefix + 1, ...)`，prefix 被丢光时
   `kept_prefix=0` ⇒ `insert(1)` ⇒ 落入**任务锚点之后**，而它概括的是锚点**之前**的历史。
3. **`mem` 不计预算**：`:289` 注释"never count against budget"——它确实不进 `used`，
   但**照发给模型、占上下文**。

另外 `memory_summary` 硬编码"至多一个"（`:347` "Contract: at most one"），
而 case2 需要 prefix 区与 suffix 区**各一个**。

## 验收标准 / Acceptance criteria

规格见 `issues-evidence/l3-03-grill-evidence.md` 与 ADR-053。以下每条均可判 True/False：

- [ ] **A** `assemblable()` 配对丢弃悬空 `tool`：`tests/test_prompt_assembly_invariants.py::test_tool_messages_have_matching_call_id` 在新增 `orphan_tool_result` 场景下通过
- [ ] **B** suffix 参与截断：`tests/test_context_round_truncation.py::test_suffix_truncation_by_round` 通过（`meta["dropped_event_count"] > 0`）
- [ ] **C** prefix 截断按**完整轮**：`test_orphan_tool_result_is_dropped` + `test_prefix_truncation_never_splits_round` 通过（无悬空 `tool`）
- [ ] **D** 预算按**实际发出的 messages** 度量：`test_budget_measured_on_emitted_messages` 通过（200k 字符结果不再 24x 高估）
- [ ] **E** `mem` 事件级定位：`test_mem_lands_before_anchor_when_prefix_dropped` 通过（`mem` 索引 < 锚点索引）
- [ ] **F** `mem` 计入预算：`test_mem_counts_against_budget` 通过
- [ ] **G** 不可再精简集超限 ⇒ fail loudly：`test_irreducible_set_overflow_is_flagged` 通过（`meta["over_budget_unresolvable"] is True`），且 `AgentRuntime` 返回 `status="context_overflow"`（**非** `completed`）
- [ ] **G（runtime 半边）** `test_runtime_returns_context_overflow_without_calling_llm` 通过：断言 `status`/`reason`/`error`、落一条 `agent_failed`，并**哨兵断言未发起任何 LLM 调用**
- [ ] **H** 支持 2 个 `mem`：`test_two_memories_are_both_emitted` 通过
- [ ] 硬约束 `N ≥ 1`：`test_min_keep_rounds_below_one_raises` 触发 `ValueError`
- [ ] 硬约束 两套口径：`test_hard_limit_accounts_for_output_reservation` 通过（`hard_limit == context_window - max_tokens`）
- [ ] 回归：`python -m pytest tests/ -q` 结果 ≥ 基线 `695 passed`，且 7 个既有云端 401 用例之外无新增失败
- [ ] **端到端（确定性，无需外部服务）**：`python -m pytest tests/test_context_round_truncation.py -k e2e -q` 通过 —— 用仓库既有 in-process `MockTransport`（ADR-012）跑完整 `组装 → LLM → 工具 → 结果回灌 → 终答` 往返，并断言第二次请求确实带上了 `role:"tool"` 结果与其 `tool_call_id`
- [ ] **端到端（真本地模型，可选）**：`python scripts/harness_smoke.py all` 输出 `{"config": true, "raw": true, "harness": true}`。**前置条件**：本地 OpenAI 兼容服务在 `configs/llm.yaml` 的 `base_url` 上可达；`bwrap` 缺失时 stage 3 跳过。脚本已入库（`scripts/harness_smoke.py`），不在库外

## 不在范围内 / Out of scope

- **不自行生成摘要**：`compress()` 触发权仍归 app（ADR-038）。本 issue 只做
  **截断 + 预留 mem 位置与接口**，框架不调用 LLM 做压缩。
- **不统一 `estimate_tokens` 的数值口径**：它与 `build_messages` 实测差 113,864 token，
  属 L1-04 范畴；本 issue 只在**文档层**统一口径。
- **不处理 L3-01**（`max_iterations` 默认值与"耗尽仍返回 `completed`"）：
  本 issue 新增 `status="context_overflow"`，L3-01 可照此形态另开。
- **不处理 L3-02 / L1-05**（声明 `context_window` 与服务端真实容量校验）：
  本 issue 新增 `meta["hard_limit"]` 为其留落点，但不做校验。
- 不支持**任意多个** `mem`（只支持 prefix 区 / suffix 区各一个）。

## 关联

- 新增 ADR：`docs/adr/architecture/053-current-task-round-truncation.md`
- 修订 ADR：`docs/adr/architecture/034-memory-compression.md`（Rev 4 注记）
- 增补 ADR：`docs/adr/architecture/052-prompt-assembly-vs-dsh.md`（§5 不变量 4→5 条）
- 术语表：`CONTEXT.md`（新增「上下文预算与会话装配」小节；「锚点」消歧为「沙箱锚点」）
- 证据：`issues-evidence/l3-03-grill-evidence.md`
- 阻塞关系：无。相关但独立：L3-01（耗尽语义）、L3-02/L1-05（容量校验）、L1-04（`estimate_tokens` 口径）
