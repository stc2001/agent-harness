# ADR-052：prompt_assembly 与官方 dsh 的差异记录（diagnose 2026-09-11）

## Status

Accepted (2026-09-11)

## Context

对「最终发送给 LLM 的上下文」（默认参数，含上下文压缩语义）做 py-harness ↔ 官方 DSH 的差分诊断。回路：
- ground truth = **真实 dsh 运行时**（桌面端已编译包）+ 自写 CaptureAdapter 捕获 3 个 `GenerateOptions`（`tests/fixtures/dsh_prompt_capture.json`）；
- 对侧 = 等价事件流跑 `build_messages`；
- 逐条 diff（`.scratch/dsh_probe/compare.py`），并把结论锁进 `tests/test_prompt_assembly_dsh_parity.py` 与 `tests/test_prompt_assembly_invariants.py`。

dsh 装配语义（`packages/core/session/src/surface.ts` + `packages/llm/llm-deepseek/src/serialize.ts`）：

```text
surface 节点逐字透传（user/message、assistant/message、tool/result）；无装配期截断
assistant：content = 扁平文本（无则 ""，**永不 null**）+ reasoning_content（有则带）+ tool_calls
user 消息内的 tool-result 展开为独立 role:"tool" 消息（content 为空则 "(no output)"）
压缩 = compaction 插件对 surface 做 replace（阈值 ratio 0.8 / retain 0.16；tool-result pruner 8192 → head 4096 + marker + tail 1024）
```

## Decision

### 1. 已对齐（判定为**与 provider 无关的自身缺陷**，已修复）

| 缺陷 | 症状 | 修复 |
|---|---|---|
| 最终轮重复 assistant | 同一轮内容发两条（thought.content + completed.output）；此前去重仅在带 reasoning 时生效 | prescan：非工具轮 thought 一律被 completed 收编（commit 06c29a5） |
| 纯工具轮 `content: null` | null 会被持久化并毒化后续请求（严格网关 400） | content 缺失 → `""`（永不 null） |

两者即使不适配 DeepSeek 也存在：前者破坏「一轮 assistant = 一条消息」的自身契约（token 浪费 / 前缀不稳 / 严格模板风险），后者是可移植性与状态污染问题。

### 2. 位置/顺序类：**已无差异**

dsh 顺序 = surface 节点（日志）顺序；我们 Rev3 修复过 anchor 前置 bug（旧任务被摆到最新位置，commit 602d8c5），现两边因果序一致，并由多任务 parity 测试锁定。

### 3. 仍存在的差异（非位置类）

| 差异 | 层 | 结论 |
|---|---|---|
| 任务消息加 `Task: ` 前缀 | agent_started → user 投影 | **接受**（我们的事件模型用前缀承载任务边界；app 若需逐字透传可后续参数化） |
| 独立空 assistant 保留 | assistant 投影 | **接受**（dsh 跳过空内容轮；我们仅在无 completed 收编时才可能出现） |
| 工具结果裁剪：头 8192 | tool_result 投影 | **接受**（dsh pruner 为头 4096+marker+尾 1024）；阈值一致（8192），形状不同 |
| 工具 schema 规范排序 + tool guidance 段 | **由谁组装 tools** | **接受**：我们由 registry/调用方给定，dsh 在 system-prompt 插件内规范化 |
| 系统提示词：调用方字符串 vs dsh 分节组装 | **由谁组装 system** | **接受**：内容归 app（ADR-038）；harness 只做透传 |
| 压缩归属：装配器预算截断 + memory_summary 折叠 vs 日志级 compaction replace | **职责归属** | **保留现状**（ADR-034 有意决策：预算截断在装配器、折叠触发归 app） |

### 4. 可调性判定

**默认参数下无法做到与 dsh 逐字一致**：可调项仅 system 文本、reasoning passback（默认已一致）、以及“近似关闭截断”（调大 window/ratio）；前缀、事件模型重建、压缩/裁剪语义、tools 与 system 的组装归属均非参数可及。

### 5. 不变量（防回归）

```text
content 任何消息不得为 null
tool 消息必有 tool_call_id 且可追溯到先前的 assistant tool_calls
不得出现连续两条内容相同的 assistant
带 tool_calls 的 assistant，content 必须是 str
截断只允许发生在「完整轮」边界（见下）
```

7 场景 × 4 不变量（`tests/test_prompt_assembly_invariants.py`）；突变检查（回放修复前版本）证实其可捕获上述两处缺陷。

### Rev 1（2026-10-01）：增补第 5 条不变量 —— 整轮边界

ADR-053 实测发现**第 2 条不变量当前会被违反**：`build_messages` 的 prefix 截断是
**逐事件**粒度（`:330-335` 的 `reversed` + `break`），丢弃边界落在轮中间时会把
`assistant(tool_calls)` 丢掉、而其 `tool` 结果留下 ⇒ 产生**悬空 `tool` 消息**。

```
实测（scripts/probe_suffix_truncation.py）:
  [0] system  SYS
  [1] tool    '结果 OLD2'   << c_OLD2      ← 悬空：其 assistant 已被丢
  [2] user    'Task: 任务 R1'
  ...
  meta: dropped_event_count=3  truncated=True
  结果无对应 tool_call 的 : {'c_OLD2'}
```

**为何既有 77 个测试未捕获**：现有 `SCENARIOS` 中没有"prefix 被逐事件截断"的用例，
故第 2 条不变量虽已定义却未真正生效。

**增补不变量（第 5 条）**：

```text
截断只允许发生在「完整轮」边界 —— 一条 agent_thought（带 tool_calls）
与其全部 tool_result 同进同出，不得只保留其一
```

**归属**：本条与第 2 条互为保障（第 2 条约束消息形态、第 5 条约束截断粒度）。
实现与验收见 **ADR-053**；新增 scenario 须覆盖"prefix 逐事件截断"形态，
使第 2 条不变量真正生效。

> 注：本条为**纯增补**，不影响既有 77 个测试
> （`test_divergence_assembly_truncation_exists` 仅断言 `len(msgs) < len(events)`，
> 按轮截断仍成立）。

## Consequences

- 正面：差异被显式记录并测试锁定；两处真实缺陷修复；后续若要对齐某条，改动点明确（投影函数、裁剪常量、tools/system 归属）。
- 代价：与 dsh 的“逐字一致”不可达（且非目标）；真 LLM e2e 存在偶发 flake（与本模块无关，单跑通过）。

## References

- ADR-034（预算/折叠）、ADR-038（原语 vs app 边界）、ADR-041（reasoning passback）
- `tests/test_prompt_assembly_dsh_parity.py`、`tests/test_prompt_assembly_invariants.py`、`tests/fixtures/dsh_prompt_capture.json`
- dsh 源码：`packages/core/session/src/surface.ts`、`packages/llm/llm-deepseek/src/serialize.ts`、`packages/compaction/*`
