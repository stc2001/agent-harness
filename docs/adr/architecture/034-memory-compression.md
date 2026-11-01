# ADR-034：记忆系统 — 会话上下文压缩与摘要注入

**日期**: 2026-05-31
**状态**: 已采纳（**部分被 ADR-053 修订** — 见下方 Rev 4）

> ⚠️ **Rev 4（2026-10-01）部分修订**：本 ADR 关于**当前任务截断**与
> **`memory_summary` 预算口径**的若干陈述已被 **ADR-053** 修订。
> 原文保留以维持审计链，但**下列声明不再成立**，请以 ADR-053 为准：
>
> | 本文位置 | 原声明 | 现状 |
> |---|---|---|
> | Rev 3 注记 2 | 「当前任务…**永不截断**」「当前 in-flight 任务的 prompt 与 tool 往返受保护」 | **已修订** —— 单任务长程下必然硬失败（服务端 `400 exceed_context_size_error`）。改为**按轮截断**，保护 `任务锚点 + 最后 N 轮` |
> | 框架层 2 / Rev 2 R4 | `memory_summary`「**不计入** token 预算」 | **已修订** —— 它确实不进 `used`，但**照发给模型、占上下文**。现纳入 **fixed-cost** 参与判定 |
> | Rev 2 R2 | 超预算「从**中间**丢弃」 | 实现是**逐事件**丢弃 ⇒ 产生**悬空 `tool` 消息**（实测违反 ADR-052 §5 不变量 2）。改为**整轮**粒度 |
> | 框架层 1 | `budget_ratio` 为"安全网"，旧事件**静默**丢弃 | 静默丢弃改为**显式**：`meta` 信号 + 不可再精简集超限时 **fail loudly** |
>
> 实测数据见 `docs/evidence/l3-03-grill-evidence.md`。
> **其余内容（分层归属、原语 API、崩溃恢复语义、R1/R3/R5）仍然有效。**

## 上下文

Agent 长时间运行时会话事件持续增长。`build_messages` 的 `budget_ratio=0.8` 提供安全网——旧事件被静默丢弃，但 LLM 丢失全部历史上下文。需要一个记忆压缩机制：超出预算时自动生成摘要，替换旧事件。

ADR-017 规定控制流归 APP 层——框架不做循环、不做条件判断。因此记忆系统遵循相同的分层：框架提供原子操作，APP 层决定何时压缩、压缩多少。

## 决策

**框架层只做消息构建，不做压缩决策。SDK 层提供原子 API，APP 层编排完整压缩流程。**

### 框架层（`prompt_assembly.build_messages`）

1. `budget_ratio` 参数（默认 0.8）：`int(context_window * budget_ratio)` 为消息预算。事件从最新往前取，超出预算即截断。截断只影响内存消息列表，不修改 session 文件。

2. `memory_summary` 事件：不计入 token 预算，始终出现在消息中。`after_event_id` 字段标记截断边界——边界前的事件不入消息，边界后（含边界）正常处理。

3. Tokenizer：transformers `AutoTokenizer`，按 provider 懒加载。模块级单例缓存，项目根 `tokenizer_dir/` 存放。

### SDK 层（`client.memory`）

| API | 作用 |
|-----|------|
| `estimate_tokens(sid)` | 返回 session 事件的精确 token 数 |
| `tail(sid, keep_ratio)` | 拆分事件 → `(old_events, boundary_id)`。`old_events` 可传 `compress()`，`boundary_id` 可传 `inject_summary()` |
| `compress(events)` | 调用内置 `memory-compressor` agent 生成摘要字符串 |
| `inject_summary(sid, content, after_event_id)` | 写入 `memory_summary` 事件，payload 携带 `after_event_id` 边界标记 |

### APP 层

APP 层控制所有策略参数：

```python
TRIGGER = 0.8      # 何时触发压缩
SHRINK_TO = 0.4    # 压缩后保留比例

if memory.estimate_tokens(sid) > int(ctx * TRIGGER):
    old, boundary = memory.tail(sid, SHRINK_TO)
    summary = memory.compress(old)
    memory.inject_summary(sid, summary, after_event_id=boundary)
```

### 崩溃恢复

`build_messages` 是纯函数——输入事件列表相同，输出消息相同。Session 文件逐条 `fsync`，崩溃不丢。`SessionManager.wake()` 重建内存视图，`build_messages` 输出与崩溃前一致。

## 理由

1. **文件永不修改**。旧事件物理保留在 session 中（审计可追溯），但 `build_messages` 的索引截断使它们不再进入 LLM 上下文。符合 ADR-005（JSONL append-only）和 ADR-024（Store 是唯一全量真相源）。

2. **不需要状态机**。压缩是单次 `if` 判断，不依赖历史状态。触不触发由当前 token 数决定——一个 `estimate_tokens` 调用即可。

3. **摘要顺序正确**。`build_messages` 将 `memory_summary` prepend 到保留事件前——自然时序上摘要替代了被丢弃的旧事件。

4. **兜底仍然有效**。即使 APP 层完全不做压缩，`budget_ratio=0.8` 的安全网仍然在——最旧的事件被静默丢弃，上下文不会撑爆。

## 后果

- Session 中可能有多个 `memory_summary` 事件（每次压缩追加一条），但 `build_messages` 用 `after_event_id` 定位最新的截断边界
- `inject_summary` 不删除旧事件，旧摘要仍可审计
- `AgentSpec.context_budget_ratio` 可 YAML 配置（默认 0.8）
- `Tokenizer` 模块级懒加载，支持多 provider 并存

---

## Rev 2（2026-08-19，issue I-04/I-05/I-06/I-07）：上下文预算与消息组装修正

**问题**：原策略"事件从最新往前取、超出预算即截断"在实践中发现两个缺陷：

1. **任务消息丢失（正确性）**：`agent_started`（最早事件，任务描述）在预算超限时最先被整体挤掉——模型在长程工具调用中丢失任务描述（实测大输出下 iter3 起 shared prefix 从 4/6 跌到 1/5，任务消息消失）。
2. **缓存前缀崩塌**：截断点随预算波动移动，system + 任务前缀不稳定，供应商前缀缓存全 miss。
3. **system prompt 膨胀**：`client.memory.compress` 把事件 JSON 直接嵌进 system prompt，每次压缩 system 都变 → 跨任务/跨 wave 缓存零命中。

**决策（Rev 2 修订截断与组装策略）**：

| 规则 | 内容 |
|------|------|
| **R1 锚点不可截断** | `agent_started`（任务消息）等同 system 级别，**永不截断**（payload 本身很小） |
| **R2 中间/尾部优先截断** | 保留 system + 任务锚点 + 最新尾部；超预算从**中间**丢弃（最旧的 `tool_result` 优先摘要化，见 R4），不从头截 |
| **R3 system 常量** | system prompt 只承载稳定身份/指令（默认 `spec.system_prompt`）；任务差异一律走任务消息；`compress` 的事件 JSON 改为普通 user 消息传入，system 保持字节常量 |
| **R4 单条体积上限** | `tool_result` 单条上限 8K 字符，超限截头 + 追加 `[... truncated N chars, M lines ...]` 标记（工具层优先，`build_messages` 兜底） |
| **R5 assistant 序列化完整化** | `agent_thought`（工具调用轮）与 `agent_completed`（最终答案）序列化为 assistant 消息回传——跨任务续跑不失忆；`reasoning_content` 契约见 ADR-041 |

**保持不变的**：`memory_summary` 不计预算、始终出现在消息中；截断只影响内存消息列表、不修改 session 文件（ADR-005/024）；`build_messages` 仍是纯函数（崩溃恢复语义不变）；Tokenizer 懒加载单例。

**后果**：任务恒在（正确性修复）；前缀稳定（缓存修复）；system 跨任务恒定（缓存命中前提）；超大输出在源头止损。

**Rev 3 注记 2（2026-09-07，anchor 定位修正）**：多任务同 session 时 anchor（最新 agent_started = **当前未完成任务起点**）**不得前置到消息头**——会颠倒历史因果序（旧任务约束被读到最新）。修正为：`[旧任务历史(可截断)] + [anchor 起当前任务全部轮次](永不截断)`，当前任务恒位于列表末尾（模型续写位）；与 DSH core 不变量一致（后续请求 = 前请求的严格前缀扩展）。截断从**最旧的已完成历史**开始，当前 in-flight 任务的 prompt 与 tool 往返受保护。

**Rev 3 注记（2026-09-06/07，I-30 核实 + v4 1M）**：

- `memory_summary` 折叠契约已端到端内置：`build_messages` 以 `payload.after_event_id` 作 cutoff（其前事件忽略、不计预算、摘要插 task 锚点后）；写侧 `client.memory.tail/compress/inject_summary/estimate_tokens` 齐全——**压缩触发策略归 app**（预步 `agent.pre_step` 估算 ≥ window×ratio 即折叠），非框架缺口（I-30 关闭）。
- v4（deepseek-v4-flash）上下文窗口按 1M 配置：`spec.context_window=1000000`、默认 budget_ratio 0.8（800k 触发边界）。

**Rev 3 注记 3（2026-09-11）**：与官方 DSH 的装配差分结论见 **ADR-052**——dsh 装配期不截断（压缩由 compaction 插件改日志），本 ADR 的「装配器预算截断 + memory_summary 折叠」是**有意保留的职责划分**（折叠触发归 app）。


### I-41 注记（2026-09-30）：组装层职责边界

装配层（`prompt_assembly`）承担：**形状投影** `assemblable()`（剥离未配对工具轮）、**预算与折叠**（截断 / `memory_summary` / reasoning passback）。
**不承担**日志语义：rewind 重放归 `session.logical_view`。runtime 只做组合。

---

### Rev 4 注记（2026-10-01）：当前任务截断与 `mem` 口径移交 ADR-053

**触发**：用真实长程任务（20 页翻译 / 42 块 / 本地小模型）验证 Rev 3 注记 2 的
「当前任务永不截断」，发现该策略在**单任务长程下必然硬失败**：

| 观测 | 数据 |
|---|---|
| suffix token | **340,929**，预算 104,857 ⇒ **一个事件都没被丢** |
| 后果 | 请求带超长 prompt 发出 → 服务端 `400 exceed_context_size_error` → `BadRequestError`（不在 `_TRANSIENT` 内，不重试） |
| 代价 | 该次 prefill **204 秒全额支付**后才被拒 |

**决策移交**：当前任务截断、轮（round）抽象、不可再精简集、两套口径、
装配期失败语义，全部由 **ADR-053** 规定；本 ADR 的对应段落以上方 Rev 4 表为准。

**同时记入 ADR-053 的三个既存缺陷**（本 ADR 未覆盖）：

1. **prefix 逐事件截断 ⇒ 悬空 `tool` 消息**（实测 `tool(tool_call_id=c_OLD2)`
   之前无对应 assistant）—— 违反 ADR-052 §5 不变量 2；
2. **`mem` 定位错**：prefix 被丢光时 `kept_prefix=0` ⇒ `insert(1)` ⇒ mem 落到
   **任务锚点之后**，而它概括的是锚点**之前**的历史；
3. **`mem` 不计预算**：`used` 不含 mem，但它照发、占上下文。

**仍然有效且未被修订**：分层归属（框架给原语 / app 决定压缩触发）符合 ADR-038；
`memory_summary` 的折叠契约（`after_event_id` 作 cutoff）；R1（任务锚点不可截断）；
R3（system 常量）；R5（assistant 序列化完整化）；崩溃恢复语义（`build_messages` 纯函数、
append-only 日志）。

**证据**：`docs/evidence/l3-03-grill-evidence.md`（8 节，含一个**无效实验的记录**）。

