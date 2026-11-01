# Agent Harness — 已解决问题与历史里程碑

> 本文档记录项目自启动以来已完成的全部模块、重构和功能。
> 只追加，不修改。用于确认功能是否已实现、新成员 onboarding、历史追溯。
> 阅读前建议先查看根索引 [`TODO.md`](./TODO.md) 了解项目全局状态。

---

## Phase 1 — MVP 基础模块（9/9 全部完成）

| 模块 | 状态 | 说明 |
|------|:----:|------|
| ① cap-token | ⚠️ 已移除 | JWT 签发/离线校验。2026-06-10 被 Gateway 取代（见下方 Gateway 重构）|
| ② session-store | ✅ | 原子追加写 JSONL + 并发安全 per-session lock，16 测试 |
| ③ agent-spec | ✅ | YAML 注册表 + 三层防线校验（解析→必填→业务），18 测试 |
| ④ sandbox | ✅ | Bubblewrap + Docker 双后端，spawn/kill/is_alive/get_logs/execute，14 测试 |
| ⑤ session | ✅ | 状态机 wake/rewind/fork/checkpoint，逻辑-物理视图分离，20 测试 |
| ⑥ agent-runtime | ✅ | LLM 循环 + 工具执行 + 权限校验 + JSON 解析容错，6 测试 |
| ⑦ coordinator | ✅ | Kahn 拓扑排序 DAG 编译 + phase 执行 + human gate 状态机，16 测试 |
| ⑧ orchestrator | ✅ | Job 生命周期管理 + 后台线程 + 协作式取消，7 测试 |
| ⑨ cli | ✅ | argparse 命令体系（后扩展至 13 命令） |

---

## Phase 2 — 架构解耦与多后端（全部完成）

| 项目 | 说明 |
|------|------|
| Agent-Sandbox 解耦 | Agent 独立实体，Sandbox 可插拔工具；函数签名通过 Protocol 定义 |
| 多沙箱后端注册表 | `get_sandbox("bubblewrap"\|"docker")` |
| SandboxManager | 命名沙箱实例生命周期 + 懒创建 + 多 Agent 共享 |
| sandbox.yaml | 沙箱实例配置（firewall + shared_folders），3 个预定义实例 |
| 路径级文件权限 | `_sandbox_execute` 逐路径 `read_only`/`read_write` 拦截 |
| AgentSpec 字段迁移 | `allow_subagent_spawn` → `permissions.subagent_spawn`；新增 `sandbox`/`permissions` |
| InstancePool | Agent 实例 acquire/release 复用 |
| Coordinator 解耦 | `_execute_node` 走 InstancePool 而非 sandbox.spawn |
| **Coordinator 拆分** | 1→4：Scheduler、NodeExecutor、GateManager + EventBus、InstancePool 并发 |
| **并行执行引擎** | ThreadPoolExecutor 按层级并行（group_by_level → 同层并发，跨层串行） |
| **节点级 Session 隔离** | isolated/inline 双模式：并行节点独立子 session，推模式合并 node_result |
| **SessionManager 多 Session** | `_session_id` 单值 → `_sessions` dict，支持多 session 并发 |
| **模块分组** | `harness/orchestration/` 集群，按"单一消费者 + 派生存在"原则 |
| instance_pool 搬家 | `harness/orchestrator/instance_pool.py` → `harness/orchestration/pool.py` |
| NullSandbox | bwrap spawn() 路径退耦支撑 |
| DAGNode.human_gate | 节点级 Human Gate + approve/reject/modify + amend_input 传递 |
| human_gate_amend | modify 决定时写入 `human_gate_amend` 事件 + CLI `--amend-input` |
| 文件权限加固 | 删除 `_is_write_command` 13 regex 黑名单，`read_only` 直接拒绝 |
| 可读性重构 | 净减 ~190 行（消除重复、提取样板） |

---

## P0 — 阻塞项（已闭合）

### P0-1. 工具生态（7 个工具全部实现）

| 工具 | 状态 | 测试 |
|------|:----:|:----:|
| `read_file(path)` | ✅ | ✅ |
| `write_file(path, content)` | ✅ | ✅ |
| `bash(command)` | ✅ | ✅ |
| `spawn_subagent(agent_name, task)` | ✅ | ✅ |
| `list_agents()` | ✅ | ✅ |
| `validate(expected, actual)` | ✅ | ✅ |
| `diff(path_a, path_b)` | ✅ | ✅ |

### P0-2a. API Key / Secret 硬编码消除 ✅

- 删除 `_build_orchestrator_legacy()` 及其中 `_DEEPSEEK_API_KEY` 和 `hmac_secret` 硬编码
- `HarnessSettings.hmac_secret` 默认空值，`HarnessClient` 空值拒绝启动
- Pydantic Settings 读取 `config/local.yaml`，环境变量覆盖

### P0-2b. 审计日志（16 个审计点全部完成）✅

实现：`harness/audit_log.py` 全局 + per-session 双写，threading.Lock 保护。

| 事件 | 触发位置 | 日志位置 |
|------|---------|---------|
| `job_created` / `job_started` / `job_completed` / `job_failed` / `job_cancelled` | `orchestrator/impl.py` | 全局 |
| `agent_created` / `agent_reused` / `agent_destroyed` | `orchestration/pool.py` | 全局 |
| `sandbox_created` / `sandbox_destroyed` | `sandbox/manager.py` | 全局 |
| `token_issued` | `orchestration/executor/impl.py` | per-session |
| `file_access_deny` | `agent_runtime/impl.py` | per-session |
| `tool_permission_deny` | `agent_runtime/impl.py` | per-session |
| `gate_approved` / `gate_rejected` / `gate_modified` | `coordinator/impl.py` | per-session |

新增文件：`harness/audit_log.py`（~60 行），`tests/test_audit_log.py`（~230 行，12 测试）

### P0-2c. 权限拦截可绕过修复 ✅

`_is_write_command` 方法（13 regex 黑名单）已删除。`read_only` 路径现在直接拒绝所有 `sandbox_execute` 调用。

### P0-3. 配置系统接入 ✅

SDK `HarnessSettings` + `resolve_llm_profile()` + 三层查找已完整实现。CLI 全部命令通过 SDK 访问配置。

### P0-5. Human Gate modify 完整实现 ✅

节点级 Human Gate：approve / reject / modify + `amend_input` 传递 + `human_gate_amend` 事件 + CLI `--amend-input`。4 个测试全过。

### P0-7. CLI 测试（18 个测试全部覆盖）✅

| 文件 | 测试数 | 覆盖命令 |
|------|:------:|----------|
| `tests/test_cli_new.py` | 6 | compile / ps / rm |
| `tests/test_cli_lifecycle.py` | 6 | start / stop / exec（含 --var/--append） |
| `tests/test_e2e.py` | 6 | run / status / gate / session / cancel |

---

## 其他已完成功能

| 功能 | 说明 |
|------|------|
| 原生 tool calling 重构 | `_call_llm` → OpenAI 兼容 function calling，删除 `_parse_tool_call` |
| 动态 spawn subagent | `spawn_subagent` 工具 + `Coordinator.spawn_dynamic_node` |
| 系统提示词模板注入 | `vars` 模板插值 + `prompt_append` 追加，KV Cache 友好 |
| CLI `--var` / `--append` | `run`、`start`、`exec` 全部支持 |
| tool_call_id 链路修复 | `_emit_event` 返回 event_id + `_build_messages` 复用 |
| Mock LLM 端点 | `httpx.BaseTransport` 拦截 + 上下文组装验证 |
| E2E 时序修复 | `time.sleep` → deadline 轮询 |
| SDK 架构 | `HarnessClient` 唯一入口，Agent/Sessions/Jobs/Gates 子客户端 |
| 配置系统 | Pydantic Settings + YAML/TOML 双格式 + env 变量插值 + 三层查找 |
| CLI 重构 | 13 命令全部实现（compile/start/stop/ps/rm/logs/exec/run/status/cancel/gate/session/audit） |
| AgentSpec 新增字段 | `llm_profile`、`llm_fallback` |
| per-instance LLM 配置 | `pool.acquire` 自动解析 `spec.llm_profile` |
| 硬编码密钥消除 | `hmac_secret` / `api_key` 全部从配置读取，空值拒绝启动 |

---

## CLI 命令完成表（13 命令全部覆盖）

| 命令 | 对标 Docker | 状态 |
|------|:-----------:|:----:|
| `harness compile <spec.yaml>` | `docker create` | ✅ |
| `harness start <instance_id>` | `docker start` | ✅ |
| `harness run <job.yaml>` | — | ✅ |
| `harness exec <sid> "..."` | `docker exec` | ✅ |
| `harness ps` | `docker ps` | ✅ |
| `harness stop <sid>` | `docker stop` | ✅ |
| `harness logs <sid>` | `docker logs` | ✅ |
| `harness audit [--tail] [--follow]` | — | ✅ |
| `harness rm <sid>` | `docker rm` | ✅ |
| `harness status <job_id>` | — | ✅ |
| `harness gate approve\|reject\|modify <id>` | — | ✅ |
| `harness session list\|show\|rewind` | — | ✅ |
| `harness cancel <job_id>` | — | ✅ |

---

## ADR 归档（已创建）

| ADR | 主题 | 类型 |
|-----|------|------|
| 001 | Kahn 拓扑排序替代 LangGraph | orchestration |
| 002 | Bubblewrap 默认沙箱 | sandbox |
| 003 | 同步函数调用替代 asyncio | communication |
| 004 | Agent-Sandbox 解耦 | architecture |
| 005 | JSONL 替代 SQLite | persistence |
| 006 | 显式编程优于 LLM 隐式推理 | methodology |
| 007 | Phase 串行是设计而非缺失 | orchestration |
| 008 | 节点级 Session 隔离 | session |
| 009 | 模块分组按消费集群原则 | engineering |
| 010 | 原生 tool calling 重构 | orchestration |
| 011 | 提示词模板变量 + 追加注入 | architecture |
| 012 | Mock LLM 端点 | architecture |
| 013 | 工具按 Spec 过滤 | architecture |
| 014 | AgentSpec 与 LLM 凭证分离 | architecture |
| 015 | SDK 作为 CLI/TUI 唯一入口 | architecture |
| 016 | 审计日志全局 + per-session 双写 | security |
| 017 | SDK 编排模式库 | architecture |
| 018 | Agent 间通信共享文件系统模型 | architecture |

---

## Refactor 001 — Coordinator 第一步（2026-05-16）

### 已完成

- [x] `Coordinator.execute_phase()` 从 ~180 行压至 ~60 行
- [x] 提取 3 个私有方法：`_execute_serial_level`、`_execute_parallel_level`、`_check_node_gate`
- [x] 并行 level gate 检查所有节点（非仅最后一个）
- [x] 修复 `_GATE_ALLOWED` 允许多个 `human_gate_pending` 事件排队（解决多 gate 死锁）
- [x] 新增回归测试 `test_parallel_level_all_gates_checked`
- [x] 新增行为契约测试 `test_parallel_gate_reject_discards_ungated`、`test_node_and_phase_gate_both_present`
- [x] 新增 `tests/README.md` 和 `docs/behavior-contract.md`
- [x] **194 测试全部通过，0 失败**

### 变更文件

| 文件 | 变更 |
|------|------|
| `harness/coordinator/impl.py` | 提取 3 个私有方法，修复并行 gate bug |
| `harness/session/impl.py` | `_GATE_ALLOWED` 加入 `human_gate_pending` |
| `tests/test_coordinator.py` | 新增 3 个 gate 行为测试 |
| `tests/README.md` | 新增（测试文档） |
| `docs/behavior-contract.md` | 新增（9 条行为契约 + 6 张真值表） |

---

## Refactor 002 — AgentRuntime._agent_loop 提取私有方法 + 超时机制（2026-05-16）

### 已完成

- [x] 提取 `_execute_tool_calls`、`_execute_single_tool`、`_check_done`、`_run_agent_loop` 四个私有方法
- [x] `_agent_loop` 从 60 行/5 层缩进压至 40 行/3 层缩进
- [x] 统一错误处理：`_agent_loop` 内部不再 raise，全部 return status dict
- [x] 删除隐式 DONE：非 "DONE" 内容继续循环而非错误终止，`max_iterations` 兜底
- [x] 超时机制：`AgentSpec.timeout_seconds` + `DAGNode.config.timeout_seconds` 覆盖，每轮 LLM 前检查，超时 emit `agent_timeout` 审计事件
- [x] `execute_task` / `run_loop` 消除重复代码，委托 `_run_agent_loop`
- [x] 新增 `AgentSpec.timeout_seconds` 字段（默认 300）
- [x] 新增 3 个测试：`test_no_implicit_done`、`test_agent_timeout`、`test_permission_denied_return`
- [x] **197 测试全部通过，0 失败**

### 变更文件

| 文件 | 变更 |
|------|------|
| `harness/agent_runtime/impl.py` | 提取 4 个私有方法 + 重构 `_agent_loop` 添加超时 + 统一 return |
| `harness/interfaces.py` | `AgentSpec` 新增 `timeout_seconds: int = 300` |
| `tests/test_agent_runtime.py` | 新增 3 个测试 + 更新 `test_tool_permission_denied` |

### 与原子级 Human Gate 的衔接

重构后的 `_execute_single_tool` 是原子级 gate 的**自然插入点**：权限校验通过后、工具执行前。Gate 回调通过参数注入，无需修改 _agent_loop 主循环。

---

## Refactor 003 — Orchestrator._run_job 提取 _poll_phase_gate + 状态转换注释（2026-05-16）

### 已完成

- [x] 提取 `_poll_phase_gate`（~50 行）从 `_run_job` 内部 gate polling 块
- [x] `_run_job` 从 ~140 行压至 ~110 行
- [x] 为 7 个 `JobStatusInfo` 构造点添加编号语义注释（① PENDING→RUNNING、② advancing phase、③ phase FAILED、④ WAITING_HUMAN、⑤ gate reject FAILED、⑥ COMPLETED、⑦ exception FAILED）
- [x] 全部测试通过，0 回归

### 变更文件

| 文件 | 变更 |
|------|------|
| `harness/orchestrator/impl.py` | 提取 `_poll_phase_gate` + 7 处状态转换注释 |

---

## 选项 B — P0/P1 速清（2026-05-17）

### 已完成

- [x] **P0-6**: `SessionStore.create_session()` 创建 `artifacts/` 子目录
- [x] **P1-13**: `spawn_subagent` 成功后 emit `subagent_spawned` 事件（parent_id/child_id/task）
- [x] **P1-12**: 每次 LLM 调用后 emit `agent_thought` 事件（完整 content + tool_calls）
- [x] **P1-9**: `max_llm_calls` 熔断（session config，超限 return failed）
- [x] **P1-14**: Phase `depends_on` 拓扑排序（Kahn 算法，cycle/unknown dep 抛 ValueError）
- [x] **P1-10**: `read_events` 从 `from_idx` 迁移到 `after_event_id`（事件时间线语义）
- [x] **204 测试全部通过，0 失败**

### 变更文件

| 文件 | 变更 |
|------|------|
| `harness/session_store/impl.py` | `create_session` +1 行 artifacts；`read_events` 签名 `after_event_id` |
| `harness/agent_runtime/impl.py` | `subagent_spawned` + `agent_thought` + `max_llm_calls` 熔断 |
| `harness/orchestrator/impl.py` | `_sort_phases` Kahn 拓扑排序 + `start_job` 同步校验 |
| `harness/interfaces.py` | `read_events` 接口签名更新 |
| `harness/sdk/client.py` | `events.stream()` 从 `from_idx` 迁移到 `after_event_id` |
| `tests/test_agent_runtime.py` | +4 测试（subagent_spawned + agent_thought + max_llm_calls ×2） |
| `tests/test_orchestrator.py` | +3 测试（reorder + cycle + unknown dep） |
| `tests/test_session_store.py` | `test_read_events_from_idx` → `test_read_events_after_event_id` |

---

## Pool/Executor 单元测试 + Coordinator 集成测试（2026-05-17）

### 已完成

- [x] **Pool 单元测试** (12): acquire 复用/新建/destroy/查询/api_key 覆盖/spawner 注入
- [x] **Executor 单元测试** (6): run 获取 agent/传递 config/default input/isolated 子 session/spawner 转发/失败状态
- [x] **Coordinator + bwrap 集成测试** (1): DAG 双节点文件共享——Coordinator → AgentPool → SandboxManager → bwrap → shared_folders 全链路
- [x] **cap_token flake 修复**: `test_tampered_token_rejected` 末尾字符改为 -3 位置（避免 43-char base64 signature 的 2-bit 填充碰撞）
- [x] **测试策略 SOP**: `docs/sop/testing-strategy.md`（三层金字塔 + 8 条集成测试 checklist + 6 个反模式）
- [x] **229 测试全部通过，0 失败**

### 变更文件

| 文件 | 变更 |
|------|------|
| `tests/test_pool.py` | 新增 12 个单元测试 |
| `tests/test_executor.py` | 新增 6 个单元测试 |
| `tests/test_integration.py` | +1 Coordinator 级集成测试 |
| `tests/test_cap_token.py` | flake 修复（tampered token 位置 -3） |
| `harness/interfaces.py` | `SandboxConfig.command` 字段 |
| `harness/sandbox/impl.py` | `spawn()` 支持 `config.command` 覆盖入口脚本 |
| `docs/sop/testing-strategy.md` | 新增测试策略 SOP |

---

## SDK 记忆压缩 + 完成信号增强（2026-05-17）

### 已完成

- [x] **SDK `client.memory.compress()`**: 方案 B — strategy 映射（key_facts/technical_decisions/user_preferences）+ 内置 memory-compressor agent spec（按需写入 `./registry/`）
- [x] **AgentSpec.completion 字段**: `done_signal`（默认，等 DONE 文本） / `single_turn`（一轮返回内容，用于 memory-compressor 等单轮 agent）
- [x] **ADR-019**: DONE 协议作为 Agent 完成信号（基于「幻觉成功」14%+ 失败率的研究）
- [x] **SandboxManager 懒加载测试**: cache hit + destroy respawn 行为覆盖
- [x] **SandboxConfig.command**: spawn 时支持自定义入口命令（集成测试用长期存活 shell）
- [x] **真实 LLM 验证**: 三种策略压缩质量通过（机器狗 RL 训练对话场景）
- [x] **238 测试全部通过，0 失败**

### 变更文件

| 文件 | 变更 |
|------|------|
| `harness/sdk/client.py` | `_MemoryClient` 类 + `HarnessClient.memory` 入口 + `_STRATEGY_PROMPTS` 映射 |
| `harness/interfaces.py` | `AgentSpec.completion` + `SandboxConfig.command` |
| `harness/agent_runtime/impl.py` | `_agent_loop` 分支 `single_turn` / `done_signal` |
| `harness/sandbox/impl.py` | `spawn()` 支持 `config.command` 覆盖入口脚本 |
| `docs/adr/methodology/019-done-protocol-agent-completion.md` | 新增 ADR |
| `tests/test_sdk_client.py` | +5 测试（compress + spec + strategy + chain + structure） |
| `tests/test_agent_runtime.py` | +2 测试（single_turn + done_signal） |
| `tests/test_integration.py` | +2 测试（SandboxManager lazy loading） |

## 当前成就（截至 2026-05-17）

```
238 passed / 0 failed (32 warnings: HMAC key length)

单元测试: 199 个（mock 依赖）
集成测试: 6 个（真实 bwrap，5 AgentRuntime + 1 Coordinator）
E2E: 6 个（真实 LLM + 全栈）

14 模块全部实现（含 audit_log）+ Phase 2 解耦 + 并行执行 + 节点级 Session 隔离
+ SDK 架构 + CLI 重构 + 配置系统 + 硬编码消除 + 审计日志完整覆盖
+ Coordinator 第一步重构 + 行为契约文档

可跑通的生产场景:
  双 Agent 校验流水线 — Worker 发 shell 命令 → Validator 检查
  并行 DAG 执行 — A → B∥C → D（ThreadPoolExecutor 层级调度 + 子 session 隔离）
  DAG 依赖编排 → bwrap 命名空间隔离 → 路径级权限差异化
  → 事件全量持久化 → Human Gate 审批（节点级 approve/reject/modify）
  → 会话 rewind/fork

架构特征:
  Coordinator 拆分为 Scheduler / NodeExecutor / GateManager + EventBus / InstancePool
  Agent/Sandbox 解耦，Protocol 接口收敛
  harness/orchestration/ 集群（按"单一消费者 + 派生存在"分组）
  节点级 Session 隔离（isolated/inline 双模式 + 推模式合并）
  并行执行引擎（Kahn 拓扑排序 → group_by_level → ThreadPoolExecutor）

代码量: ~5,350 行实现 + ~4,800 行测试 = ~10,150 行总计
```

---

## ToolRegistry + 内置工具重构（2026-05-19）

### 已完成

- [x] **ToolRegistry 核心**: 注册/decorator（`@registry.register`）/定义生成（OpenAI 格式）/DI 依赖注入/冲突检测/双重注入幂等
- [x] **7 个内置工具模块化**: 
  - `harness/tools/file_io.py`: read_file, write_file, diff（共享 `_check_access` 权限门控）
  - `harness/tools/sandbox_exec.py`: bash, sandbox_execute
  - `harness/tools/agent_mgmt.py`: spawn_subagent（spawner 注入）, list_agents
- [x] **Test-only 工具迁移**: echo/sum/validate 移入 `tests/conftest.py` → `register_test_tools()`
- [x] **DI 模型**: 显式 deps 声明（`deps=["execute", "permissions"]`），`ToolRegistry.inject()` 绑定
  - 注入点：`AgentRuntime._inject_tool_deps()` 在 `_agent_loop` 开局执行
  - spawn_subagent 走调用侧传入 spawner，不走 DI
- [x] **AgentRuntime 瘦身**: 723 → 464 行（-259 行，-36%）
  - 删除 `_TOOL_DEFINITIONS` 模块级列表（10 工具 × 150 行）
  - 删除 10 个旧工具方法 + `_extract_sandbox_paths` + `_audit_file_access_deny`
- [x] **审计日志兼容**: `_check_access` try/except 兼容 list + AuditLog 两种 append 签名
- [x] **266 测试全部通过**（14 ToolRegistry + 13 内置工具 + 全量回归）

### 新增文件

| 文件 | 行数 | 职责 |
|------|:--:|------|
| `harness/tool_registry.py` | 185 | ToolRegistry 类 |
| `harness/tools/__init__.py` | 27 | `load_builtins()` |
| `harness/tools/file_io.py` | 125 | read_file + write_file + diff |
| `harness/tools/sandbox_exec.py` | 40 | bash + sandbox_execute |
| `harness/tools/agent_mgmt.py` | 41 | spawn_subagent + list_agents |
| `tests/test_tool_registry.py` | 180 | 14 单元测试 |
| `tests/test_builtin_tools.py` | 183 | 13 单元测试 |
| **合计** | **781** | |

### 变更文件

| 文件 | 变更 |
|------|------|
| `harness/agent_runtime/impl.py` | 723 → 464 行，接入 ToolRegistry + `_inject_tool_deps` |
| `tests/conftest.py` | +35 行，`register_test_tools()` 助手 |
| `tests/test_agent_runtime.py` | 4 测试加 `register_test_tools()` 调用 |
| `tests/test_tools.py` | `_tools[x]` → `_tool_registry.get_handler(x)` + 注入适配 |
| `tests/test_integration.py` | `_inject_tool_deps()` 插入 `_build_runtime` + 手动 runtime 创建 |
| `tests/test_coordinator.py` | `_spawn_subagent()` → `get_handler("spawn_subagent")(spawner=...)` |
| `tests/test_mock_llm.py` | 移除 `_TOOL_DEFINITIONS` 导入 + 加 `register_test_tools()` |
| `tests/test_audit_log.py` | `_read_file()` → `get_handler("read_file")()` + list 替代 AuditLog |

### 技术决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 依赖注入方式 | DI 显式声明（deps 列表） | 保证可测试性——纯函数工具零依赖 |
| 文件组织 | 一个文件一个工具（file I/O 三合一） | 清晰 + 共享权限逻辑 |
| spawn_subagent | 调用侧传入（非 DI） | spawner 可能为 None，处理逻辑在调用侧更自然 |
| 向后兼容 | 无薄包装，直接改测试 | 清洁架构，不背技术债 |
| 审计日志兼容 | try/except 双签名 | 工具层不依赖 AuditLog 具体签名 |

代码量: ~6,600 行实现 + ~5,800 行测试 = ~12,400 行总计
```

---

## 架构重构：Prompt 组装 + Skills 沙箱化 + YAML 提权（2026-05-19）

### 已完成

- [x] **Skills 沙箱注入模型**（ADR-021）：文件系统化 + AgentSkills.io 标准对齐 + 渐进式披露
- [x] **Prompt 组装提权到 SDK**（ADR-022）：`_PromptsClient`（assemble/build_messages/describe_tools）
- [x] **YAML 提取到 CLI**（ADR-023）：SandboxManager(configs)、agents.register(spec)、jobs.submit_spec()
- [x] **Skills 扁平目录**：`skills/{name}/SKILL.md`，符合 AgentSkills.io 标准
- [x] **`_SkillsClient.scan()`**：沙箱内扫描 Skill
- [x] **`_SandboxClient`**：sandbox 生命周期（destroy/destroy_all/list）
- [x] **`declare_output_path` 工具**：Agent 声明产物路径，Pipeline 通过事件提取
- [x] **`install_skill` 工具**：Agent 验证沙箱内 SKILL.md 有效性
- [x] **CLI skills install/list**：宿主机 + 沙箱 Skill 管理
- [x] **`events.filter()` + `events.find()`**：事件过滤 API（P2-4）
- [x] **`SandboxConfig.outputs`**：命名产物目录
- [x] **AgentSpecService.register()**：程序化注册（无 YAML）
- [x] **`_MemoryClient` YAML 消除**：内存注册替代 `ensure_spec()`
- [x] **`pydantic populate_by_name` 修复**：HarnessSettings 构造参数正常传递
- [x] **LLM profile 优先级修正**：程序注入 > YAML profile > 环境变量
- [x] **`jobs.submit()` YAML 移至 CLI**：SDK 只留 `submit_spec()`
- [x] **`local.yaml` 现代化**：TOML → YAML，字段对齐 HarnessSettings
- [x] **E2E CLI 全覆盖**：13/13 命令 + `client.memory.compress()`
- [x] **305 测试全绿**

### 新增文件

| 文件 | 行数 | 职责 |
|------|:--:|------|
| `harness/tools/declare_output.py` | 34 | declare_output_path 工具 |
| `harness/tools/install_skill.py` | 56 | install_skill 工具 |
| `tests/test_outputs_and_skills.py` | 9 测试 | 工具单元测试 |
| `tests/test_sdk_prompts.py` | 11 测试 | prompts API |
| `tests/test_sdk_skills.py` | 5 测试 | skills.scan() |
| `tests/test_event_filter.py` | 7 测试 | events.filter/find |
| `tests/test_cli_jobs.py` | 4 测试 | CLI YAML 解析 |
| `docs/adr/architecture/021-skills-sandbox-injection.md` | ADR | |
| `docs/adr/architecture/022-prompt-assembly-to-sdk.md` | ADR | |
| `docs/adr/architecture/023-yaml-extraction-to-cli.md` | ADR | |

### 删除

| 文件/代码 | 原因 |
|------|------|
| `harness/skills/registry.py` | SkillRegistry → 文件系统 |
| `harness/tools/skill_tools.py` | list_skills/invoke_skill → read_file/bash |
| `_MemoryClient._SPEC` + `ensure_spec()` | YAML → 程序化注册 |
| `_JobsClient.submit()` | YAML → CLI |
| `SandboxManager._load_configs()` | YAML → 构造参数 |
| `_AgentsClient.compile()` | YAML → register(spec) |
| `skills/global/` `skills/agents/` | 扁平化 |
| 11 个旧测试 | 功能迁移至 SDK/CLI 层 |

代码量: ~7,200 行实现 + ~8,100 行测试 = ~15,300 行总计
```

---

## Pipeline + MolClaw 分析 + Skills 选择性挂载闭合（2026-05-19）

### 已完成

- [x] **`harness/prompt_assembly.py`**：assemble_prompt/build_messages/describe_tools 提取为纯函数模块，SDK 和 executor 平权 import
- [x] **AgentSpec.skills 选择性挂载**：`compute_skill_mounts()` 纯函数 + CLI `_client()` 读 sandbox.yaml + 注册 spec 扫描 → 注入 SandboxConfig.shared_folders
- [x] **Pipeline**：`harness/sdk/patterns.py` — PipelineStep + Kahn 排序 + run()（submit_spec → 轮询 → declare_output_path 提取 → vars 注入）
- [x] **Pipeline 集成测试**：双 bwrap 沙箱 + 共享产物文件夹 + skills + tools 全链验证
- [x] **`declare_output_path` 工具**：Agent 声明产物路径，Pipeline 提取并注入下游
- [x] **SDK `_AgentsClient.register()` 补漏**：调用 `spec_svc.register(spec)` 确保程序化注册可被 load() 找到
- [x] **MolClaw 移植分析**：`TODO/molclaw_port_plan.md` — 技能文件零代码移植，4 阶段计划
- [x] **329 测试全绿**

### 新增文件

| 文件 | 行数 | 职责 |
|------|:--:|------|
| `harness/prompt_assembly.py` | 110 | 纯函数：assemble_prompt/build_messages/describe_tools |
| `harness/sdk/patterns.py` | 110 | Pipeline + PipelineStep + Kahn 排序 + run() |
| `tests/test_pipeline.py` | 8 测试 | Kahn 排序 + run() 单元测试 |
| `tests/test_skills_mount.py` | 8 测试 | compute_skill_mounts + prompt 过滤 |
| `tests/test_cli_skills_e2e.py` | 3 测试 | CLI skill roundtrip |
| `TODO/molclaw_port_plan.md` | 分析文档 | MolClaw 底层拆解 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `harness/orchestration/executor/impl.py` | 删除 format_map inline 组装 → 调 prompt_assembly |
| `harness/sdk/client.py` | _PromptsClient → delegate prompt_assembly；register() 补 spec_svc.register() |
| `harness/cli/main.py` | _client() 读 sandbox.yaml + 扫描 spec.skills → compute_skill_mounts() |
| `harness/skills/__init__.py` | +compute_skill_mounts() 纯函数 |
| `tests/test_integration.py` | +4 集成测试（选择性/并集/发现 + Pipeline） |

### 删除

| 文件/代码 | 原因 |
|------|------|
| `executor/impl.py` 中 `_SafeDict` import | 跨层依赖消除 → prompt_assembly |
| `test_integration.py` 全量 skills 挂载 | → 选择性挂载 |

代码量: ~7,420 行实现 + ~8,360 行测试 = ~15,780 行总计
```

---

## Amend + Gate 现代化 + 原子 Gate 闭合（2026-05-19）

### 已完成

- [x] **amend_input 通过 prompt_assembly 注入**：executor 读 `node.config["_amend"]` → `assemble_prompt(amend=...)` + `task_input` 覆盖
- [x] **`assemble_prompt(amend=...)` 新参数**：追加 [修改指令]，覆盖 vars
- [x] **amend 队列 FIFO**：`list[dict]` 替代 `dict[str, dict]`，消除跨 Phase key 不匹配 bug
- [x] **并行节点 amend**：`execute_phase` 统一注入 + 中段检查（node gate 后）
- [x] **GateManager 死代码删除**：`_pending` dict, `_lock`, `is_pending()`, `get_amend_input()`, `set/get_phase_status()` 全部删除
- [x] **GateManager 纯事件日志消费者**：`pending()` / `resolve()` 只 emit 事件，不维护内存状态
- [x] **SDK `list_pending(sid)`** + **`get(event_id, sid)`**：基于事件日志查询 gate 状态
- [x] **SDK `sessions.fork(sid, new_sid)`**：暴露 SessionManager.fork()
- [x] **`GateInfo` dataclass**：frozen domain type，`harness.interfaces` 定义，SDK `__all__` 导出
- [x] **原子 Human Gate**：`_execute_single_tool` Hook，`threading.Event` 阻塞，配置格式 `(True, [tool_list])`
- [x] **human_gate_resolve 统一路由**：agent pool 遍历 → GateManager 回退，SDK `approve/reject/modify` 自动适配
- [x] **`finish_reason="stop"` 完成协议**：替代 DONE，删除 `_check_done` / `completion` 字段
- [x] **Pipeline `waiting_human` 处理**：Phase gate 在 Pipeline step 内触发时正确返回
- [x] **secret 工具测试**：验证真实 LLM 调工具（348 全量通过）
- [x] **ADR 019 修订 + ADR 024/026/027 新建**：文档 27 个 ADR

### 新增文件

| 文件 | 行数 | 职责 |
|------|:--:|------|
| `harness/sdk/patterns.py` Pipeline `waiting_human` | +8 | 等待人工状态处理 |
| `docs/adr/architecture/024-two-tier-session-read-write.md` | 44 | SessionStore/Manager 双写单读 |
| `docs/adr/architecture/026-atomic-human-gate.md` | 49 | 工具调用前拦截 |
| `docs/adr/architecture/027-amend-input-fifo-queue.md` | 49 | 队列消除 key 不匹配 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `agent_runtime/impl.py` | -DONE 协议, +finish_reason, +原子 gate Hook, +run_loop 权限修复 |
| `coordinator/impl.py` | 队列 FIFO, 统一路由, 中段 amend 检查 |
| `executor/impl.py` | amend → prompt_assembly + task_input 覆盖, -DONE prompt |
| `gate_manager/impl.py` | 删除 42 行死代码 |
| `prompt_assembly.py` | `amend` 参数 |
| `sdk/client.py` | +list_pending, +get, +fork, -completion 字段 |
| `sdk/patterns.py` | +waiting_human |
| `interfaces.py` | +GateInfo, -completion 字段 |
| ADR 019 | 修订为 finish_reason |

代码量: ~7,800 行实现 + ~8,600 行测试 = ~16,400 行总计
```

## SessionStore + AgentInstance + Hashline 闭合（2026-05-22）

### 已完成

- [x] **SessionStore/SessionManager 双写诊断**：AgentRuntime 直接写 SessionStore，SessionManager 读内存缓存
- [x] **沙箱 entrypoint 提取**：`_build_runtime_script()` 80 行 f-string → `harness/agent_main.py`
- [x] **AgentInstance 死字段删除**：`spec_path`、`llm_fallback`、`sandbox_config`、`metadata` 删除
- [x] **AgentStatus 精简**：`RUNNING/STOPPED/FAILED/ARCHIVED` 删除，只剩 `COMPILED`
- [x] **`sessions.start()` 创建语义**：删除 `prompt/vars/append` 参数，只创建 session
- [x] **`events.send()` 返回 `event_id`**
- [x] **`events.get(sid, event_id)` 单事件查询**
- [x] **Heartbeat `heartbeat()` SDK 模式**：等 `agent_completed` → 注入确认 prompt → 读回复
- [x] **Agent-Session 解耦集成测试**：删除 agent instance → job 继续运行
- [x] **`Event.parent_event_id`**：`subagent_spawned` 接线，SessionStore 序列化支持
- [x] **CLI `skills` 默认动作**：无子命令时列宿主 skills
- [x] **Hashline 编辑工具**：`hashline_read` + `hashline_edit`，内容哈希锚点，原子验证
- [x] **ADR 028 新建**：sessions.start create-only
- [x] **ADR 029 新建**：沙箱 entrypoint 外部化

### 新增文件

| 文件 | 行数 | 职责 |
|------|:--:|------|
| `harness/agent_main.py` | ~85 | 沙箱内 AgentRuntime 入口脚本（原内嵌模板提取） |
| `harness/tools/hashline.py` | ~110 | 内容哈希锚点文件编辑工具 |
| `docs/adr/architecture/028-sessions-start-create-only.md` | 32 | sessions.start = docker create |
| `docs/adr/architecture/029-sandbox-entrypoint-external.md` | 33 | 消除 L1→L2 依赖 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `sandbox/impl.py` | 删除 `_build_runtime_script()`，引用 `agent_main.py` |
| `sdk/models.py` | AgentInstance 精简至 5 字段 |
| `sdk/client.py` | start() 简化，+send() event_id, +get() |
| `sdk/patterns.py` | +heartbeat() |
| `interfaces.py` | +parent_event_id, +GateInfo |
| `session_store/impl.py` | parent_event_id 序列化 |
| `tools/__init__.py` | +hashline 注册 |

代码量: ~7,800 行实现 + ~9,100 行测试 = ~16,900 行总计
测试: 359 collected, 350 passed

---

## Gateway 重构（2026-06-10）

将散落在 `cap_token`、`coordinator`、`orchestrator`、`agent_runtime`、`gate_manager` 中的授权逻辑收敛为统一的 Gateway 授权引擎。

**新增模块**：
- `harness/gateway/gateway.py` — 统一授权入口 (`authorize(ctx) → AuthorizeResult`)
- `harness/gateway/hooks.py` — PreHook / PostHook 生命周期 + SandboxMutexHook
- `harness/gateway/gate_manager.py` — Gate 生命周期（从 `orchestration/gate_manager/` 迁入）

**删除模块**：
- `harness/cap_token/` — 单进程无需 JWT（权限直接读 AgentSpec）
- `harness/orchestration/gate_manager/` — 迁入 gateway/

**核心变化**：
- `agent_runtime`: `cap_token.verify()` + `_atomic_gate` → `gateway.authorize(ctx)`
- `coordinator`: `_check_node_gate` → `gateway.create_gate() + wait_for_resolution()`
- `orchestrator`: `_poll_phase_gate` → `gateway.create_gate() + wait_for_resolution()`
- SDK: `_GatesClient` → `gateway` public attribute + `_GatewayPublicClient` wrapper
- `interfaces.py`: `CapTokenInterface` → `GatewayInterface`

**规模**: 生产代码净 ~+100/-105 行，测试净 ~+250/-350 行，删除 `test_cap_token.py`（90 行）

**ADR**: ADR-035（新建），ADR-013（删除），ADR-030/033/009/behavior-contract（更新引用）

**测试**: 380+ tests pass（全量回归）


---

## DAG 编排移除 + 事件追溯链重构（2026-06-18）

**决策**：ADR-038。移除 Coordinator/Orchestrator/Scheduler/Executor/Pipeline 等固定编排模块，框架回归"提供原语，APP 决定调度"。

**原因**：对齐 AgentScope 2.0 设计方向（"rather than constraining them with opinionated orchestrations"）。APP 实测表明 Pipeline 仅提供 3 行 API 调用的薄封装。

**删除模块**：
- `harness/coordinator/`（DAG 编译 + 节点调度）
- `harness/orchestrator/`（Job 生命周期）
- `harness/orchestration/executor/`（节点执行器）
- `harness/orchestration/scheduler/`（Kahn 排序）
- `harness/orchestration/gate_manager/`（Human Gate 事件，Phase/Node 级）
- `harness/sdk/patterns.py`（Pipeline/Step）
- 7 个测试文件 + CLI run/status/cancel 命令

**保留核心**：pool、agent_runtime、sandbox、session、gateway、tools

**文档**：删 9 ADR、修 5 ADR、新增 ADR-038、CONTEXT.md 简化

---

## 事件追溯链重构（2026-06-17）

`tool_call_id` → `parent_event_id` 统一：

- `tool_called.event_id = tc.id`（LLM 原始工具调用 ID）
- `tool_result.parent_event_id = tc.id`（链回 tool_called）
- `agent_failed.parent_event_id = tc.id`（所有失败路径统一追溯）
- `prompt_assembly` 读 `ev.parent_event_id`（fallback `ev.event_id`）
- 移除 payload 中冗余的 `tool_call_id` 字段

---

## Session 简化（2026-06-18）

- `rewind()`: 修复多次 rewind 事件复活 bug（迭代截断算法）
- `SessionStore.append_event()`: 去掉 tmp 文件 → 单次原子 write+fsync（ADR-037）
- `sandbox/manager`: TOCTOU 竞态修复（spawn 后等待进程就绪）
- `audit_log`: 5 模块 17 处 `if self._audit_log:` → `is not None`

---

## Gateway 安全验证（2026-06-18）

5 个红队测试（`test_gateway_red_team.py`）验证三个 API 表面不可绕过：
Pool.acquire、AgentRuntime 构造函数、_execute_single_tool。
ADR-035 新增 Defense-in-Depth 段。
---

## python-harness 扩展：上下文/回传/hooks/生命周期（2026-08-19）

> 按 `python-harness-issues.md`（I-01…I-24）推进，本阶段完成 **I-01…I-10**（17 项 issue 闭环 10 项），ADR 先行 + TDD（红→绿→重构），全量测试 393 → 450 passed / 2 skipped（另 1 个网络环境依赖测试剔除）。

### ADR 产出（先决策后实现）

| ADR | 内容 |
|-----|------|
| ADR-024 修订 | fork 事件 ID 全局唯一性（重写 event_id + parent_event_id 重映射，I-02） |
| ADR-034 Rev2 | 上下文预算与消息组装修正：任务锚点不可截断、中间/尾部优先截断、system 常量、tool_result 体积上限、assistant 序列化完整化（I-04/05/06/07） |
| ADR-036 注记 | node_result 残留清理完成（I-01/I-03） |
| ADR-041 | reasoning_content 供应商契约与 thinking 显式配置（bool 字段、仅工具轮回传、不硬编码） |
| ADR-042 | 统一 hooks 模块：四模式（observe/transform/veto/around）、优先级确定性、错误隔离、进程级注册表、SDK 视图、规范 hook 点 |
| ADR-043 | Agent 生命周期 IDLE/wake/dispose（COMPILED → IDLE ⇄ RUNNING → STOPPED；wake 复用 ADR-039） |
| ADR-044 | Stop 双轨制：framework 硬闸门（veto 优先、app 不可覆盖）+ app 软裁决（请求-裁决） |

### Issue 完成清单（I-01…I-10）

| Issue | 内容 | 关键产出 |
|-------|------|----------|
| I-01 | SDK 死代码与运行时 bug | `_GatesClient.list_pending/get` 改走 `_store`（删 `_coordinator` 引用）；`estimate_tokens` 补 provider；5 处残留文档清理（含 agent_mgmt 改 read_subagent 语义） |
| I-02 | 会话与沙箱边界 | `fork()` 重写 event_id + payload 引用重映射（`_remap_event_refs`）；`DockerSandbox.kill` 补 `keep_workspace` 参数 |
| I-03 | 接口层残留清理 | interfaces.py 删 16 个死符号（DAGNode/DAG/JobSpec/Phase*/JobStatus*/CapabilityToken/token 异常/Coordinator/Orchestrator/SubagentHookContext/DAG 异常）；GateManager 删（保留 EventBus）；rules.py/node_result 分支/_SafeDict 删；pyproject testpaths 收敛为 tests/；新增 `tests/test_no_dead_symbols.py` 静态守卫 |
| I-04 | 上下文锚点保护 | `build_messages`：最新 agent_started 锚点不可截断、尾部优先截断、memory_summary 插任务后；前缀稳定测试 |
| I-05 | system 与任务解耦 | `compress` system prompt 字节常量（事件 JSON 移入任务消息）；`_build_messages` 空 system_prompt 回退 spec.system_prompt |
| I-06 | tool_result 体积上限 | `prompt_assembly.truncate_output`（8K 上限 + `[... truncated N chars, M lines]` 标记）；read_file/bash/组装层三层兜底 |
| I-07 | assistant 序列化 + thinking | `agent_thought` 捕获 reasoning_content；build_messages 增 agent_thought/agent_completed → assistant（工具轮回传 reasoning、tool_called 去重）；AgentSpec 增 `reasoning_passback`/`thinking`；`_call_llm` extra_body 透传 |
| I-08 | hooks 核心模块 | 新建 `harness/hooks/`（registry/context/points）；四模式/优先级/错误隔离/contextvars/叶子依赖，16 测试 |
| I-09 | hook 迁移 + 新点 + SDK 视图 | **完整迁移（无兼容层）**：content_filter → `tool.result.transform`、_tool_hook → `tool.execute.around`、gateway register_hook → `authorize.veto/observe` + `install_sandbox_mutex`；新点 llm.call/agent.pre_step/agent.lifecycle/agent.stop.* 接入 runtime；`client.hooks` 视图 + 进程级注册表（两 client 互见） |
| I-10 | Agent 生命周期 IDLE/dispose | AgentRuntime 状态机（compiled/idle/running/stopped + `stop()` + `is_idle_expired`）；pool 复用守卫（stopped/超时实例不重用）+ `get_status`/`stop_instance`；SDK `client.agents.status`；AgentStatus 枚举扩展 |

**测试基线**：450 passed / 2 skipped（另 1 个 `test_host_network_allows_connections` 因外网不可达剔除，环境依赖）。

**待办（未完成，见 `python-harness-handoff.md`）**：I-11 wake、I-12 Stop 双轨、I-13…I-24（混合推理/mission/OTel/SDK 执行面）。


---

## python-harness 扩展：I-11 wake 机制（2026-08-19）

> 按 `python-harness-issues.md` I-11（ADR-043 wake / ADR-039 重放）实现，TDD（红→绿→重构），
> 测试基线 450 → **466 passed / 2 skipped**（另 1 个网络环境依赖测试剔除）。

### 实现内容

| 模块 | 改动 |
|------|------|
| `harness/agent_runtime/impl.py` | **wake 机制**：每 agent 懒创建 daemon worker 线程 + `queue.Queue` FIFO 收件箱；`wake(msg, session_id)`（compiled/idle 可唤醒，RUNNING 排队不重复执行，stopped/disposed no-op）；`dispose()`（join(timeout=2s) + 清空收件箱，幂等）；`_loop_lock` 串行化 worker 轮与直接 `execute_task`/`run_loop`；**`_resume_events`（ADR-039 幂等配对）**——剔除无匹配 tool_result 的悬挂 agent_thought/tool_called，保证 LLM 消息链合法；`_current_task` 记录 Task 五元组（start_event_id/agent_spec/prompt/max_iterations） |
| `harness/orchestration/pool.py` | `get_by_instance_id()`；**线程泄漏修复**：acquire 丢弃、destroy、destroy_all、stop_instance 全部改调 `agent.dispose()`（不再只 pop） |
| `harness/sdk/client.py` | `events.send` → 追加 user_message 后 `_wake_session`（session metadata instance_id → spec → pool.acquire 复用守卫 → wake，best-effort 不破坏 send 契约）；`agents.status` 改池感知（按 spec_name 查池内实例真实状态） |
| `harness/session_store/impl.py` | `get_session_metadata()`（wake 接线用） |
| `harness/session/impl.py` | `checkpoint(task=...)` 可选 task 块（ADR-039 恢复点地基，P0-4 前置） |

### 关键设计

- **续跑 = 新 Task 轮**：wake 轮复用 `_run_agent_loop`，emit 新 `agent_started(input=msg)` → ADR-034 锚点保证最新任务永不截断；前轮 `agent_completed` 以 assistant 消息回放（I-07），LLM 看到完整累积历史。
- **ADR-039 幂等判定落地**：`_resume_events` 以 tool_called↔tool_result 配对（parent_event_id）判定悬挂调用并剔除，干净历史 no-op。
- **池感知状态**：`client.agents.status` 返回真实 running/idle/stopped（池内实例），回落 registry 值。

### 测试

- `tests/test_wake.py`（新增 14 测试）：wake 状态机往返、RUNNING 排队（threading 屏障，恰好 N 轮）、compiled 首轮、stopped/disposed no-op、dispose join + 清箱、`_resume_events` 三种场景（成对保留/悬挂剔除/legacy 悬挂剔除）、pool get_by_instance_id + dispose、**MockTransport 集成**（wake 轮上下文含前轮输出 + 新 Task）、**SDK 链路**（register→start→send→wake→idle，follow-up 复用同一实例；未绑定 session 仅追加不触池）。
- `tests/test_session.py`（新增 2 测试）：checkpoint task 块写入/读回 + 无 task 向后兼容。

**待办（见 `python-harness-handoff.md`）**：I-12 Stop 双轨、I-13…I-24。

---

## python-harness 扩展：I-12 Stop 双轨制（2026-08-19）

> 按 `python-harness-issues.md` I-12（ADR-044 双轨 + §5 reason 谱系）实现，TDD（红→绿→重构），
> 测试基线 466 → **480 passed / 2 skipped**（另 1 个网络环境依赖测试剔除）。

### 实现内容

| 模块 | 改动 |
|------|------|
| `harness/agent_runtime/stop.py`（新增） | `STOP_REASONS`（LEGAL ∪ NEEDS_CONFIRM，ADR-044 §5 谱系）；`validate_stop_reason`（fail-closed 前置校验）；`install_stop_hooks()`——内置 framework veto（**最低优先级 -100**，返回 condition → vetoed） + app 默认裁决表（priority 0：合法→None 批准 / 需确认→否决文本），按**函数身份**幂等（registry.clear() 后可重装） |
| `harness/agent_runtime/impl.py` | **双轨落点 = loop 步窗口**：`_framework_stop_check`（timeout / max_llm_calls / **sandbox_death** is_alive 探活 → `agent.stop.framework` veto 链 + fail-closed 复查 → agent_failed）；`_app_stop_check`（消费待决请求 → `agent.stop.app` veto 链 → 批准 `agent_completed`{reason:stopped, stop_reason} → loop **stopped** / 否决 `agent_stop_denied` → 续跑）；`request_stop(reason)`（非 RUNNING / 非法 reason 立即拒绝，不触发 hook）；`_run_agent_loop` 补 stopped 映射（回 IDLE 可恢复） |
| `harness/sdk/client.py` | `client.agents.stop(instance_id, reason=...)`：注册实例 → 池内 running 实例 → request_stop |

### 关键设计

- **stop 是请求不是结论**：`request_stop` 仅线程安全置待决请求（独立锁），裁决在下一步窗口；RUNNING 期间到达必被下一轮处理，loop 已结束自然 IDLE no-op（§5 一致）。
- **同一终态**：loop 结果 completed / stopped / failed 都回 IDLE（可恢复，SSE 语义）；显式 stop()/dispose 才 STOPPED。批准停复用 agent_completed（wake 续跑可见前轮）。
- **app 无法覆盖 framework**：内置 veto 必然触发 + 运行时 fail-closed 条件复查双重保证。
- **app 可覆盖默认表**：veto（priority>0）强制否决；需确认 reason 强制批准用 around hook（返回无 veto 的 TriggerResult）。

### 测试

- `tests/test_stop_dual.py`（新增 14 测试）：framework 硬闸门（timeout 触发 hook + agent_failed / max_llm_calls / sandbox_death / **app 更高优先级 veto 返回 None 仍停**）；app 裁决（objective_met 批准→stopped→IDLE / budget_policy 默认否决→agent_stop_denied+续跑 / no_reason·None·未知 fail-closed 拒绝且 hook 未触发 / IDLE no-op）；覆盖（veto 否决合法 reason / around 批准需确认）；终态一致（stopped 后池复用同一实例）；SSE 续跑（停→wake→前轮输出在上下文）；SDK 端到端（agents.stop → stopped → idle）。

**待办（见 `python-harness-handoff.md`）**：I-13 起（混合推理执行原语）。

---

## python-harness 扩展：I-23 SDK 公共执行入口（client.agents.run）（2026-08-19）

> 按 `python-harness-issues.md` I-23（ADR-015 SDK 单一入口）实现，TDD（红→绿→重构），
> 测试基线 480 → **491 passed / 2 skipped**（另 1 个网络环境依赖测试剔除）。
> Grill 闭合 11 项设计决策（同步 run / 双形态 / 续用轻校验 / 装配 raise / 忙时 raise /
> tools 过滤 / 测试注入保留 / output 事件流 / stop_reason 仅 stopped / 新任务新 session）。

### 实现内容

| 模块 | 改动 |
|------|------|
| `harness/sdk/models.py` | `RunResult{session_id, instance_id, status, output, error, stop_reason}` |
| `harness/sdk/client.py` | `_AgentsClient.run(spec\|instance_id, task, session_id=None, config, system_prompt, tools)`：注册→acquire（复用守卫）→忙检查（RUNNING raise）→session 新建/续用（轻校验）→execute_task→事件流组装 RunResult；`_terminal_output`/`_terminal_error` helper |
| `harness/agent_runtime/impl.py` | **Q7 现存 bug 修复**：`_spec_tools()` 按 spec.tools 白名单过滤内置 + subagent_spawn 条件追加；`_run_agent_loop` 归一化 tools=None（execute_task / run_loop / wake worker 三入口一致；修复 execute_task 路径真实 LLM 无工具 schema 的缺口，原 `tools or []` 掩盖）；run_loop 过滤块去重 |
| `harness/sdk/client.py` | `_MemoryClient.compress` 迁移到 `agents.run`（自建 session 保留自定义 metadata，用 RunResult.output，删除 `_pool.acquire` 直用） |

### 关键设计（grill 决策落点）

- **同步 run + 异步 wake 互补**：run 是同步启动入口（脚本/集成/compress），send 是异步续跑入口（SSE/长任务），同一执行栈不重复。
- **双形态**：spec 自动注册；instance_id 复用已注册实例；RunResult 带 instance_id 可接 `agents.stop`。
- **Q7 顺带修复 execute_task 工具缺口**：`tools=None` → spec 白名单（含 spawn_subagent 条件），显式列表覆盖、`[]` 真无工具；mock 测试曾掩盖（MockTransport 不校验请求 tools）。

### 测试

- `tests/test_sdk_run.py`（新增 11 测试）：run 双形态/复用+新 session/续用累积/session 不存在 raise/非法 spec raise/忙时 raise/stopped+stop_reason 透出/tools 过滤（spec 白名单、显式空、spawn_subagent 条件、非内置空列表）。
- 迁移：`test_sdk_client.TestMemoryCompress` 改 mock `agents.run`；`TestLLMProfileResolution` 迁至 `test_pool.py`（池行为归属地）。

**待办（见 `python-harness-handoff.md`）**：I-24 装配引导收敛（保底 main/来源装配/硬前置 fail-fast）。

---

## python-harness 扩展：I-24 装配引导收敛 + sessions.stop 迁移（2026-08-19）

> 按 `python-harness-issues.md` I-24（5 条验收含必须项）实现，TDD（红→绿→重构），
> 测试基线 491 → **499 passed / 2 skipped**（另 1 个网络环境依赖测试剔除）。

### 实现内容

| 模块 | 改动 |
|------|------|
| `harness/sdk/client.py` | **`_load_sandbox_configs()`**：来源装配收敛（显式参数 > `sandbox.yaml` > 内置保底 main；技能挂载扫描**双向**：registry 目录 + 内存注册）；`agents.register` 补 `llm_profile` 字段非空校验（带指引）；`agents.run` acquire 包 try/except——LLM profile 解析失败重抛带修复指引；**`sessions.stop` 转调**（必须项）：始终追加 `session_stopped` 标记 + 绑定实例且池内 running → `request_stop("user_requested")` 真双轨裁决；未绑定/未运行 → 仅标记（向后兼容） |
| `harness/cli/main.py` | `_client()` 53 行样板（yaml 读取/skills 扫描/保底 main）**全部移入 client**，收敛为 `return HarnessClient()`（纯命令路由） |
| `harness/orchestration/pool.py` | **移除 resolve_llm 错误吞掉**（`except RuntimeError: pass`）——缺失 profile 不再以 OpenAIError "Missing credentials" 在运行期随机炸，acquire 处 fail-fast |
| 潜伏 bug | **技能挂载扫描从未生效**：`AgentSpecService` 方法是 `list_all()`，旧 CLI 代码用 `svc.list()` 被 except 吞掉——I-24 修复，扫描现真正工作 |

### 关键设计

- **保底优先级**：显式 `sandbox_configs` > `sandbox.yaml`（同名覆盖）> 内置 main（bubblewrap/network=host）——`pool.acquire` 永不因缺沙箱炸。
- **三前置落点**：hmac_secret 构造期（已有+锁定测试）；sandbox 构造期（保底使永不缺）；**LLM profile run() 前置**（acquire 解析失败 → 指引，非注册期——注册是纯管理面，且 `resolve_llm_profile("default")` 无配置必抛，注册期校验会误伤 compile-only 流程）。
- **sessions.stop 语义**：审计标记保留 + 条件转调（I-12 双轨 reason=user_requested）；CLI `harness stop` 同步生效。

### 测试

- `test_sdk_client.py` 增补 8 测试：保底 main（chdir 隔离）/ yaml 合并+显式覆盖 / 技能挂载（registry 持久化 spec）/ hmac_secret 指引 / register llm_profile 空校验 / run LLM 解析失败指引 / **sessions.stop 转调 2 测试**（绑定 running → request_stop / 未绑定 → 仅标记）。
- `test_wake.py` 竞态修复：agent_completed 先于 idle 状态写入，完成轮询后补 idle 状态轮询（加载环境下偶发）。

**待办（见 `python-harness-handoff.md`）**：I-13 起（混合推理执行原语，7 项中剩 6 项）。

---

## python-harness 扩展：I-13 模型配置扩展 + 本地后端 + 故障回退（2026-08-19）

> 按 `python-harness-issues.md` I-13 实现，TDD（红→绿→重构），grill 3 决策闭合，
> 测试基线 499 → **509 passed / 2 skipped**（另 1 个网络环境依赖测试剔除）。

### Grill 决策

- **Q1 backend 语义**：`spec.backend: auto|cloud|local`；auto 取 profile 解析值；显式与 profile 不一致 → pool.acquire fail-fast（带指引）。
- **Q2 故障回退**：白名单触发（APIConnection/APITimeout/RateLimit/5xx InternalServer）；**单级** primary→fallback；fallback 失败抛合并错误（`primary: ...; fallback: ...`）；4xx 不触发（配置错回退无意义）；audit `agent_llm_fallback` + `agent_thought.llm_backend` 标记。
- **Q3 hook 交互**：回退是框架 do_call 层原语；每次 `llm.call` 独立触发，trigger fields 增 `backend`/`attempt`（进 ctx.extra；I-15 归因、I-21 span 关联）。

### 实现内容

| 模块 | 改动 |
|------|------|
| `harness/interfaces.py` | AgentSpec 增 `backend="auto"`、`adapter=""`、`reasoning_effort=""`；`AgentSpecService.validate` 增 backend ∈ {auto,cloud,local} |
| `harness/sdk/config.py` | LLMConfig 显式 `backend="cloud"`、`endpoint`、`adapter`（extra=allow 已自动接收 yaml 新键） |
| `harness/orchestration/pool.py` | acquire 解析**完整** LLMConfig（model/backend/adapter，不再只拷凭据）+ Q1 一致性检查 + `llm_fallback` 解析传 `llm_fallback_config` |
| `harness/agent_runtime/impl.py` | `__init__` 增 `llm_config`/`llm_fallback_config`（懒建 fallback client）；`_call_llm` 拆 `_call_llm_once(backend, attempt)` + 单级回退封装；model = `llm_config.model or spec.model`；extra_body = thinking/adapter/reasoning_effort 合并；`agent_thought` payload 增 `llm_backend` |
| `harness/sdk/client.py` | register 增 backend 字段校验 |

### 测试

- `tests/test_llm_backend.py`（新增 10 测试）：backend 校验/Q1 fail-fast/auto 取 profile/本地后端 model 覆盖/adapter·effort 透传/回退触发+审计+标记/4xx 不触发/双失败合并错误/hook 见 backend·attempt/profile 解析。

**待办（见 `python-harness-handoff.md`）**：I-14 起（双池化，混合推理链）。

---

## python-harness 扩展：I-14 模型服务池（双池化）（2026-08-19）

> 按 `python-harness-issues.md` I-14 实现，TDD（红→绿→重构），grill 13 决策闭合，
> 测试基线 509 → **528 passed / 2 skipped**（另 1 个网络环境依赖测试剔除）。

### Grill 决策（13 项，摘要）

Q1-B 池键=model_name、进程内多 adapter；Q2 C+薄B 不 spawn、/health 探活（ADR-040 §2 探针）；Q3 单端点+多候选预留；Q4-C models.yaml 基线+动态注册；Q5-A acquire 路由一次；Q6-A adapter 预热尽力而为（成败归引擎，DESIGN.md §8.2）；Q7-A acquire 探活+低频轮询+unregister（不做 TTL 假驱逐，DESIGN.md §8.3）；Q8-A client.models 公共面+_acquire 内部（三池独立）；Q9 复用路径端点更新+审计；Q10 短锁+锁外探活；Q11 结构化校验+同名覆盖保留 loaded；Q12 yaml 初始 unknown+动态仅内存；Q13 多候选故障转移+全败清晰报错（无 acquire 降级）。

### 实现内容

| 模块 | 改动 |
|------|------|
| `harness/orchestration/model_pool.py`（新增） | `ModelService`/`ModelServiceStatus`/`ModelServicePool`：register（校验+覆盖保留 loaded+多候选）/unregister/status/list/`_acquire`（锁外探活+故障转移+adapter 预热+全败报错）/`_probe`（/health 回退 /v1/models，可注入）/后台轮询 daemon/dispose |
| `harness/sdk/config.py` | LLMConfig 增 `model_service` 池键 |
| `harness/orchestration/pool.py` | acquire 的 local 分支（model_pool 配置时路由+探活+预热，未配置保留 I-13 直连）；**Q9 轻联动**（复用路径端点更新 `_client`/`base_url` + audit `model_endpoint_updated`，对齐沙箱 re-spawn 先例）；`model_pool` 构造参数 |
| `harness/sdk/client.py` | `_load_model_services`（models.yaml 基线，初始 unknown）+ `client.models` 子客户端（register/unregister/status/list + 内部 `_acquire`） |

### 测试

- `tests/test_model_pool.py`（新增 19 测试）：校验/初始 unknown/覆盖保留 loaded/多候选/adapter 预热成功·失败不阻断·无端点跳过/探活 200·404 回退·异常/concurrent 共享端点/acquire 健康·缺失·unhealthy 指引/故障转移/local 路由·无池条目报错/Q9 换端口复用更新+审计。

**待办（见 `python-harness-handoff.md`）**：I-18 起（Program 执行器）；I-20/21（A2A/OTel）。

---

## python-harness 扩展：I-15 token 计量原语（2026-09-04，grill 收敛缩为捕获原语）

### Grill 决策（A/B/C/D）

| 分叉 | 决策 |
|---|---|
| A 契约 | 框架在 `llm.call` 的 final（`do_call`）捕获 usage → `set_context_fields(tokens_in/out)`；app 层自行编排 set_context_fields/call 与消费 |
| B 审计 | 审计/成本账归 app 层——py harness 不写审计，只保证 context 里有实测 token |
| C 口径 | 服务端 `usage.prompt_tokens/completion_tokens` 原样透传；`usage=None` 时字段保持 None，无估算兜底 |
| D 回退 | 逐 attempt 独立上报；失败 attempt 无 usage → None；框架不跨 trigger 累积 |

### 实现内容

- `harness/agent_runtime/impl.py` `_call_llm_once`→`do_call`：捕获 `response.usage` → `set_context_fields(tokens_in/out)`（usage 缺失时跳过）
- HookContext `tokens_in/out` 字段 I-09 已有，本次填充实测值
- 隐性契约：around hook 的 ctx 构建于 final 前（`ctx.tokens_in` 恒 None）——读实测 token 须在 `next_fn()` 后调 `current_context()`

### 测试

- `tests/test_llm_backend.py::TestTokenMetering`（2 测试）：带 usage 成功 → around hook 读到 (in,out)；后端不报 usage → (None,None)

---

## python-harness 扩展：I-25 run/stop/status/wake 按注册实例分派（2026-09-04，grill 定稿）

### Grill 决策

| 分叉 | 决策 |
|---|---|
| 归属 | py-harness 执行面新 issue（I-23/24 延续） |
| D1 身份合并 | 每个已注册实例（registry inst_id）拥有专属懒建常驻运行时；池键与 `runtime.instance_id` = 注册 inst_id（同模板两实例 uuid 互异） |
| D2 分派键 | run/wake/stop/status 一律按 inst_id 精确定位（消除按名塌缩） |
| D3 busy | 同实例 RUNNING 时 run 保持 raise；follow-up 走 events.send→wake 收件箱（不引入 run 排队） |
| D4 兼容 | `pool.acquire(spec)` legacy 随机 uuid 语义保留（测试/非 SDK 用法） |
| D5 沙箱不动 | 共享容器 + session workspace 隔离（I-26 另行） |

### 实现内容

- `harness/orchestration/pool.py` `acquire` 新增可选 `instance_id`：精确键控 `(name, instance_id)`；开头 `requested_instance_id` 捕获防 sandbox `get_or_create` 局部同名变量遮蔽
- `harness/sdk/client.py`：`agents.run`（:461）/ `_wake_session`（:307）→ instance-keyed acquire；`agents.stop`（:412）/ `status`（:392）→ `get_by_instance_id` 精确定位（不再可能停错/读错同名实例）

### 测试

- `tests/test_instance_dispatch.py`（7 测试）：S1 池键控区分/复用；S2 run 按实例分派 + busy 实例级（A busy 不挡 B）；S3 status/stop 精确命中；S4 wake 精确唤醒。基线 531 → 538。

---

## python-harness 扩展：I-26 沙箱 uuid 身份 + agent→sandbox 关联（2026-09-04，grill 定稿）

### Grill 决策
- D1 接口层统一 uuid：SandboxInterface 按 sandbox_id 寻址；OS 身份（PID/容器 id）不出后端（_by_uuid 私有映射；bwrap+docker 一并）
- D2 SandboxEntity(sandbox_id, config_name, backend)，无公开 backend_instance_id
- D3 双 API：get_or_create(name, create_new=False)（name 级；create_new=True 同名独立新 uuid 且规范指针不动）+ create(name)（总是新建）+ uuid 组 get_entity/destroy(sandbox_id)/list()
- D4 respawn 同 uuid 覆盖式（Q1）；D5 绑定缺失透明重物化 ensure（Q3）；D6 命名收敛（_sandbox_instance_id→_sandbox_id）

### 实现内容
- manager 实体化重构（_entities[uuid]/_default_by_config；respawn/probe/audit 保留语义）；ensure(name, sandbox_id) 重物化 + 跨配置校验
- SandboxInterface + bwrap/docker 后端 uuid 翻译层；pool acquire(sandbox_id)/runtime _sandbox_id/register(AgentInstance.sandbox_id)/run·wake 传递；_SandboxClient 实体化
- 配置变更语义迁移：respawn 在 manager 内同 uuid（池测试 5 处改写）；mock 夹具 38 处 + 真实 manager 测试迁移

### 测试
- tests/test_sandbox_entity.py（16 测试：规范复用/create_new/create/list/get_entity/destroy/respawn 保 uuid/ensure 重物化/跨配置拒绝/pool 绑定/register 持久化/dispose 不销毁/docker 签名）
- 全量回归 553 passed / 2 skipped / 1 deselected（+16 新增，基线 538 → 554 收集）

---

## python-harness 扩展：I-18 Program 执行器（2026-09-05，grill 收敛为 SDK 执行面包装）

### Grill 决策
- Program = agent 类配置子集（无 LLM/无推理）——非新引擎；入口独立 client.program.run
- 输出写 session 事件为主；sandbox 可选（脚本自身写 FS artifact）
- 重试语义：瞬时失败重试；超时/永久失败不重试（mission §2）

### 实现
- harness/sdk/program.py：with_retry / run_script_host（subprocess+timeout）/ run_script_sandbox（I-26 实体 ensure+execute）/ ProgramResult
- sdk/client.py _ProgramClient.run：宿主或 sandbox 路径 + program_started/program_completed session 事件

### 测试
- tests/test_program.py（13）：重试策略 4 / 宿主 3 / sandbox 3 / SDK 集成 3
---

## python-harness 扩展：I-27 策略中间件（2026-09-05，grill 2026-09-04/05 定稿）

### Grill 决策
- gateway 层定向策略中间件：既有全局 hooks 按 ctx.spec.name 定向；纯新增不改现有代码
- 一切默认关；enable/add 必带 agent（或 "*"）；spec.policy 声明=register 自动 enable；app 可 add 自定义
- 多规则=并集+deny-wins；规则条目 {name, veto(ctx,cfg), observe, observe_exec, cfg}

### 实现
- harness/policy.py PolicyEngine 单例 + resilient AUTHORIZE_VETO/OBSERVE + TOOL_EXECUTE hooks 注册
- harness/policy_presets.py 4 preset：bash_whitelist（危险通配）/ fs_paths（系统目录）/ read_before_edit（DSH 版本化：hashread→hashedit old_hash 纯参数门；literal 经 cfg.digest_provider sha256，无则 seen fallback）/ net_policy（IP/CIDR/域名命令级）
- AgentSpec.policy 字段；client.policy（enable/add/disable/list）；agents.register 自动 enable

### 测试
- tests/test_policy.py（16）：engine 默认关/定向/star；scoped hook 端到端；bash/fs/net preset；read_before_edit 版本化；spec.policy；client.policy add/自定义
- 全量 582 passed / 2 skipped / 1 deselected（基线 566 → 582）
- 注：沙箱实体绑定策略面（共享组/隔离/重绑）→ I-28 草稿


---

## python-harness：CLI 观测双模 + 模型改名 v4（2026-09-06）

- CLI logs/session show：agent/human 双模（--user_type；agent=首行 JSON 摘要含 truncated bool→详情窗口；human=md 全量；--out-file；--follow）——契约记 ADR-049
- 全仓 model 改名 deepseek-chat → deepseek-v4-flash（provider 清单无旧名；d656e51）；conftest/e2e key 源改读 examples/.keys/auth（旧 key 失效；b744de7/682c58d）

## python-harness：hook_result 冗余修复（2026-09-06，08741bd）

- 根因：_fire_post_tool_hook 以「payload 字典 truthy」判发——无 hook 也每次镜像 tool_result（不进上下文，仅日志噪声/计数翻倍）
- 修复：identity 判断——仅 around hook 实际改写载荷（新对象）才发 hook_result（还原注释本意）
- 测试：tests/test_post_tool_hook.py 3 例；ADR-042 hook_result 契约注记

## python-harness：I-31 Session 去状态机化（2026-09-07，7c13e4b；ADR-050 + 037/039/043 注记）

- Session = 无状态 append-only 日志；持有=派生（agent running/idle 持有；stopped/disposed→unbound，无物理解绑 Q2-b）；rewind/fork/checkpoint 保留为日志级运算 + RUNNING 守卫
- show 契约去推断 status → holder_instance_id/holder_status；EventBus 薄封装不动；SessionStatus 枚举降展示
- 测试：test_session 重写日志语义（8）+ CLI observe holder（4）；590 全绿

## python-harness：I-32 Agent 绑定历史图（2026-09-07，0e31fd3；ADR-049）

- 二部图 session×sandbox，边=(agent,instance,session,sandbox,first_ts,last_ts) 存 session meta.binding_history（同三元组合并 last_ts）
- SessionStore.update_session_metadata（lock+fsync）；run/wake 记录边
- SDK agents.history_sessions/history_sandboxes/history_bindings（agent 必填；bindings 限定该 agent 该 session）；CLI agents --name A --history_* 双模
- DRT 差分随机测试（sqlite reference，3 种子 ×150 边）抓到 2 类语义分歧后收敛单边不变量

## python-harness：hashline e2e flake 修复（2026-09-07，4274324）

- 根因：模型在两轮中从全工具面选型漂移（第二调用选 read_file 而非 hashline_edit）
- 修复：按轮次收紧 tools 面（仅暴露该轮应选的 hashline 工具）；连续 3 次通过

## python-harness：DS thinking reasoning 回传 + 装配时序修复（2026-09-07，376172b/3c8e7f1/4b492ff/602d8c5；ADR-041 Rev/034 Rev3）

- reasoning：V4 thinking 任意 assistant（含最终轮）带 reasoning_content 即须回传——agent_thought 任意轮嵌 + agent_completed 与同轮 reasoning 合并为单条（skip 前导 content 重复）；provider 直连回放两会话 OK
- anchor 时序：多任务同 session 原将最新 agent_started 前置→因果倒置（旧任务约束被当最新指令）；修复=[旧任务历史(截断)]+[当前任务 anchor 起全保]，与 DSH core「严格前缀扩展」不变量一致
- 测试：TestReasoningPassbackMultiTurn（单条合并/工具轮/关断/parity 时序+R推理归属）
- 全量 590 passed / 2 skipped / 1 deselected

## python-harness examples：split 双 task 场景 + 隔离实验（2026-09-07，3c8e7f1/cab6c98）

- run.py --mode split：task1 翻译 inline（不落盘）→ task2 用 echo/grep/sed 落盘
- 隔离实验：弱措辞 + harness 修复后即成功落盘（bc2a28ff）→ 根因=上下文装配时序，非提示词纪律；保留弱措辞为默认

## ADR 体系（2026-09-07）

- 新增：049（CLI 双模+agent 历史）、050（Session 无状态化）
- 修订：041 Rev（reasoning 任意轮回传/单条合并）、034 Rev3（memory fold 契约 + v4 1M + anchor 定位）、042（hook_result 契约）、037/039/043（I-31 注记）

