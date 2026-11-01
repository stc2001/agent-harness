# ADR-015：SDK 作为唯一切入点

**日期**：2026-05-16（更新 2026-06-18）
**状态**：已采纳（简化）

## 上下文

CLI 和 APP 层通过 SDK (`HarnessClient`) 访问框架功能。SDK 提供 agents、sessions、
gateway、sandboxes、tools、skills、prompts 等子客户端。

2026-06-18 移除 DAG 编排模块（coordinator、orchestrator、Pipeline）后，SDK 中
的 `jobs`、`gates`（node/phase 级）子客户端一并移除。编排决策由 APP 层自行处理
（pool.acquire → agent.execute_task → poll session events）。
3. **硬编码密钥**。`cli/main.py` 中存在 `_DEEPSEEK_API_KEY = "sk-..."` 和 `hmac_secret = "mvp-dev-secret..."`,既无法环境隔离,也无法审计。

## 决策

**所有上层界面(CLI、TUI、脚本)统一通过 `harness.sdk.HarnessClient` 访问功能,禁止直接调用内部模块。**

### SDK 架构

```
CLI / TUI / Scripts
        │
        ▼
  HarnessClient (唯一入口)
        ├── agents    → compile / list / get / delete / archive
        ├── sessions  → start / stop / list / show / rewind
        ├── jobs      → submit / status / cancel
        ├── gates     → approve / reject / modify
        └── events    → list / send / stream
```

### 安全基线:拒绝启动

`HarnessClient.__init__` 在 `hmac_secret` 为空时抛出 `RuntimeError`,拒绝启动:

```python
if not self._settings.hmac_secret:
    raise RuntimeError("hmac_secret is not configured...")
```

这消除了"忘记配密钥就用默认硬编码值启动"的安全隐患。

### 配置来源

所有敏感配置通过 `HarnessSettings`(Pydantic Settings)统一读取:

```
环境变量  >  harness/config/local.yaml  >  pydantic 默认值
```

- `HARNESS_HMAC_SECRET` 覆盖文件中的 `hmac_secret`
- `DEEPSEEK_API_KEY` 覆盖文件中的 `deepseek_api_key`
- 默认值为空字符串,强制显式配置

## 理由

1. **接口稳定性**。内部模块可以自由重构,只要 SDK 接口不变,CLI/TUI 不受影响。
2. **多前端复用**。CLI 和 TUI 共享同一 SDK,行为一致。
3. **安全默认**。空 `hmac_secret` 拒绝启动,避免"配置遗漏 → 使用弱默认值 → 被攻击"的连锁风险。
4. **测试简化**。CLI 测试只需 mock SDK 方法,不需要构造完整的 Orchestrator 依赖树。

## 后果

- CLI 代码从 ~300 行(含业务逻辑)降至 ~150 行(纯命令路由)
- 删除 `_build_orchestrator_legacy()` 函数及其中全部硬编码密钥
- 新增 `HarnessClient` 4 个子客户端:`agents`、`sessions`、`jobs`、`gates`
- 所有 CLI 命令(`run`、`status`、`gate`、`session`、`cancel`)迁移到 SDK 调用
- `HarnessSettings.hmac_secret` 默认值为 `''`(空),不再提供任何默认密钥

### 实现注记（2026-08-19，I-23 执行面落地）

- **`client.agents.run(spec|instance_id, task, session_id=None, ...) → RunResult`** 补齐了 ADR 的执行面缺口：此前 SDK 只有管理面（register/start/send/stop），"让 agent 干活"只能碰私有 `_pool.acquire`。run 是同步执行入口（spec 自动注册、acquire 复用守卫、忙时 raise、session 新建/续用轻校验、装配错误 fail-fast、执行结果进 RunResult），异步续跑沿用 `events.send`/wake。
- **执行装配不外泄**：gateway/sandbox/llm/tools/skills 全部在 run 内部绑定；tools 按 `spec.tools` 白名单装配（`AgentRuntime._spec_tools()`，修复 execute_task 路径真实 LLM 无工具 schema 的缺口）。
- **私有路径收敛**：`_MemoryClient.compress` 与测试执行路径迁移到 `agents.run`（`~/.harness` 实例注册表为 register 的持久化副作用，测试需 HOME 隔离）。
- 来源装配（sandbox.yaml/保底 main/skills 挂载扫描）仍归 **I-24** 收敛进 `HarnessClient.__init__`。

### 实现注记（2026-08-19，I-24 来源装配收敛）

- **来源装配全部收敛进 `HarnessClient.__init__`（`_load_sandbox_configs`）**，CLI `_client()` 由 53 行样板收敛为纯 `HarnessClient()`——符合本 ADR 的 CLI/SDK 职责边界（CLI 禁止解析 YAML）。
- **装配优先级（已定）**：显式 `sandbox_configs` 参数 > `harness/config/sandbox.yaml` > 内置保底 main（bubblewrap/network=host）——同名沙箱显式覆盖，yaml 缺失的沙箱用内置兜底，`pool.acquire` 永不因缺沙箱失败。
- **技能挂载扫描**：registry 目录 + 内存注册双向扫描（顺带修复 `svc.list()` → `list_all()` 的潜伏 bug——扫描此前从未生效）。
- **三前置 fail-fast 落点**：hmac_secret 构造期（含指引）；sandbox 构造期（保底使永不缺）；**LLM profile 在 run() 前置**（register 字段非空校验 + `pool.acquire` 不再吞 resolve 错误——缺失 profile 不再以 OpenAIError "Missing credentials" 运行期随机炸）。注册期不做解析校验：`resolve_llm_profile("default")` 无配置必抛，注册是纯管理面（compile-only），解析校验会误伤。
- **最小可执行 agent = AgentSpec + api_key**：`HarnessClient()`（零配置）→ `agents.register(spec)` → `agents.run(...)` 三步（guide I-24 章节示例）。

### 实现注记（2026-09-04，I-25 实例分派落地——修正 I-23 注记的 run 语义）

- **run 语义更新**：§I-23 注记所述「acquire 复用守卫、忙时 raise」现为**实例级**——`run(spec)` 每次自动注册**新实例**（同模板运行中的另一实例不阻挡）；`run(instance_id)` 精确定位到该注册实例的专属运行时。同一实例 RUNNING 时再次 `run(inst_id)` 才 raise busy；同实例 follow-up 走 `events.send`→wake 收件箱（不排队 run）。
- **身份合并**：池键与 `runtime.instance_id` = 注册 inst_id（`pool.acquire` 新增 `instance_id` 参数；legacy 无参调用保留随机 uuid 语义，仅供非 SDK 用法）。stop/status/`events.send` 唤醒按 inst_id 精确命中，不再按 spec 名塌缩。详见 ADR-046。

### 实现注记（2026-09-05，I-18 Program 执行器）

- **`client.program.run(script, session_id=…, sandbox_id=…, timeout=…, retries=…)`**：确定性零 token 脚本执行入口（Program = agent 类配置子集：无 LLM/无推理循环，**不构造 AgentRuntime**）——SDK 执行面又一入口；输出写 session 事件（program_started/program_completed），sandbox 可选（脚本自身写 FS，ADR-018）。实现：`harness/sdk/program.py`（with_retry/host runner/sandbox runner）+ `_ProgramClient`。详见 guide I-18 章节。

## CLI vs SDK 职责边界

| 层级 | 职责 | 禁止做的事 |
|------|------|-----------|
| **CLI** (`harness/cli/main.py`) | argparse 解析、参数校验、错误格式化、`print()` 输出、返回 exit code | 禁止解析 YAML、禁止拼接消息格式、禁止注入变量到 node.config |
| **SDK** (`harness/sdk/client.py`) | YAML/TOML 解析、业务编排、配置管理、服务组装、消息格式拼接 | 禁止直接 `print()` 到 stdout、禁止读取 `sys.argv` |
| **Internal** (`orchestrator/`...) | 执行引擎、状态机、持久化、LLM 调用、沙箱生命周期 | 禁止被 CLI/TUI 直接 import(只能通过 SDK) |

### 下沉原则

任何在 CLI 中通过 `--flag` 提供的功能,SDK 必须提供等价的参数:

```python
# CLI: harness run job.yaml --var time=12:00 --append "Be concise"
# ↓ 等价 SDK 调用
client.jobs.submit("job.yaml", vars={"time": "12:00"}, append="Be concise")

# CLI: harness exec <sid> "Hello" --append "Be concise"
# ↓ 等价 SDK 调用
client.sessions.events.send("<sid>", "Hello", append="Be concise")
```

CLI 只负责把命令行字符串翻译成 SDK 参数,所有业务逻辑在 SDK 层完成。

## 迁移路径

| 命令 | 之前 | 现在 |
|------|------|------|
| `harness run` | `Orchestrator.start_job()` | `client.jobs.submit()` |
| `harness status` | `Orchestrator.get_job_status()` | `client.jobs.status()` |
| `harness cancel` | `Orchestrator.cancel_job()` | `client.jobs.cancel()` |
| `harness gate` | `Coordinator.human_gate_resolve()` | `client.gates.approve/reject/modify()` |
| `harness session` | 直接 `SessionStore()` + `SessionManager()` | `client.sessions.list/show/rewind()` |

### 2026-05-19 更新

SDK 子客户端从 4 个扩展到 7 个:
- `prompts`(ADR-022):prompt 组装--assemble/build_messages/describe_tools
- `skills`(ADR-021):Skills 沙箱扫描--scan()
- `sandboxes`:沙箱生命周期--destroy/destroy_all/list
- `memory`:记忆压缩(已有)

### 2026-10-19 注记（SDK 公开面补齐 — 支撑 app 层 pi CLI）

背景：ADR-002（app 层）要求「严格只调 SDK 公开 API」；盘点发现 6 组缺口（ADR-002 §1.4）。本次补齐**只加公开门面、不改语义**：

| # | 公开面 | 签名 / 位置 | 说明 |
|---|---|---|---|
| 1 | 数据类导出 | `from harness.sdk import AgentSpec, SandboxConfig, ToolRegistry, HookMode, points` | app 构造 spec/sandbox/tool registry/hooks 不再直接 import 内部模块 |
| 2 | 项目根 | `HarnessClient(project_dir=...)` | 修复 `_project_dir` 硬编码 `Path.cwd()` 的假设；`config.profile()`、`memory.count_tokens()` 同源使用 |
| 3 | 工具接线 | `HarnessClient(tool_registry=...)`；`AgentInstancePool(..., tool_registry=)`、`AgentRuntime(..., tool_registry=None)` | **缺陷修复**：此前 runtime 固定 `load_builtins()`，`client.tools.register(...)` 注册的工具到不了 runtime（测试只能私有赋值 `runtime._tool_registry`）；默认仍 builtins，旧行为不变 |
| 4 | 成本度量 | `client.memory.event_cost(ev)` / `client.memory.span_cost(evs)` | 替代 app import `prompt_assembly._cost`；可传 `provider` / `reasoning_passback` |
| 5 | policy cfg | `client.policy.enable(name, agent, cfg=None)` | 供 `read_before_edit` preset 注入 `digest_provider` 与参数名映射；cfg 合并到规则**全局**定义（当前架构无 per-agent cfg，单 agent 使用） |
| 6 | policy guard 查询 | `client.policy.guard(agent)` | 返回编译后的 `SandboxRestriction`，替代 `from harness.policy import engine` |
| 7 | hooks 点位 | `client.hooks.points` + `HookMode` 导出 | app 不再 `from harness.hooks import HookRegistry, points` |
| 8 | LLM profile 列表 | `harness.sdk.config.available_llm_profiles()` | 修复 `client.config.profiles()` 的既有 `ImportError`（该方法此前必抛） |
| 9 | 回退 tokenizer 定位 | `_get_tokenizer`：相对 `tokenizer_dir` 先按 `$HARNESS_TOKENIZER_DIR`、再按 harness checkout 根解析 | 修复「app 在用户工作区运行时找不到 tokenizer_dir」；相对路径从此不再依赖进程 cwd |

约束不变：CLI / TUI / app 仍只能通过 `HarnessClient` 访问功能；内部模块不得被直接 import。上述 7 项随 ADR-002 的 L1 层实现并补 `tests/test_sdk_public_surface.py`。
