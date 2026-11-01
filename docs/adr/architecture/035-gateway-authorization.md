# ADR-035: Unified Gateway Authorization Engine

## Status

Accepted (2026-06-10)

## Context

Before this refactor, authorization was scattered across four modules:

1. **CapToken** (`harness/cap_token/`) — JWT-based tool call and subagent spawn verification
2. **GateManager** (`harness/orchestration/gate_manager/`) — event persistence for human gates
3. **Coordinator** — node-level human gate polling (`_check_node_gate`)
4. **Orchestrator** — phase-level human gate polling (`_poll_phase_gate`)
5. **AgentRuntime** — per-tool atomic human gate (`_request_atomic_gate`)

This scattered architecture caused multiple problems:

- **Redundant permission checks**: `cap_token.verify("toolcall", tool_name)` duplicated `ToolRegistry` filtering by `spec.tools`
- **Inconsistent gate mechanisms**: Atomic gate used `threading.Event` (transient), node/phase gates used JSONL polling (persistent)
- **No unified authorization entry point**: callers had to know which module handled which check
- **cap_token was in-process JWT overkill**: single-node deployment doesn't need cryptographic tokens between processes

## Decision

Converge all authorization into a single **Gateway** module at `harness/gateway/`.

### Architecture

```
                   ┌──────────────────┐
                   │     Gateway      │
                   │                  │
   Agent ─────────→│ authorize(ctx)   │──→ AuthorizeResult
                   │                  │
                   ├── human_gate     │── 3 gate types unified (Event-based)
                   ├── rules          │── user-defined callbacks
                   ├── hooks          │── PreHook / PostHook lifecycle
                   └── audit          │── unified event emission
                   └──────────────────┘
```

### Authorization Flow

```
authorize(ctx):
  1. Pre-hooks (SandboxMutexHook, rate limiter, etc.)
  2. Custom rules (spec.tools enforcement, content filtering)
  3. HumanGate if gate_type is set (waits for human resolution)
  4. Post-hooks (cleanup, lock release, metrics)
  5. Return AuthorizeResult(approved|modified|denied, amend_args)
```

### Key Design Choices

#### PreHook / PostHook Lifecycle

Gateway supports two hook types that wrap the authorization flow:

```
PreHook.before_authorize(ctx)  →  return AuthorizeResult to block, None to continue
                                    ↓
                          [ rules + gate ]
                                    ↓
PostHook.after_authorize(ctx, result)  →  cleanup, metrics, lock release
```

**Design decisions**:

1. **Pre-hooks CAN block** by returning `AuthorizeResult("denied", ...)`. Post-hooks are observation-only.
2. **Both class-based and callable hooks supported**. `PreHook`/`PostHook` base classes provide protocol clarity; plain callables provide zero-boilerplate usage.
3. **Post-hooks guaranteed to fire**. Wrapped in `try/finally` — even if HumanGate throws, post-hooks still run. Critical for lock-based hooks (SandboxMutexHook).
4. **Post-hooks don't change the result**. They receive the result and can log/cleanup, but cannot override the decision. Intentional design choice: authorization is immutable once decided.
5. **Hook errors are logged, not propagated**. `logging.exception()` captures stack traces without crashing the agent loop. Fail-safe: a broken hook doesn't break authorization.
6. **Callable return values wrapped automatically**. If a callable pre-hook returns `True` or a string, Gateway wraps it as `AuthorizeResult("denied", reason=str(val))`.

**Built-in hook**: `SandboxMutexHook` uses per-sandbox `threading.RLock` (reentrant for same-thread calls) to serialize tool execution when N agents share one sandbox. PreHook acquires the lock; PostHook releases it via a separate callback.

**Rationale for RLock over Lock**: Same-thread nested `authorize()` calls (e.g., agent calling tool A, then tool B while A's authorization is still in progress) would deadlock with non-reentrant Lock. RLock allows same-thread re-acquisition while still blocking cross-thread access.

1. **Pre-hooks → Rules → Gate** ordering. Automated policy (rules) runs first and can deny without human involvement. Human gate only triggers after rules pass.
2. **Callable-based rules and hooks**. No interface registration required — pass any `Callable[[GateContext], bool|str]` or PreHook/PostHook subclass.
3. **GateManager absorbed into Gateway**. GateManager was a thin event emitter (39 lines). Gateway now owns EventBus internally for gate events.
4. **CapToken removed entirely**. Single-process deployment doesn't need JWT. All permissions are read directly from `AgentSpec` via `ctx.spec`.
5. **Post-hooks fire regardless of outcome**. Guaranteed by wrapping `_evaluate()` in try/finally, enabling cleanup (e.g., SandboxMutexHook lock release).
6. **SDK exposes safe subset**. `_GatewayPublicClient` wraps Gateway, hiding `resolve()` from end users — gate resolution must go through Coordinator.

### Removed Modules

| Module | Reason |
|--------|--------|
| `harness/cap_token/` | In-process JWT redundant; replaced by Gateway reading `AgentSpec` directly |
| `harness/orchestration/gate_manager/` | Moved to `harness/gateway/gate_manager.py` |
| `AgentRuntime._atomic_gate_*` | Replaced by `Gateway.authorize(ctx, gate_type="atomic")` |
| `Coordinator._check_node_gate()` | Replaced by `Gateway.create_gate() + wait_for_resolution()` |
| `Orchestrator._poll_phase_gate()` | Replaced by `Gateway.create_gate() + wait_for_resolution()` |

### New Modules

| Module | Purpose |
|--------|--------|
| `harness/gateway/gateway.py` | Core authorization engine |
| `harness/gateway/hooks.py` | PreHook / PostHook base classes + SandboxMutexHook |
| `harness/gateway/gate_manager.py` | Gate lifecycle (persisted via EventBus → SessionStore) |
| `harness/gateway/rules.py` | Custom rule registration |

### Framework / APP Boundary

Gateway provides mechanism; APP defines rules. Follows ADR-006 (explicit
programming) and ADR-017 (framework doesn't do control flow).

**Framework** (`harness/gateway/`, `harness/orchestration/`, `harness/prompt_assembly.py`):

- `GateContext.gate_type`: `"atomic"`, `"sandbox_spawn"`
- `authorize(ctx) → AuthorizeResult`: pre-hooks → rules → gate → post-hooks
- `register_rule(fn, name)`: accepts `Callable[[GateContext], AuthorizeResult | None]`
- `sandbox_spawn`: `pool.acquire()` invokes `gateway.authorize()` before
  `sandbox_manager.get_or_create()`. Rules inspect `ctx.config` (SandboxConfig).
  Pool records `amend_args` in audit log generically; never interprets them.
- `build_messages(content_filter=...)`: callback slot to filter tool_result
  content before LLM context. Framework provides the slot; APP provides the logic.

**APP** (user code, `app/`):

- Concrete rules: `env_sanitizer_rule(ctx) → AuthorizeResult("modified", amend_args={...})`
- Content filters: `redact_secrets(text) → text`  (replaces `sk-*` with `[REDACTED]`)
- Registration: `gateway.register_rule(env_sanitizer_rule)`,
  `build_messages(content_filter=redact_secrets)`

**Key constraint**: framework never hardcodes security policies. A rule can
return `"modified"` without denying the action. SandboxConfig is never mutated
by framework — tools inside sandbox need real credentials. Only LLM context
is filtered.

## Consequences

### Positive

- **Single authorization entry point**. All callers invoke `gateway.authorize(ctx)` — no need to know which module handles which check.
- **Extensible via hooks and rules**. Users register callables for custom policies, rate limiting, sandbox serialization.
- **Sandbox serialization built-in**. `SandboxMutexHook` prevents race conditions when N agents share one sandbox.
- **Code reduction**. Net ~-100 lines production code (removed ~200 cap_token lines, added ~100 gateway lines).
- **Simpler testing**. Gateway tests cover all authorization paths; removed ~350 lines of scattered test code.

### Negative

- **Post-refactor test churn**. ~16 test files required import/path updates during migration.
- **Orchestrator gate behavior change**. Phase gates now use Gateway's blocking wait instead of polling loop — requires explicit `gateway.resolve()` in tests.
- **No JWT for potential future multi-node deployment**. If remote agent pools are needed later, JWT can be re-added behind Gateway's interface without changing callers.

## Defense-in-Depth Verification

Three API surfaces were tested for Gateway bypass (red-team tests):

| Surface | Enforcement | Verified |
|--------|:---:|:---:|
| `Pool.acquire()` | Constructor requires Gateway parameter (no default) | ✅ |
| `AgentRuntime.__init__` | Constructor requires Gateway parameter (no default) | ✅ |
| `_execute_single_tool()` | Must call `gateway.authorize()` before tool execution | ✅ |
| Gateway deny → sandbox blocked | Sandbox.execute() not called when denied | ✅ |

These are not runtime policy checks — they are architectural invariants
enforced by the constructor signatures and internal call sites.  No
runtime overhead is incurred for the verification.

## References

- Supersedes: ADR-001 (cap_token module), ADR-026 (atomic human gate)
- Related: ADR-024 (JSONL events), ADR-033 (tool error soft-continue)

### 2026-10-19 注记（deny 的执行期闭环）

`Gateway.authorize()` 返回 denied 后，runtime 现在除 `tool_called(allowed=False)`
与 `agent_failed` 外，还会补一条**配对到该 tool_call id 的 `tool_result`**
（`[tool error] <reason>`），并由 `_build_messages` 跳过对应 `agent_failed` 的
user 映射（`represented_by_tool_result`）。这把「拒绝」从单向观测变成模型可
自纠的软继续闭环；安全语义不变（工具未执行、审计仍记录）。详见 ADR-033
2026-10-19 注记；测试 `tests/test_denial_feedback.py`、
`tests/test_pi_security.py`。
