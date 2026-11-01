# ADR-053：当前任务（suffix）按轮截断、不可再精简集与装配期失败语义

**日期**: 2026-10-01
**状态**: 已采纳
**关系**: 修订 ADR-034（记忆压缩/预算截断）；增补 ADR-052 §5 不变量；延续 ADR-038（原语 vs app 边界）

---

## 上下文

ADR-034 Rev 3 注记 2 确立了装配期策略：

```
[旧任务历史(可截断)] + [anchor 起当前任务全部轮次](永不截断)
```

即**当前任务（suffix）的 prompt 与工具往返受保护、永不截断**，并断言"截断从最旧的已完成历史开始"。

**该断言在单任务长程场景下被实测推翻。** 用真实长程任务（20 页翻译，42 块，本地小模型）验证：

| 观测 | 数据 |
|---|---|
| 单任务事件全部落在 suffix 内 | `suffix_cost` 达 340,929 token，预算仅 104,857，**一个事件都没被丢** |
| 实际后果 | 请求带着超长 prompt 发出 → 服务端 `400 exceed_context_size_error` → `BadRequestError`（不在 `_TRANSIENT` 内，不重试） |
| 代价 | 该次 prefill **204 秒全额支付**后才被拒绝 |
| 循环上限先到 | `max_iterations` 常先于 `n_ctx` 触发，故**自然长跑观测不到溢出**，必须显式构造才能测到 |

同时实测出三个**既有缺陷**（均与"当前任务不受约束"同源）：

| 缺陷 | 实测 |
|---|---|
| **prefix 逐事件截断产生悬空 `tool` 消息** | `tool(tool_call_id=c_OLD2)` 之前没有对应 assistant ⇒ 违反 ADR-052 §5 不变量 2 |
| **`mem` 定位错** | prefix 被丢光时 `kept_prefix=0` ⇒ `insert(1)` ⇒ mem 落到**任务锚点之后**，而它描述的是锚点**之前**的历史 |
| **`mem` 不计预算** | `used` 不含 mem，但它照发给模型、占上下文 |

结论：ADR-034 的"当前任务永不截断"在**长任务下必然硬失败**（而非优雅降级），
且保护当前任务所依赖的机制本身有链完整性缺陷。

---

## 决策

### 1. 术语（本 ADR 引入，已进 `CONTEXT.md`）

| 术语 | 定义 |
|---|---|
| **任务锚点（task anchor）** | 最新一条 `agent_started`。消息形态 `role:"user"` + `Task: <input>`。**永不截断** |
| **轮（round）** | 一条 `agent_thought`（带 `tool_calls`）+ 其后**全部** `parent_event_id` 指向它的 `tool_result`。**截断的最小单位** |
| **不可再精简集（irreducible set）** | `SYS + mem(prefix) + 任务锚点 + mem(suffix) + 最后 N 轮`。内部全部不可丢 |

> 与 ADR-040 的 **sandbox anchor** 是**不同概念**，术语表已加限定语区分。

### 2. 截断规则改为统一在**轮边界**

保护集 = `{ 任务锚点事件 } ∪ { 最后 N 个完整轮 }`；丢弃单位 = **完整轮**
（`agent_thought` 与其全部 `tool_result` **同进同出**）。

- **prefix 与 suffix 用同一套按轮规则**（当前 prefix 是逐事件，suffix 完全不截断）；
- **任务锚点事件永不丢**，但**不保护"锚点轮"** —— 锚点轮的工具轮可被丢，语义由 `mem(suffix)` 替代；
- 目标形态：

```
情况 1（prefix 超限，suffix 装得下）:
  [SYS][mem(prefix)][tool97..100][user_prompt2][tool1][tool2][正在执行:tool3]

情况 2（suffix 自身超限）:
  [SYS][mem(prefix)][user_prompt2][mem(suffix 中段)][…最后 N 轮…]
```

### 3. 预算按**实际发出的 messages** 度量

弃用 `_count_tokens(str(ev.payload))` —— 它数的是**变换前**的中间态，
而实际发出的是经 `assemblable()` 剥离、`truncate_output()` 截断（含 marker）、
`content_filter` 过滤后的消息。实测偏差 **2.4x ~ 24x**（第三方工具未在写入侧截断时）。

按终态度量是"保证请求不超 `n_ctx`"这一目的的**唯一正确口径**。

### 4. 两套口径，语义分离

| 用途 | 口径 | 语义 |
|---|---|---|
| 正常**丢弃**判定 | `budget_tokens = context_window × budget_ratio` | 策略：留多少余量 |
| **raise** 判定 | `context_window − max_tokens` | 物理：装不下 |

**理由**：`budget_ratio` 是调优旋钮（可配、无下限校验），**不应成为崩溃开关**；
raise 的语义是"物理上做不到"。实测依据：默认 `budget_ratio=0.8` 时单轮几乎不可能超预算
（需一轮约 52 个工具调用），而 `budget_ratio=0.1` 时**单次工具调用即超** ——
后者是配置问题，不该以硬失败形式惩罚。

### 5. `mem` 定位改为**事件级**，并**计入预算**

- 定位：按 `after_event_id` 定到**原位**，弃用 `kept_prefix + 1`（该索引随丢弃变化，
  在 prefix 丢光时恒为 1 ⇒ 落错位置）；
- 计预算：`SYS + mem(prefix) + mem(suffix)` 构成 **fixed-cost**，参与判定
  （此前注释称 "never count against budget"，实为"不计入**判断**"而非"不占上下文"）；
- **支持 2 个 `mem`**（prefix 区 / suffix 区各一），弃 "at most one" 契约。

### 6. 失败语义：不可再精简集超限 ⇒ **fail loudly**

```
SYS + mem(prefix) + 任务锚点 + mem(suffix) + 最后 N 轮  >  context_window − max_tokens
    ⇒ 报错（meta 信号 + AgentRuntime 返回可诊断失败状态）
```

- **框架不自行压缩**：`compress()` 触发权仍归 app（ADR-038）；
- 框架职责是**报告不可满足**，并在 `meta` 中给出实际/预算 token 与原因分类
  （"单个工具结果即超" / "多轮累积超"），使责任方可诊断；
- 误差信息应在**付费前**产生（当前是 prefill 全额支付后才由服务端拒绝）。

### 7. 新增配置项

```python
# harness/interfaces.py  AgentSpec
context_min_keep_rounds: int = 1   # 不可再精简集中的完整轮数；< 1 ⇒ ValueError
```

仅约束"轮"；任务锚点独立于轮，不受该值影响。

---

## 八项配套改动（A–H）

| # | 改动 | 位置 |
|---|---|---|
| **A** | `assemblable()` 悬空 `tool` **配对丢弃**（与"结果缺失删整轮"对称） | `prompt_assembly.py:184-196` |
| **B** | suffix 按**完整轮**截断（保护任务锚点 + 最后 N 轮） | `:318-336` |
| **C** | prefix 截断改为**整轮** | `:330-335` |
| **D** | 预算按**实发 messages** 度量（含 `truncate_output` marker） | `:327/:331` |
| **E** | `mem` 定位改**事件级** | `:346-352` |
| **F** | `mem` **计入预算**（fixed-cost） | `:289` |
| **G** | **fail loudly**（meta 信号 + 可诊断失败状态） | `meta_out` / `impl.py:347` |
| **H** | 支持 **2 个 mem** | `:292/:294/:347` |

### 实现顺序（有依赖）

```
A + C（轮完整性） → D（口径） → B（prefix/suffix 统一按轮） → E/F/H（mem） → G（失败语义）
```

**A 与 C 是同一问题的两面**（A 修"结果侧"不对称，C 修"丢弃粒度"），
**只做一个仍会产出悬空消息**。

---

## 硬约束（不可违反）

1. **丢弃单位 = 完整轮** ⇒ `tool_call_id` 配对恒定完整（ADR-052 §5 不变量 2）；
2. **`SYS` 与任务锚点事件永不丢**；
3. **`N ≥ 1`**，否则 `ValueError`；
4. **两套口径语义分离**（见决策 4）；
5. **`mem` 内容归 app**：harness 只**预留接口与位置**，不生成摘要；
6. **实现顺序**遵循上节依赖。

---

## 后果

**正面**
- 长任务不再硬失败：上下文受控降级（丢中间轮）或显式报告不可满足；
- 消息链合法性在所有截断形态下得证（A+C 修掉实测的悬空 `tool`）；
- `mem` 落位与语义一致（它概括的历史就在它所在的位置）；
- 失败发生在付费前，且带可诊断原因。

**负面 / 代价**
- `build_messages` 需**两趟**（先构造终态再按预算决定丢弃），
  但 `_count_tokens` 已走本地服务端 `/tokenize` 且带 memo 缓存（实测 43 次计数 789ms），增量可控；
- 需引入**轮**这一显式抽象（此前仅隐含于 `assemblable()` 的配对逻辑）；
- `estimate_tokens()` 与 `build_messages()` 的口径差会因决策 3 而**放大** ——
  数值统一归 **L1-04**，本 ADR 只统一文档口径；
- `AgentRuntime` 当前丢弃 `meta`（`impl.py:347`），**G 是它的首个消费者**，
  需定义失败状态形态 —— 与 **L3-01（`max_iterations` 耗尽语义）同类**，建议同批处理。

## 被否决的方案

| 方案 | 否决理由 |
|---|---|
| 保持"当前任务永不截断"，仅调大 `context_window` | 物理上限（6 GiB VRAM 下 harness 侧 `n_ctx` 天花板）不可无限调大；且服务端真实容量与声明值需校验（L3-02） |
| 框架内自动 `compress()` | 违反 ADR-038：压缩触发权归 app |
| 丢弃最新轮以压进预算 | 模型看不到刚发生的事，会反复重试同一动作；是**静默错误**，比报错危险 |
| raise 判定用 `budget_tokens` | 使 `budget_ratio` 从调优旋钮变成崩溃开关 |
| 让最后一个工具轮豁免截断 | 该轮自身超预算时无解，等于把硬失败推后，未消除 |
| 把 `mem` 数量简单改为 2 而不改定位 | `kept_prefix` 在 prefix 丢光时恒为 0，位置错误依旧（实测） |

---

## Rev 注记

**Rev 1（2026-10-01，原始采纳）**：本文档为初版。
对应的 ADR-034 修订以**注记**形式追加（保留原文以维持审计链）。

## 参考资料

- **证据**：`docs/evidence/l3-03-grill-evidence.md`（8 节，含一个**无效实验的记录**）
- **复现脚本**：`scripts/probe_truncation.py`（截断作用域）、
  `probe_suffix_truncation.py`（悬空 `tool`）、`probe_msg_order.py`（mem 落位）、
  `probe_llm_400.py`（400 传播路径）、`probe_context_wall.py`（声明值高于真实容量）
- ADR-034（记忆压缩/预算截断）、ADR-038（原语 vs app 边界）、
  ADR-040（sandbox anchor）、ADR-052（装配 vs dsh 差异与不变量）
- 不变量测试：`tests/test_prompt_assembly_invariants.py`（4 条 × 7 场景）

---

## 2026-10-04 修订：截断降级为「仅防溢出」的兜底

**原行为**：`budget_tokens = context_window × budget_ratio`（ratio 默认 0.8）。装配在约 **64% 模型窗口**时就开始逐轮丢弃。

**问题（实测）**：这是**机制替 app 做策略决定** —— 用多少上下文属 app 层（ADR-001 的高水位状态机）。更直接的后果是：**截断会封顶装配成本**，于是 app 的高水位判据**永远不可达** —— 上下文涨不到那个本该触发压缩的位置。实测一次 4K 窗口运行：预算 3200，cost 恒在 1,391（43%），`drop` 已从 3 涨到 6，却因 43% < 85% 而**零压缩**。

**修订后**：
```
hard_limit   = context_window − output_reserve    （物理上限，含输出预留）
                                  # output_reserve = max_tokens（显式时）
                                  #                = output_reserve_tokens（默认 16384）
budget_tokens = min(context_window × budget_ratio, hard_limit)
默认 budget_ratio = 1.0  ⇒  budget_tokens = hard_limit
```
- **默认不再提前截断**：只在请求将超过硬上限时丢弃，且**只丢到刚好装下**（最少轮次）。
- `budget_ratio < 1` 仍保留为**部署主动收紧**的旋钮；它与 `hard_limit` **解耦** —— 降低 ratio **不得**导致"装不下"的失败（`hard_limit` 是物理量，`budget_tokens` 是策略量；此不变量由 `test_hard_limit_accounts_for_output_reservation` 守住）。
- **每次截断在审计日志打警告**：`prompt_truncated`（category `context`，level `warn`），detail 含 `dropped_rounds` / `dropped_event_count` / `kept_rounds` / `budget_tokens` / `hard_limit` / `unresolvable`，并注明"这是兜底触发的丢弃，而非 app 的记忆策略"。按丢失形状去重（组装会被预览多次调用）。
  - 语义：截断意味着 **app 的压缩没有及时生效**，这是运维必须看见的条件，静默会掩盖真实配置问题。

**未变的不变量**：按轮丢弃（不产生孤儿 `tool` 消息）、任务锚点永不丢弃、`meta` 继续报告 `truncated` / `dropped_rounds` / `kept_rounds`、不可满足集上报而非发超长请求。

**测试**：`tests/test_truncation_is_fallback_only.py`（9 项：上限内不截断、默认不提前截断、只丢最少、结果必不超限、锚点存活、无孤儿 tool、输出预留、meta 完整、runtime 告警）。



---

## 2026-10-06 修订（二）：记忆插槽化、审计拆级、划归 app

**背景**：ADR-038 的边界原则 + Pi harness 的实测实践（核心只提供「触发 + 扩展点」，`怎么压缩` 由扩展负责；其自动 compaction 在「上下文溢位 / 超过保留门槛」触发，保留最近约 2 万 token，旧讯息摘要成 context handoff 并**逐级链式压缩**）共同指向：harness 不应持有"压缩策略"。

### (a) 划分（prefix/suffix）移交 app，harness 只留两个固定插槽

| 旧 | 新 |
|---|---|
| harness 按 `after_event_id` **相对锚点推断**摘要属 prefix 还是 suffix（`mem_prefix`/`mem_suffix` 两个 list） | **删除推断**。harness 只提供两个**固定位置**：`slot="before_task"` / `slot="after_task"` |
| `after_event_id` 兼管「折叠边界」与「区域判定」 | **只留折叠边界**；区域由 app 经 `slot` 显式指定 |
| 渲染 `[memory] {content}` | 渲染 **`<mem>{content}</mem>`**（占位符由 harness 提供） |

**为何占位符必须由 harness 拥有**：它让插槽**自识别**。若由 app 决定（或无占位符），替换时无法区分插槽与 **Task 锚点**，可能连带删除任务本身。

**role 恒为 `user`**：`assistant` 会破坏 tool-call 配对契约；`system` 会改变位置 0 的缓存前缀（KV 复用失效）。

**装配顺序**：`前序任务(prefix) → slot_before → Task → slot_after → 当前任务轮次(suffix)`。插槽**紧邻 Task**。

**SYS 与 Task 永不截断**（SYS 硬编码于 `messages[0]`；anchor 无条件加入保护集）⇒ 截断的作用域精确地是**历史**。

### (b) `inject_summary` 契约

```python
inject_summary(session_id, content="", *, slot, after_event_id=None)
```
- `slot` **必填**（harness 不再猜测）
- **顶替机制属 harness**：一个插槽至多一个摘要。日志为 append-only（store 无 rewrite 原语，保留历史是目的），故"顶替"是**装配期的读规则**：每个 slot **取最新者**，旧的自然不再装配。app 无法删除事件，也不需要能删。

### (c) 审计拆为两个不同结果（不再是一个 umbrella 事件）

| 情形 | 事件 | level | 语义 |
|---|---|---|---|
| 丢弃历史后装得下 | **`history_context_truncated`** | **warn** | 兜底生效（常态）。名字说明**截的是历史**；SYS 与 Task 未被截断，故 `prompt_truncated` 名不符实 |
| 无历史可丢仍超限 | **`over_budget_unresolvable`** | **error** | app 侧**固定开销**（SYS + 插槽）本身超过窗口 ⇒ 配置错误，非兜底 |

两者的 detail 均含 `preserved: ["system", "task", "memory_slots"]`，使事件名**自证**。

### (d) 删除：`memory.compress()` 与 `memory-compressor` spec

它是**平行压缩机制**（程序化注册的 agent spec），其模型 id `deepseek-v4-flash` **不存在**（API 只接受 `deepseek-flash` / `deepseek-v4-pro`）⇒ 一旦调用必然失败；使用者仅其自身测试。压缩归 app 后它没有角色。

### (e) SDK 公开面（消除 app 的私有调用）

| 新增 | 替代 |
|---|---|
| `client.sessions.events.logical(sid)` | `client._logical_events`（私有包装；物理日志读面另有 `list()/filter()/find()`） |
| `client.memory.count_tokens(text, provider=None)` | `from harness.prompt_assembly import _count_tokens` |
| `client.config.profile(name)` / `client.config.profiles()` | `from harness.sdk.config import resolve_llm_profile` |

`client.policy.add/enable` 与 `client.hooks` **本已存在**，app 直接用即可；装配改用 `client.sessions.preview_llm_messages`（与 runtime 同一函数），不再需要 `client._store`。

### (f) 测试

`tests/test_memory_slots.py`（8）：插槽紧邻 Task、`<mem>` 占位符与 role、同插槽顶替、`slot` 必填、审计拆级（warn/error + `preserved`）、三个公开面存在。

**已知依赖（未在本轮修）**：云部署下回退 tokenizer 的 CJK 计数为 0（见 `merge_TODO.md` §4.1），会使 app 的高水位判据**系统性低估** —— 端到端验收需在该缺口修复后重做。

## 2026-10-19 修订（R1）：请求输出上限与预算输出预留是两个独立旋钮

`max_tokens` 只决定**请求里是否发送该字段**（`AgentSpec.max_tokens=None` ⇒ 省略，由后端默认输出上限）；它不应决定**预算是否预留输出空间**。因此新增 app 层配置 `AgentSpec.output_reserve_tokens: int = 16384`（profile/调用方可覆盖），仅参与预算：

```
reserve      = max_tokens if max_tokens is not None and max_tokens > 0 else output_reserve_tokens
hard_limit   = max(1, context_window − min(reserve, max(0, context_window − 1)))
```

- 默认 `max_tokens=None` 时仍预留 16,384 tokens，prompt **不能**填满整个窗口；
- 显式 `max_tokens` 时使用显式值（请求与预算一致）；
- 预留被钳制到 `context_window − 1`，保证小窗口下 `hard_limit >= 1`。

**测试**：`tests/test_context_round_truncation.py::test_output_reserve_applies_when_max_tokens_is_omitted`、`test_explicit_max_tokens_wins_over_output_reserve`、`test_output_reserve_never_makes_hard_limit_nonpositive`、`test_agent_spec_output_reserve_reaches_the_preview_hard_limit`。


## 2026-10-19 注记（R1 修正）：slot 边界语义与旧 summary 兼容

- **边界是 per-slot、且永不删除 Task 锚点**：`after_event_id` 只折叠**本 slot 所属区域**中位于边界之前的事件（`before_task` 只在 prefix，`after_task` 只在 suffix）；Task 锚点不属于任何区域，故任何边界都不可能吞掉它。边界不在本区域时忽略（summary 仍占固定位置）。
- **旧格式 summary（无 `slot`）**：ADR-053 rev2 之前的 `memory_summary` 没有 slot，harness 无从判定位置，**不装配**；`build_messages` 在 `meta.ignored_legacy_summary_count` 报告数量，避免静默消失。迁移方式：按 ADR-001 的 region 重新 `inject_summary(..., slot=...)`（prefix→`before_task`，suffix→`after_task`）；旧事件留存审计。
- `client.config.profiles()` 依赖的 `available_llm_profiles()` 已补齐（project configs/llm.yaml + user/system 目录）。
- 审计 guard 测试改为断言 emitted event 名（`history_context_truncated` / `over_budget_unresolvable`），不再以注释含 `prompt_truncated` 为证据。


## 2026-10-19 修订（I-64）：小窗口 reserve 钳制与 per-build cost 上下文

- **小窗口预算**：R1 的 `min(reserve, context_window − 1)` 会让 3200 窗口的默认
  16384 预留把 `hard_limit` 压到 1，prompt 不可用、app 的 85% 高水位不可达。
  I-64 改为：

```
reserve_cap       = max(1, ceil(context_window / 4))
effective_reserve = min(reserve, reserve_cap)     # reserve = max_tokens 或 output_reserve_tokens
hard_limit        = max(1, context_window − effective_reserve)
```

  `max_tokens` 的**请求字段原值不变**（仍按原值发送）；只有预算的
  `effective_reserve` 被钳制，`meta` 同时报告 `output_reserve_tokens`（原始）
  与 `output_reserve_effective`（钳制后）。大窗口（如 100K、16K reserve）不受影响。
  测试：`test_output_reserve_is_capped_to_a_quarter_of_the_window`、
  `test_tiny_window_keeps_a_reachable_high_water_mark`、
  `test_explicit_max_tokens_is_also_capped`、`test_output_reserve_applies_when_max_tokens_is_omitted`。

- **per-build cost 上下文**：`build_messages(..., cost_map_out=dict)` 填充本 build
  的 `(id(event), passback) -> (event, cost)` 快照；`_cost(..., cost_map=...)`
  使用显式上下文。全局 `_LAST_COST_MAP` 仅作兼容回退，**仅最近一次 build 精确**；
  交错/并发场景必须传显式 map（否则会读到别的 build 的 pairing/skip 上下文）。
  测试：`test_cost_map_out_is_per_build_and_interleaved_builds_stay_exact`、
  `test_cost_map_out_is_safe_across_concurrent_builds`。
