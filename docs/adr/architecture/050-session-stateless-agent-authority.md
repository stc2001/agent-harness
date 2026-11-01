# ADR-050：Session 无状态化与 Agent 权威状态层（I-31）

## Status

Accepted (2026-09-07)

## Context

运行链路证据：AgentRuntime 只写 append-only store 并自建逻辑视图；SessionManager 状态机（status 推断/transition 校验）仅服务管理面展示与 gate 落盘，其 job 级终态（_TERMINAL_SUCCESS）随 ADR-038 删 DAG 成为死语义（生产无 job_* 发射者）——真实 agent 会话被推断为永远 RUNNING；且会话与 agent 两套状态易混。

## Decision

- **Session = 无状态 append-only 日志**（ADR-037 store 唯一事实源）+ 绑定记录（metadata.instance_id 保留）；不再有进程内 session 状态机。
- **agent 实例状态 = 权威状态层**（ADR-043）：持有关系从 pool 派生——running/idle = 持有；stopped/disposed/missing = unbound（无物理解绑，Q2-b 派生）。
- `rewind/fork/checkpoint` 保留为**日志级运算**（fork 映射顶层 parent_event_id）；执行守卫以派生持有为事实源：绑定 agent RUNNING 时拒绝（SessionBusyError 语义），IDLE/unbound 放行。
- `sessions.show` 契约去推断 status，返回 event_count/events + holder_instance_id/holder_status（breaking，直接改）。
- EventBus（gate 事件）本就薄封装 store append（无 prod 接线，保持）。
- 观测：CLI logs/session show agent JSON 带 holder；SessionStatus 枚举保留仅展示兼容。


### I-41 注记（2026-09-30）：读面契约落实

**契约**：逻辑视图（rewind 标记重放）是日志层唯一读面；runtime 与 SDK 读面**不得直接消费物理日志**。

**实现**：`harness/session/impl.py` 提供模块级纯函数 `logical_view(physical)` 与公开入口 `SessionManager.logical_view(session_id)`；`AgentRuntime._agent_loop` 改为 `assemblable(logical_view(store.read_events(sid)))`，runtime 只组合、不实现投影。

**验收**：`tests/test_i41_logical_view.py` 以「模型实际收到的 messages」为信号——rewind 后旧轮不得出现（修复前可见）。

**遗留（待 app 层确认）**：`sdk/client.py` 仍有 11 处直接读物理日志（`_terminal_output`、holder、`session show`、`history_*`、prompt preview 等），rewind 后这些读面仍显示被回退内容。


### I-42 注记（2026-09-30）：读面二分 —— 语义（逻辑）vs 审计（物理）

session 的物理日志（`events.jsonl`）与逻辑视图（rewind 重放）**用途不同**，必须显式二分，避免"契约声明了但消费者各读一半"：

| 读面 | 语义 | 数据来源 | 命名约定 | 用途 |
|---|---|---|---|---|
| **语义读面** | 逻辑视图（rewind 已重放） | `SessionManager.logical_view()` | 现有 API 名（`sessions.events.list/get/stream`、`memory.*`、`RunResult.output`、`history_*`） | 编排、模型上下文、app 决策 |
| **审计读面** | 物理日志（append-only 全量，含 `session_rewound` 与被回退内容） | `SessionStore.read_events()` | **`audit_` 前缀**：`sessions.audit_events(sid)`、`sessions.audit_path(sid)` | 调试/对比/合规取证 |

**实现规则（可被测试守卫）**：

1. `store.read_events()` 只允许出现在两处：`harness/session/impl.py`（`logical_view` 的实现）与 `_SessionsClient.audit_events()`；
2. 其余一切读点必须经 `HarnessClient._logical_events(session_id)`（内部即 `SessionManager.logical_view`）；
3. 新增源码扫描型 fitness 测试，防止回退。

**CLI**：`harness logs <sid>` 默认走**语义读面**；`--audit` 切到**审计读面**（用于对照两级视图定位"内容去哪了/为什么模型没看到"）。


### I-43 注记（2026-09-30）：读面三分 —— 审计 / 语义 / **组装产物预览**

I-42 确立了审计（物理）与语义（逻辑）二分；本注记补上第三面：**组装产物（模型实际收到的上下文）**。

| 读面 | API | 内容 | 参数依赖 | 副作用 |
|---|---|---|---|---|
| 审计（物理） | `sessions.audit_events(sid)`、`audit_path(sid)` | append-only 全量（含被 rewind 内容） | 无 | 无 |
| 语义（逻辑） | `sessions.events.list(sid)`、`SessionManager.logical_view(sid)` | rewind 已重放；事件级；不截断 | 无 | 无 |
| **组装产物（预览）** | `sessions.preview_llm_messages(...)` | 与 `_call_llm` **一致**的 messages（含预算截断/折叠/passback/形状剥离） | `context_window / budget_ratio / provider / reasoning_passback / content_filter` | **只读**：不落盘、不 acquire、不调 LLM |

**命名**：预览 API 一律带 `preview`，避免被误当作会驱动执行的“真 API”。

**单一真源（硬约束）**：runtime 与 SDK 预览必须调用**同一个函数** `AgentRuntime.llm_messages(...)`；
否则预览会与真实请求漂移（I-41 的教训：契约声明 ≠ 消费者遵守）。

**预览三模式**：

1. `task=` 给定 → 当前逻辑视图 + **合成**一条 `agent_started`（不落盘），预览“这条 Task 发出去会看到什么”；
2. `event_id=` 给定 → 以该事件为界的**历史**上下文（`≤ event_id` 的日志前缀，经逻辑视图 + 形状剥离）；
3. 两者皆无 → 当前视图（等价于“下一次调用将发送的内容”）。

**必填 `instance_id`**：上下文窗口/预算比/passback/system prompt 都来自实例的 spec；实例不存在 → **显式报错**（不得静默兜底）。

**历史保真的显式假设**：事件日志不存 system prompt（来自 spec 或 per-call 覆盖），历史预览默认用**当前 spec 值**，
并在 `meta.system_prompt_source` 中标注（`current-spec` / `override`）——不假装它能还原历史 system prompt。

**返回 meta（回答“内容去哪了”）**：`truncated` / `dropped_event_count` / `stripped_dangling` / `folded_summary` / `as_of_event_id` / `synthetic_task`。

**CLI 对齐**：`harness preview <instance_id> --session <sid> [--event-id <eid> | --task <text>] [--json]`。

## Consequences

- 正面：消除「永远 RUNNING」误判与 job 死语义；状态单一权威（agent）；会话可跨任务续跑不受状态机阻断。
- 代价：show 契约 breaking；tests/test_session 重写为日志语义；holder 跨进程查不到运行时 → 显示 unbound（文档注明）。

## References

- ADR-037/039/043（注记已同步）、I-31、ADR-049