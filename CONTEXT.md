# Agent Harness — 项目术语表（CONTEXT.md）

> 本文件提供项目共享语言。
> 最后更新：2026-10-01 / 上下文预算与会话装配术语（ADR-053）

---

## 核心概念

| 术语 | 英文 | 含义 |
|------|------|------|
| **Gateway** | Gateway | 统一授权引擎。收敛 HumanGate + 自定义规则 + 审计日志。`authorize(ctx) → AuthorizeResult` |
| **实例池** | AgentInstancePool | Agent 实例复用。acquire/release，支持同 agent 多实例并发。含 sandbox_spawn 授权 |
| **沙箱管理器** | SandboxManager | 沙箱实例生命周期。懒创建、TTL 管理、配置变更检测 |

## 数据模型

| 术语 | 含义 |
|------|------|
| **Event** | 追加写日志中的一条记录。event_id, session_id, event_type, timestamp, source, payload, **parent_event_id**（追溯链到触发事件） |
| **AgentSpec** | Agent 的不可变定义。YAML 注册表加载。name, sandbox, tools, permissions, model |
| **AgentInstance** | AgentSpec 的注册表条目。无状态身份标识（instance_id, spec_name, llm_profile, created_at）。生命周期由 Session 接管 |
| **agent_thought** | LLM 原始响应的审计记录（含 tool_calls 和 content）。`build_messages` 不发送给 LLM，仅存 session 供调试 |
| **事件 ID 链** | `agent_thought.tool_calls[0].id` = `tool_called.event_id` = `tool_result.parent_event_id` = `agent_failed.parent_event_id`，全程同一 LLM 原始 ID |
| **parent_event_id** | Event 通用追溯字段。`tool_result.parent_event_id → tool_called.event_id`，链回触发该结果的事件 |

## 执行模型

| 术语 | 含义 |
|------|------|
## 执行模型

| 术语 | 含义 |
|------|------|
| **agent loop** | Agent 的执行循环：读 events → 调 LLM → 解析 tool call → 执行工具 → 写 events → 重复 |
| **软继续** | Soft-continue | 工具调用失败不终止 Agent Loop。仅 `finish_reason="stop"`、`timeout`、`max_iterations` 终止。参见 ADR-033 |
| **结构化返回** | Structured return | 工具可选返回 `{ok, content, error, exit_code}`。框架识别 `ok` 字段，`ok=False` 时写 `agent_failed` 但不杀 Loop |
| **配置指纹** | Config fingerprint | `str(sandbox_config)` 哈希。SandboxManager 和 AgentInstancePool 用此比对检测配置变更，触发 re-spawn / re-create |

## 隔离与沙箱

| 术语 | 含义 |
|------|------|
| **Bubblewrap (bwrap)** | Linux namespace 隔离工具。零 daemon 依赖，`bwrap` 命令行驱动。MVP 默认沙箱后端 |
| **SandboxConfig** | 沙箱实例配置。backend, firewall, shared_folders。独立于 AgentSpec |
| **SandboxManager** | 命名沙箱实例生命周期管理。懒创建，多 Agent 共享 |
| **SandboxModule** | 模块层次：`manager`（API，上层入口）→ `backends`（工厂注册表）→ `impl`（bwrap 进程原语）/ `docker_impl`（Docker 容器）。规则：**永远通过 manager 调用生命周期方法**，不直接调 impl |
| **路径级权限** | AgentRuntime 执行的逐路径 read_only/read_write 拦截。正则匹配命令中的路径 |

## 会话与持久化

| 术语 | 含义 |
|------|------|
| **Session** | 一次作业执行的完整事件日志。append-only JSONL。支持 wake/rewind/fork |
| **SessionStore** | 纯持久化 I/O。`./sessions/{id}/events.jsonl` + `meta.json`。已有 per-session 线程安全锁 |
| **SessionManager** | Session 状态机。逻辑事件视图重建（rewind 后截断），状态转换校验 |
| **rewind** | 回退到指定事件，丢弃后续事件。逻辑视图截断，物理文件只追加 |
| **fork** | 从当前事件创建新 Session 分支。复制全部事件到新 session |
| **Task（任务）** | 一次 agent_loop 执行，由 (session_id, start_event_id, agent_spec, prompt, max_iterations) 定义。Session 是任务的持久化容器，任务不是 Session |
| **start_event_id** | 任务在 Session 事件流中的起点。崩溃恢复时从该事件后的未完成事件点重放 |
| **事件追溯链** | `tool_result.parent_event_id → tool_called.event_id`。不仅是审计链，还是崩溃恢复的幂等边界：parent 有对应 result 的事件视为已完成 |
| **checkpoint** | Session 状态快照。`write_checkpoint()/read_checkpoint()`。当前保存 status + 全量事件；崩溃恢复需补 start_event_id 等任务边界字段 |
| **孤儿清理（reap_orphans）** | 宿主进程退出后残留的**沙箱锚点**树回收。**显式操作**：APP 启动时调用 `SandboxManager.reap_orphans()`。无 PDEATHSIG 自动兜底（ADR-040）。沙箱锚点带 `HARNESS_SANDBOX_ANCHOR=1` 标记，扫描 `/proc` 按 pgid 定位整树根后 killpg |
| **沙箱锚点（sandbox anchor）** | spawn() 的 no-op 容器（sleep infinity）。三层 bwrap 树，整树同进程组（pgid=沙箱锚点 pid）。不带 `--die-with-parent`（PDEATHSIG 并发误杀，ADR-040）。execute 每次自建 namespace，不依赖沙箱锚点 |

> ⚠️ **术语消歧**：「锚点」在沙箱语境指 **sandbox anchor**（上表），在装配语境指
> **任务锚点（task anchor）**（下表）。两者**完全无关**，引用时请带限定语。

## 上下文预算与会话装配

> 归属：`harness/prompt_assembly.py`；决策依据 **ADR-053**（修订 ADR-034）。

| 术语 | 英文 | 含义 |
|------|------|------|
| **任务锚点** | task anchor | 最新一条 `agent_started` 事件。消息形态 `role:"user"` + `Task: <input>`。**永不截断**（payload 本身很小，且是"任务是什么"的唯一来源，ADR-034 R1） |
| **轮** | round | 一条 `agent_thought`（带 `tool_calls`）+ 其后**全部** `parent_event_id` 指向它的 `tool_result`。消息形态 = `assistant(tool_calls)` + N 条 `role:"tool"`。**截断的最小单位** —— 同进同出，否则产出悬空 `tool` 消息（违反 ADR-052 §5 不变量 2） |
| **不可再精简集** | irreducible set | `SYS + mem(prefix) + 任务锚点 + mem(suffix) + 最后 N 轮`。集合内全部不可丢；仍超限 ⇒ **fail loudly**（框架不自行压缩） |
| **`context_min_keep_rounds`** | — | `AgentSpec` 字段，默认 `1`。不可再精简集中的完整轮数。**`< 1` ⇒ `ValueError`**。仅约束"轮"；任务锚点独立于轮 |
| **两套口径** | — | **丢弃**判定用 `budget_tokens = context_window × budget_ratio`（策略：留多少余量）；**raise** 判定用 `context_window − max_tokens`（物理：装不下）。分离的理由：`budget_ratio` 是调优旋钮，不应成为崩溃开关 |
| **`mem(prefix)` / `mem(suffix)`** | memory_summary | `memory_summary` 事件，由 **app 层** 生成并 `inject_summary` 注入（框架不生成摘要，ADR-038）。最多两个：一个概括被丢弃的旧任务历史（prefix 区），一个概括被截断的当前任务中段（suffix 区）。**计入预算**（fixed-cost） |
| **按终态度量** | — | 预算按**实际发出的 messages** 计算（含 `truncate_output` marker、`assemblable` 剥离后的形态），而非 `str(ev.payload)` 中间态。旧口径实测偏差 **2.4x ~ 24x** |

## 安全

| 术语 | 含义 |
|------|------|
| **Gateway rules** | 自定义授权规则。`gateway.register_rule(fn, name)` 注册回调。用于 spec.tools 过滤、内容检查、配额控制 |
| **Gateway hooks** | 授权生命周期钩子。PreHook（授权前拦截）、PostHook（授权后清理）。内置 SandboxMutexHook 防沙箱竞态 |
| **GateContext** | 授权上下文。包含 gate_type, session_id, spec, tool_name, tool_args, phase_name, node_id |
| **AuthorizeResult** | 授权结果。decision: approved/modified/denied。modify 时携带 amend_args |
| **subagent_spawn** | 权限：是否允许 Agent 动态创建子 Agent。Gateway 直接读 `spec.permissions` |
| **file_access** | 权限：Agent 对沙箱路径的访问控制。read_only / read_write |

## 辅助

| 术语 | 含义 |
|------|------|
| **DI** | 依赖注入。Coordinator 不自己创建依赖，由构造函数注入 Scheduler/Executor/GateManager |
| **推模式合并** | 并行节点完成后，把关键事件主动写入父 session。下游节点读取父 session 即可看到上游结果 |
| **isolated / inline** | 节点执行模式。isolated=子 session 隔离（并行节点），inline=父 session（串行节点） |
| **SDK Tools 客户端** | `client.tools` 子客户端。封装 `ToolRegistry.register/list/definitions`。APP 层不再需要直接 import harness 内部模块 |
| **_retry_error** | 重试错误上下文。`retry_from` 注入到 `node.config`，经 `agent_started` payload → `_build_messages` 渲染为 `[Previous attempt failed: ...]` |
