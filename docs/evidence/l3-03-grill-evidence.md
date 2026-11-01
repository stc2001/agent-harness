# L3-03 grill 实测证据（2026-10-01）

> 本文件记录 L3-03 规格收敛过程中每个论断的**原始实测数据**。
> 所有脚本位于 `scripts/`，均可复跑。
> 环境：`GTX 1060 Max-Q 6 GiB / 15.6 GiB RAM`，llama-server `-c 131072 -ctk q4_0 -ctv q4_0`,
> harness build = OH_workspace 当前 HEAD。

---

## 1. 截断作用域：prefix 生效，suffix 不生效

脚本：`scripts/probe_truncation.py`　预算 `104,857`（`128000 × 0.8`）

| 场景 | 总 token | 消息内 token | dropped | truncated | 结果 |
|---|---|---|---|---|---|
| A. 单任务，未超预算 | 21,707 | 21,606 | 0 | False | 正常 |
| B. 多任务，**prefix** 超预算 | 216,148 | 102,284 | **14** | **True** | ✅ 截断生效 |
| C. **suffix（当前任务）**超预算 | 351,242 | **340,929** | 6 | True | ❌ **仍超预算 236,072** |

**结论**：截断逻辑本身工作（B 证明）；坏的是**作用域**——
`suffix_events`（`:320` `keep ALL`）的 token 只作 `suffix_cost` 基数
（`:327-328`），**永不参与** `:332` 的超预算判断。

`estimate_tokens` 与消息内实际 token 的差：场景 B **差 113,864**
（前者统计全部事件，后者排除 suffix）→ 两个口径未对齐。

---

## 2. prefix 逐事件丢弃 ⇒ 悬空 `tool` 消息（不变量 2 被违反）

脚本：`scripts/probe_suffix_truncation.py`

事件顺序（轮）：`OLD1 OLD2 | [R1 锚点] R2 R3 R4`
喂入：`OLD1 OLD2 | [R1 锚点] R3 R4`（R2 已按轮预丢弃）
预算压到 `160` token ⇒ `build_messages` 的 prefix 循环也会丢弃

产出：

```
[0] system  SYS
[1] tool    '结果 OLD2'   << c_OLD2      ← ⚠️ 悬空
[2] user    'Task: 任务 R1'                （任务锚点）
[3] assistant ['c_R1']
[4] tool    << c_R1
[5] assistant ['c_R3']
[6] tool    << c_R3
[7] assistant ['c_R4']
[8] tool    << c_R4

meta: dropped_event_count=3  truncated=True
检查: 结果无对应 tool_call 的 : {'c_OLD2'}   ← 唯一失败项
```

**根因**：`:330-335` 是**逐事件**丢弃

```python
for ev in reversed(prefix_events):
    ev_tokens = _count_tokens(str(ev.payload), ...)
    if used + suffix_cost + ev_tokens > budget_tokens:
        break                       # ← 逐事件 break
    recent.insert(0, ev)
```

丢弃边界落在轮中间时，`assistant(tool_calls)` 被丢、其 `tool` 结果留下：

```
OLD2 轮 = [assistant(c_OLD2)] [tool(结果 OLD2)]
                ↑ 预算用尽从这里断              ↑ 保留 ⇒ 悬空
```

**注**：`assemblable()`（`:184-196`）只剥"缺结果的轮"，**不剥"缺调用方的结果"** ——
所以这条悬空消息能一路发到服务端。这就是 Grill #1 的不对称。

**对照（prefix 全保留时）**：suffix 内丢 R2 **不产生**任何错误
（锚点在位、配对完整、无连续同角色）→ 说明问题**不在** suffix 丢轮本身，
而在 **prefix 的逐事件粒度**。

---

## 3. `mem` 落在任务锚点之后（prefix 丢光时）

脚本：`scripts/probe_msg_order.py`　预算 `7200`

```
[0] system    SYS
[1] assistant ['p6']                    ← 旧任务保留轮
[2] tool      << p6
[3] assistant ['p7']                    ← 旧任务保留轮
[4] tool      << p7
[5] user      'Task: 当前任务 user_prompt2'   ← 任务锚点
[6] user      '[memory] 旧任务摘要'          ← ⚠️ mem
[7] assistant ['c1']                    ← 当前任务
[8] tool      << c1
```

**根因**：`:346-352`

```python
# memory_summary ... lands right after the current-task anchor
recent.insert(len(recent) - len(suffix_events) + 1, memory_summaries[0])
#             └── 保留的 prefix 条数 ──┘   └ +1 = 跨过锚点
```

`kept_prefix` 在 prefix 被丢光时为 **0** ⇒ `insert(1)` ⇒ 落在**锚点之后**。
而该 mem 描述的是**锚点之前**的历史 ⇒ **顺序反了**。

**对照**：prefix 未被丢时（`kept_prefix = 2`）mem 落在 `[p6][p7]` 与锚点**之间**，
即 prefix 区内 —— **正确**。

⇒ **mem 归属错只在 `kept_prefix = 0` 时发生**，与"只有一个 mem"无关：
把 mem 数量改成两个**修不了**这个位置错误（公式里的 `kept_prefix` 仍为 0）。

**这是"只做 E 不做 H"也能让情况 1 完全正确**的依据。

---

## 4. 超长请求的 400 传播路径

脚本：`scripts/probe_llm_400.py`

直接 HTTP：

```
HTTP 400  request (216167 tokens) exceeds the available context size (131072 tokens)
          type=exceed_context_size_error
```

经 `AgentRuntime._call_llm`（确定性构造超长 messages，绕过模型）：

```
constructed prompt ~= 135,004 tokens vs server n_ctx=131,072
RAISED BadRequestError after 204s
  400 - request (135165 tokens) exceeds the available context size (131072 tokens)
==> the 400 DID propagate
```

**`BadRequestError` 不在 `_TRANSIENT`（`impl.py:161-166`）内 ⇒ 不重试，直接抛出。**

注意 **204 秒全在 prefill** —— 服务端先填完才拒绝，长 prompt 的代价已全额支付。

---

## 5. 一个**无效实验**的记录（防重犯）

`scripts/probe_context_wall.py` 第一版**没能验证它想验证的东西**：

它依赖模型"碰巧"调用 bash（`Run exactly this bash command …`）来产生巨大工具结果，
但小模型**没有发出工具调用**，直接把命令文本当回答返回 ⇒ 大文件从未进入上下文。
服务端日志显示那次调用 prompt 仅 **1,293–2,470 token**，**什么都没溢出**。

而日志里确实有一条 **216,167 token 的 400**，但那是同脚本**第 1 步的原始 HTTP 探测**
造成的，不是 harness 路径。差点据此误报"harness 吞掉 400 并报告成功"。

**教训**：验证错误处理路径时不要依赖模型"碰巧"触发条件 ——
应**直接构造输入并调用被测函数**（`probe_llm_400.py` 就是这么做的，结果 `BadRequestError` 正常抛出）。

---

## 6. `truncate_output` 的两层防御（修正一个夸大结论）

`truncate_output` docstring（`:242-248`）：

> "Applied at the tool layer first; `build_messages` re-applies it as a **fallback for
> third-party tools**."

| 层 | 位置 | 作用 |
|---|---|---|
| **写入侧** | `file_io.py:128`、`sandbox_exec.py:63,86` | 内置工具返回前就截到 8192 字符 ⇒ **session 里存的就是截断后的** |
| **组装侧** | `prompt_assembly.py:470` | 兜底，保护第三方工具 |

**修正**：早期测出的"24x 计数偏差"在**内置工具**上**不成立**（session 内存的已是截断版）。
那个偏差只在**第三方工具**（写入侧未截断）时出现；当时的合成测试绕过了写入侧，**夸大了范围**。

但**预算口径问题仍然成立**：预算数的是 `str(ev.payload)`（未截断），
消息里发的是截断版 —— 对第三方工具两者不等。

---

## 7. 单轮规模上界 ⇒ "单轮超预算"的真实触发条件是配置

`_execute_tool_calls:407` **不限制** `tool_calls` 数量；单轮受 `max_tokens` 约束。

```
单轮 ≈ max_tokens(≤16384) + N_tool_calls × ~2000
```

| tool_calls 数 | 单轮上界 | 占预算(104,857) |
|---|---|---|
| 1 | 18,384 | 17.5% |
| 16 | 48,384 | 46.1% |
| 52 | 120,384 | **114.8%** |

**默认 `budget_ratio=0.8` 下，单轮几乎不可能超预算**（需一轮约 52 个工具调用）。
真正会触发的场景是 **`context_budget_ratio` 被调小**（可配、无下限校验）：

| budget_ratio | 预算 | 单轮就超 |
|---|---|---|
| 0.8（默认） | 104,857 | 几乎不可能 |
| **0.1** | **13,107** | **单次工具调用（18,384）即超** ❌ |

⇒ "单个工具轮超预算"在实践中主要是**配置问题**，不是工具问题。
这也是 raise 判定用 `context_window − max_tokens`（物理）而非 `budget_tokens`（策略）的实证依据。

---

## 8. 消息链合法性：不变量 2 当前会被违反

`tests/test_prompt_assembly_invariants.py:124` 定义了不变量：

```python
def test_tool_messages_have_matching_call_id(name):
    """每条 tool 消息必须有 tool_call_id，且能对应到之前的 assistant tool_calls。"""
```

实测（§2）该不变量**会被违反**，但**现有 77 个测试全绿** ——
因为现有 `SCENARIOS` 里没有"prefix 被逐事件截断"的用例。

⇒ L3-03 需**新增 scenario**覆盖该形态，让不变量真正生效。

**ADR-052 §5 的 4 条不变量需增补第 5 条**：整轮边界。
