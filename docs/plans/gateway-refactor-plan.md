# Gateway 重构计划

> 日期: 2026-05-13
> 状态: 计划确认，待执行

## 目标

将散落在 `coordinator`、`orchestrator`、`agent_runtime`、`gate_manager` 中的授权/门禁逻辑收敛为统一的 Gateway 授权引擎。砍掉 `cap_token` 模块（进程内授权无需 JWT）。

## 当前问题

1. **三种 HumanGate 各自实现**：Node gate 在 coordinator，Phase gate 在 orchestrator，Atomic gate 在 agent_runtime，等待机制不统一（threading.Event vs JSONL 轮询）
2. **cap_token 全冗余**：toolcall / subagentspawn / session 绑定均可用 AgentSpec + Gateway 直接替代（单进程无需 JWT）
3. **权限日志分散**：各工具各自调 `audit_log.append()`，格式不统一
4. **GateManager 是纯事件发射器**：39 行代码，不持状态、不做等待、不做裁决

## 目标架构

```
                    ┌──────────────────┐
                    │     Gateway      │
                    │                  │
    Agent ─────────→│ authorize(ctx)   │──→ AuthorizeResult
                    │                  │
                    ├── human_gate     │── 三种 gate 统一暂停/决议
                    ├── rules          │── 自定义回调（跨agent/内容/配额）
                    ├── gate_manager   │── 事件发射（EventBus 内嵌）
                    └── 审计日志       │── 统一记录
                    └──────────────────┘
                             │
                             ↓
                    ┌─────────────────┐
                    │    Sandbox      │
                    │                 │
                    │ execute_with()  │── bwrap namespace
                    │                 │── Landlock per-path r/w (未来)
                    │                 │── firewall network
                    │                 │── env_vars
                    └─────────────────┘
```

Gateway 直接裁决（不执行）：approve → 放行，reject → 拒绝，modify → 返回 amend_args。
调用方（agent_runtime / coordinator）自行应用 amend。

## 目录结构

```
harness/gateway/
├── __init__.py          ← Gateway, GateContext, AuthorizeResult
├── gateway.py           ← authorize() 统一入口 + 三种 gate 逻辑
├── gate_manager.py      ← GateManager + EventBus（从 orchestration/gate_manager/ 搬入）
└── rules.py             ← 自定义规则回调注册/评估
```

## 核心接口

### GateContext

```python
@dataclass
class GateContext:
    gate_type: Literal["phase", "node", "atomic"]
    session_id: str
    spec: AgentSpec
    tool_name: str | None
    tool_args: dict | None
    phase_name: str | None
    node_id: str | None
```

### AuthorizeResult

```python
@dataclass
class AuthorizeResult:
    decision: Literal["approved", "modified", "denied"]
    amend_args: dict | None         # modify 决策的修正参数
    reason: str | None              # deny 时的原因
```

### Gateway

```python
class Gateway:
    def authorize(self, ctx: GateContext) -> AuthorizeResult: ...
    def resolve(self, event_id: str, decision: str,
                amend: dict | None = None) -> None: ...
    def list_pending(self, session_id: str) -> list[GateInfo]: ...
    def get_gate(self, event_id: str, session_id: str) -> GateInfo | None: ...
    def register_rule(self, fn: Callable, name: str) -> None: ...
```

## 统一授权流程

```
gateway.authorize(ctx):

  1. 检查是否触发 HumanGate
     ├── ctx.gate_type == "phase"  → 创建 phase gate → 等待决议
     ├── ctx.gate_type == "node"   → 创建 node gate → 等待决议
     └── ctx.gate_type == "atomic" → 创建 atomic gate → 等待决议

     决议结果:
     ├── "approve"  → continue
     ├── "reject"   → return AuthorizeResult(denied, reason="human gate rejected")
     └── "modify"   → result.amend_args = amend_input

  2. 评估自定义规则
     for rule in self.rules:
         verdict = rule(ctx)
         if verdict == BLOCK:
             return AuthorizeResult(denied, reason=f"rule: {rule.name}")

  3. 返回授权结果
     return AuthorizeResult(approved|modified, amend_args=amend_input)
```

## 权限真相源

Gateway 直接读 `ctx.spec`，无需 JWT 中介：

```
tool 白名单:    ctx.spec.tools（ToolRegistry 已按此过滤）
subagent 权限:  ctx.spec.permissions.get("subagent_spawn", False)
session 归属:   ctx.session_id（Gateway 构造时注入）
file_access:    ctx.spec.permissions.get("file_access", {})
```

## cap_token 删除

整个 `harness/cap_token/` 目录删除。理由：

- 单进程系统无需 JWT 跨服务授权
- 所有校验均可由 Gateway 直接读 AgentSpec 替代
- 如果未来加远程 agent 池（多节点），再加回——Gateway 接口不变

## 审计日志统一

```
Gateway.authorize() → 统一记一条:

{
  "event": "gateway_authorize",
  "decision": "approved|modified|denied",
  "agent": "validator",
  "tool": "bash",
  "session": "abc123",
  "amend_args": {"text": "corrected"} | null
}
```

代替分散的 `tool_permission_deny` / `atomic_gate_rejected` / `file_access_deny`。

## 改动分级

### L3 — 重度（逻辑迁移）

| 文件 | 改动 | 生产 delta |
|------|------|-----------|
| `agent_runtime/impl.py` | 删 cap_token.verify + _atomic_gate → gateway.authorize(ctx)；modify 时读 result.amend_args 自己做 args merge | -30/+20 |
| `coordinator/impl.py` | 删 cap_token 引用 + EventBus 构造 + _check_node_gate + _gate_phases + human_gate_resolve → gateway；_amend_inputs 保留（编排逻辑不动） | -30/+15 |
| `orchestrator/impl.py` | 删 cap_token 引用 + _poll_phase_gate → gateway | -18/+10 |
| `sdk/client.py` | 删 CapTokenService init + _GatesClient → gateway.resolve() / gateway.list_pending() | -15/+15 |

测试：`test_agent_runtime.py`, `test_coordinator.py`, `test_orchestrator.py` — mock 从 CapTokenService 构造换 Gateway

### L2 — 中度（构造器 wiring）

| 文件 | 改动 |
|------|------|
| `orchestration/executor/impl.py` | 删 cap_token_service + event_bus 构造参数，注入 gateway |
| `orchestration/pool.py` | 删 cap_token_service，注入 gateway |
| `sdk/client.py` `__init__` | 删 CapTokenService(...) + EventBus 构造，改 Gateway(...) |
| `interfaces.py` | 新增 GatewayInterface Protocol，删 CapTokenInterface |

测试：`test_pool.py`, `test_executor.py`, `test_sdk_client.py`, `test_integration.py`, `test_mock_llm.py`

### L1 — 轻量（import 重写）

| 生产 | 测试 |
|------|------|
| `sandbox/impl.py` (删 cap_token 死参数) | `test_cap_token.py` (删除) |
| `sandbox/manager.py` (删 cap_token 死参数) | `conftest.py` (删 CapTokenService fixture) |
| `sandbox/docker_impl.py` (删 cap_token 死参数) | `test_skills.py`, `test_tools.py`, `test_audit_log.py` |

### L0 — 不改

| 文件 | 原因 |
|------|------|
| `cli/main.py` | Phase.human_gate=True 只是数据 |
| `session/impl.py` | gate 事件名保留 |
| `prompt_assembly.py` | 不涉及权限 |
| `tool_registry.py` | 不涉及权限 |

## 改动量汇总

| 指标 | 数值 |
|------|------|
| 受影响生产文件 | 11（比前版少 2） |
| 受影响测试文件 | 13（test_cap_token.py 删除） |
| 生产代码净 delta | ~+100 / -95 行 |
| 测试代码净 delta | ~+250 / -350 行（删 1 整文件） |
| 核心重构时间 | 2-3 小时 |
| 回归风险 | 中等（三种 gate 行为测试已有覆盖率） |

## 执行步骤

| # | 内容 | 类型 |
|---|------|------|
| 1 | 创建 `harness/gateway/` 骨架 (`__init__.py`, `gateway.py`, `rules.py`) | 新建 |
| 2 | 搬 `orchestration/gate_manager/impl.py` → `gateway/gate_manager.py`（EventBus 并入 Gateway 内部）；改所有 import | 搬迁 |
| 3 | 实现 `Gateway.authorize()` + 统一三种 gate 创建/等待/决议；内部持有 GateManager + EventBus | 新逻辑 |
| 4 | `agent_runtime/impl.py`：删 cap_token.verify + _atomic_gate → `gateway.authorize(ctx)`；modify 时读 `result.amend_args` 自己 merge 到 tool_args | 重构 |
| 5 | `coordinator/impl.py`：删 cap_token 引用 + EventBus 构造 + node gate → `gateway.create_gate("node", ...)`；_amend_inputs 保留 | 重构 |
| 6 | `orchestrator/impl.py`：删 cap_token 引用 + phase gate → `gateway.create_gate("phase", ...)` | 重构 |
| 7 | `sdk/client.py`：删 CapTokenService + `_GatesClient` → `gateway.resolve()` / `gateway.list_pending()` | 重构 |
| 8 | `interfaces.py`：新增 `GatewayInterface` Protocol，删 `CapTokenInterface` | 协议 |
| 9 | 全量测试适配 + 回归 | 测试 |
| 10 | 删 `harness/cap_token/` 和 `harness/orchestration/gate_manager/` 旧目录 | 清理 |

## 不变项

| 组件 | 说明 |
|------|------|
| `_check_access` in `file_io.py` | 暂时不动（等 Landlock 接手后统一删除） |
| `tool_registry.py` | spec.tools 过滤逻辑不变 |
| `prompt_assembly.py` | 不涉及权限 |
| `sandbox/*` | 除 cap_token 死参数清理外不动 |
| `session/impl.py` | gate 事件名保留 |
| `cli/main.py` | Phase.human_gate=True 数据不动 |
| SDK 公开 API | `client.gates.approve/reject/modify/list_pending` 签名不变 |

## 后续演进

- **Landlock 集成**：Gateway 算好允许读/写的路径列表，传给 Sandbox.apply_landlock_policy()
- **跨 agent 文件追踪**：Gateway 记录每次文件操作，支持"agent B 不能删 agent A 的产物"
- **内容级规则**：Python 回调 `def no_sudo(ctx): ...` → Gateway.rule.register(no_sudo)
- **配额规则**：单 session 最多 100 次 LLM 调用，Gateway.rules 中评估
- **多节点扩展**：加回 JWT cap_token 时，Gateway 接口不变，仅换底层实现
