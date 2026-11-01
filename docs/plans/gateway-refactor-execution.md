# Gateway 重构——详细执行步骤

> 依据: `docs/plans/gateway-refactor-plan.md`
> 原则: 每步完成后跑回归 → 提交 → open-code-review

---

## Step 1: 创建 gateway/ 骨架

**目标**: 新建目录和空壳文件，不碰任何现有代码。

**调用技能**: 无（纯创建）

**具体操作**:
```
mkdir -p harness/gateway/
touch harness/gateway/__init__.py
touch harness/gateway/gateway.py
touch harness/gateway/rules.py
```

`harness/gateway/__init__.py` 初始内容: 空，等 Step 3 补充导出。

**验证方式**: `python -c "import harness.gateway"` 不报错

**提交**: `chore(gateway): scaffold gateway/ directory`

---

## Step 2: 搬迁 gate_manager → gateway/

**目标**: 移动 `orchestration/gate_manager/impl.py` 到 `gateway/gate_manager.py`，改所有 import 引用。

**调用技能**: 无（纯搬迁 + import 替换）

**具体操作**:
1. 复制文件: `cp harness/orchestration/gate_manager/impl.py harness/gateway/gate_manager.py`
2. 改生产 import: `harness/coordinator/impl.py:28` — `from harness.orchestration.gate_manager.impl import GateManager, EventBus` → `from harness.gateway.gate_manager import GateManager, EventBus`
3. 改 `harness/orchestration/__init__.py:3` — 同上
4. 全量测试确认 100% pass

**验证方式**: `pytest tests/ -q --ignore=tests/test_e2e.py` 全绿

**提交**: `refactor(gateway): move GateManager + EventBus into gateway/`

---

## Step 3: 实现 Gateway.authorize() 核心逻辑

**目标**: 在 `gateway/gateway.py` 实现统一授权入口 + 三种 gate 的创建/等待/决议。**不删除旧代码路径**，GateManager 作为 Gateway 内部组件。

**调用技能**: **tdd**（无新行为测试——以现有回归为 green）

**具体操作**:
1. 实现 `GateContext` dataclass（`gateway/gateway.py`）
2. 实现 `AuthorizeResult` dataclass
3. 实现 `Gateway` 类:
   ```python
   class Gateway:
       def __init__(self, gate_manager: GateManager, session_store, agent_spec_svc):
           self._gate_manager = gate_manager
           self._rules = []
       
       def authorize(self, ctx: GateContext) -> AuthorizeResult:
           # 1. HumanGate check
           if ctx.gate_type in ("phase", "node", "atomic"):
               gate_id = self._create_gate(ctx)
               result = self._wait_for_resolution(gate_id, timeout=ctx.timeout)
               if result == "reject": return AuthorizeResult(denied)
               if result == "modify": ...
           # 2. Custom rules
           # 3. Return
       
       def resolve(self, event_id, decision, amend=None): ...
       def list_pending(self, session_id): ...
       def register_rule(self, fn, name): ...
   ```
4. 更新 `harness/gateway/__init__.py` 导出

**验证方式**: 
- `python -c "from harness.gateway import Gateway, GateContext, AuthorizeResult"` 不报错
- 此时不删除旧代码，不影响现有测试

**提交**: `feat(gateway): Gateway.authorize() unified authorization entry`

---

## Step 4: agent_runtime 切换

**目标**: `agent_runtime/impl.py` 中 `cap_token.verify()` + `_atomic_gate` 逻辑替换为 `gateway.authorize(ctx)`。

**调用技能**: **tdd**（回归为 green）

**具体操作**:
1. 删除 `cap_token.verify("toolcall", tool_name)` (approx line 286-300)
2. 删除 `_request_atomic_gate` 方法 (approx line 558-597)
3. 删除 `_resolve_atomic_gate` 辅助函数 (approx line 627-)
4. 删除 `resolve_atomic_gate` 方法 (approx line 599-)
5. 删除 `_atomic_gate_events` / `_atomic_gate_config` 实例变量
6. 删除 `from harness.cap_token.impl import CapTokenService` import
7. 在 `_execute_single_tool` 中替换为:
   ```python
   ctx = GateContext(gate_type="atomic", session_id=session_id, spec=self.spec,
                     tool_name=tool_name, tool_args=tool_args)
   result = self._gateway.authorize(ctx)
   if result.decision == "denied":
       return None  # 软继续
   if result.decision == "modified":
       tool_args = {**tool_args, **result.amend_args}
   ```
8. 构造函数参数: `cap_token_service` → `gateway`

**验证方式**: `pytest tests/test_agent_runtime.py -q` 全绿（需同步更新 mock）

**提交**: `refactor(agent_runtime): replace cap_token + atomic gate with gateway.authorize()`

---

## Step 5: coordinator 切换

**目标**: `coordinator/impl.py` 中 `cap_token` 引用 + `_check_node_gate` + `EventBus` 构造 + `human_gate_resolve` 替换为 Gateway 调用。

**调用技能**: **tdd**（回归为 green）

**具体操作**:
1. 删除 `from harness.cap_token.impl import CapTokenService` import
2. 删除 `EventBus` 构造 (line 59)
3. 删除 `GateManager` 构造 + EventBus 传递 (line 66)
4. 构造函数参数: 删 `cap_token_service`，注入 `gateway`
5. `human_gate_resolve` → `gateway.resolve(event_id, decision, amend)`
6. `_check_node_gate` → `gateway.authorize(ctx)` + 等待决议
7. `_amend_inputs` **保留**（编排逻辑不动）
8. 删除 `_gate_phases` / `_gate_sessions` dict（移入 Gateway）
9. `execute_phase` 中 node gate 检查: `if node.human_gate` → 调 `gateway.authorize(ctx)`

**验证方式**: `pytest tests/test_coordinator.py -q` 全绿

**提交**: `refactor(coordinator): replace node gate + cap_token with gateway`

---

## Step 6: orchestrator 切换

**目标**: `orchestrator/impl.py` 中 `cap_token` 引用 + `_poll_phase_gate` 替换为 Gateway 调用。

**调用技能**: **tdd**（回归为 green）

**具体操作**:
1. 删除 `cap_token_service` import + 构造参数
2. `_poll_phase_gate` → `gateway.authorize(ctx)` + 等待决议
3. Phase gate 检查: `if phase.human_gate` → 调 `gateway.authorize(ctx)`
4. Gate 决议状态读取: `PhaseStatus.WAITING_GATE` → 从 `gateway.authorize()` 返回值判断

**验证方式**: `pytest tests/test_orchestrator.py -q` 全绿

**提交**: `refactor(orchestrator): replace phase gate + cap_token with gateway`

---

## Step 7: SDK 切换

**目标**: `sdk/client.py` 中 `CapTokenService` 构造 + `_GatesClient` 内部调用切换为 Gateway。

**调用技能**: **open-code-review**（SDK 是公开 API，需格外仔细审查）

**具体操作**:
1. 删除 `from harness.cap_token.impl import CapTokenService`
2. `__init__`: 删除 `self._cap_token_svc = CapTokenService(...)`，改为 `self._gateway = Gateway(...)`
3. 删除传给 Coordinator/Orchestrator/Pool 的 `cap_token_service=` 参数
4. `_GatesClient.approve/reject/modify`: `self._client._coordinator.human_gate_resolve(...)` → `self._client._gateway.resolve(...)`
5. `_GatesClient.list_pending/get`: `self._client._coordinator._gate_phases` → `self._client._gateway.list_pending(...)`

**验证方式**: `pytest tests/test_sdk_client.py -q` 全绿

**提交**: `refactor(sdk): replace CapTokenService + gate dispatch with Gateway`

---

## Step 8: 协议更新

**目标**: `interfaces.py` 新增 `GatewayInterface` Protocol，删除 `CapTokenInterface` Protocol。

**调用技能**: 无（纯协议定义）

**具体操作**:
1. 新增 `GatewayInterface` Protocol:
   ```python
   class GatewayInterface(Protocol):
       def authorize(self, ctx: GateContext) -> AuthorizeResult: ...
       def resolve(self, event_id: str, decision: str, amend: dict | None = None) -> None: ...
       def list_pending(self, session_id: str) -> list[GateInfo]: ...
       def register_rule(self, fn: Callable, name: str) -> None: ...
   ```
2. 删除 `CapTokenInterface` Protocol (approx line 336-370)
3. 删除 `CapabilityToken` dataclass (approx line 87-94)
4. 更新所有 `CapTokenInterface` 类型标注 → `GatewayInterface`

**验证方式**: `pytest tests/ -q --ignore=tests/test_e2e.py` 全绿

**提交**: `refactor(interfaces): add GatewayInterface, remove CapTokenInterface`

---

## Step 9: 全量回归 + 适配

**目标**: 运行全量测试，修复所有 step 4-8 中未发现的适配问题。

**调用技能**: **tdd**（全量回归）+ **diagnose**（如有失败）

**具体操作**:
1. 全量测试（不含 E2E）: `pytest tests/ -q --ignore=tests/test_e2e.py`
2. E2E 测试: `pytest tests/test_e2e.py -q`
3. 修复所有失败
4. 同步更新 L1 文件（sandbox cap_token 死参数清理）:
   - `sandbox/impl.py`: 删 `_tokens` dict + `cap_token` 参数
   - `sandbox/manager.py`: 删 `cap_token=""` 参数
   - `sandbox/docker_impl.py`: 删 `cap_token` 参数
5. 删除测试文件: `tests/test_cap_token.py`
6. 更新 `tests/conftest.py`: 删 CapTokenService fixture，换 Gateway fixture

**验证方式**: 全量 100% pass

**提交**: `test: full regression — gateway refactor complete`

---

## Step 10: 清理旧目录

**目标**: 删除搬移后的旧目录。

**调用技能**: 无（纯删除）；之后 **context-compaction-check** 确认记忆准确

**具体操作**:
1. `rm -rf harness/cap_token/`
2. `rm -rf harness/orchestration/gate_manager/`
3. 全量测试最终确认: `pytest tests/ -q`

**验证方式**: 全量 100% pass；git status 干净

**提交**: `chore: remove old cap_token/ and gate_manager/ directories`

---

## 每步通用 Check

| 检查项 | 命令 |
|--------|------|
| 子模块回归 | `pytest tests/test_<module>.py -q` |
| 全量回归（无 E2E） | `pytest tests/ -q --ignore=tests/test_e2e.py` |
| E2E | `pytest tests/test_e2e.py -q` |
| 提交前 diff 确认 | `git diff --stat` |
| 提交后审查 | `ocr review --audience agent -b "<context>" --commit HEAD` |
