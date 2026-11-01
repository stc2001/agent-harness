# ADR-042：统一 hooks 模块（注册表 / 上下文 / hook 点）

## Status

Accepted (2026-08-19)

## Context

框架当前的特设钩子分散四处、语义各异、无法组合：`gateway.register_hook`（PreHook/PostHook 生命周期）、`AgentRuntime._tool_hook`（PostToolUse 回调）、`_content_filter`（tool_result 内容过滤）、`SandboxMutexHook`（沙箱竞态锁）。问题：

1. **不可发现**：没有统一注册表，mission/OTel 等新消费方无法挂接。
2. **client 间不可见**：注册表实例挂在每个 `HarnessClient` 内部，client A 的 middleware client B 看不到。
3. **语义不统一**：回调签名、错误处理、返回约定各写各的。

## Decision

### 1. 新模块 `harness/hooks/`（叶子依赖）

```
harness/hooks/
├── __init__.py
├── registry.py    # HookRegistry: on(point, mode, fn, priority) / trigger(...)
├── context.py     # HookContext 数据类 + contextvars 承载当前上下文（含 trace 字段）
└── points.py      # 规范 hook 点名常量（tool.execute / llm.call / agent.pre_step / ...）
```

**红线**：模块不 import 任何业务模块（session/gateway/runtime）——叶子依赖，可独立测试。

### 2. 四模式语义（不可违反）

| 模式 | 返回值 | 语义 | 错误隔离 |
|------|--------|------|----------|
| observe | 忽略 | 只观察，不改数据 | 异常吞记（日志） |
| transform | 替换 payload | 返回值替换原值 | 异常回退原值 |
| veto | 非 None = 拒绝 | 短路后续 + 拒绝 | 异常 fail-closed（deny） |
| around | 包裹 call() | 前/后/异常包裹 | 异常透传 |

### 3. 优先级确定性

同 hook 点按 `priority` 降序执行；同 priority 按注册序。结果可复现。

### 4. contextvars 传播

`HookContext` 的 trace/归因字段由 contextvars 承载，嵌套 hook、跨线程、跨 async 边界不丢字段。

### 5. 进程级注册表 + SDK 视图

- 注册表实例为**进程级单例**（模块级），修掉 client A 的 hook client B 不可见。
- SDK 暴露 `sdk.hooks` 视图（`_HooksClient`），只是注册表外观，不复制状态。

### 6. 规范 hook 点（mission/OTel 消费方）

| 点名 | 模式 | 消费者 |
|------|------|--------|
| `authorize.veto` / `authorize.observe` | veto/observe | 迁移 Gateway pre/post hook（ADR-035） |
| `tool.execute.around` | around | 迁移 `_tool_hook`（PostToolUse） |
| `tool.result.transform` | transform | 迁移 `_content_filter` |
| `llm.call` | around | 路由（I-16）+ 计量（I-15）+ span（I-21） |
| `agent.pre_step` | observe | 插话窗口（I-20） |
| `agent.lifecycle` | observe | start/idle/stop 追踪 |
| `agent.stop.framework` | veto | 硬闸门（I-12） |
| `agent.stop.app` | veto/observe | 软裁决（I-12） |

### 7. 迁移策略（I-09）

每迁一处特设钩子到统一注册表，就删除一处特设代码；`SandboxMutexHook` 保留为 builtin hook 挂新注册表。行为等价（旧测试不变）。

### 8. 混合模式边界（2026-08-19 补充，代码调用点已核实）

**hooks 全局共享，业务服务实例隔离——这是终态形态，不是过渡态。**

| 层 | 作用域 | 实例 | 依据 |
|----|--------|------|------|
| hooks 注册表（`HookRegistry`） | **进程级全局单例** | 全进程一份 | §5；全树 11 处 trigger 全部 `HookRegistry.get()`，无 per-client 副本 |
| gateway / agent_runtime / store / pool / sandbox / audit | **per-client 实例隔离** | 每个 `HarnessClient` 一套 | `client.py:105-112` 每 client 新建 |

理由：

1. **广播者无需共享**：gateway/agent_runtime 内部只是 `HookRegistry.get().trigger(...)`——无论实例化几套，trigger 都指向同一全局总线。共享的是"总线"，不是"触发方"。
2. **业务状态隔离是特性**：gateway rules、sandbox 实例、session、audit 是隔离边界（client A 的规则/沙箱不应污染 client B）；全局化 gateway 会引入共享状态竞态。
3. **消费方（mission/OTel）只依赖全局 hooks**：注册一次（`client.hooks.on(...)`）即覆盖进程中所有 client 的所有触发点，无需接触任何业务实例。

**明确不做**：不抽"共享 core"（I-09 曾提的 Gateway/Store/Pool 共享化动议——hooks 进程级单例已用更轻方式达成同目标，无需共享业务实例本身）；不做跨进程 hook 注册。

**I-15 实现注记（2026-09-04，代码已落地）**：token 计量原语在 `llm.call` 的 final（`AgentRuntime._call_llm_once`→`do_call`，`agent_runtime/impl.py`）捕获服务端 `response.usage`，经 `set_context_fields(tokens_in/tokens_out)` 写入 contextvars——HookContext 的 `tokens_in/tokens_out` 字段（§1）即由此填充。**口径**：服务端 usage 原样透传，`usage=None` 时不填（无估算兜底）；逐 attempt 独立、失败 attempt 无 usage → None、框架不跨 trigger 累积。**消费/账本归 app 层**：app 在 `llm.call` around hook 中调 `next_fn()` 后读 `current_context()`（隐性契约：around ctx 构建于 final 前，`ctx.tokens_in` 恒 None）自行落审计/路由/预算——不做 `harness/mission/metering.py` 框架级账本，不改架构。测试：`tests/test_llm_backend.py::TestTokenMetering`。

**hook_result 契约注记（2026-09-07）**：`tool.execute` 触发的广播 payload 恒为请求字典——`hook_result` 事件**仅当某 around hook 实际改写载荷（返回新对象）时**才发出（identity 判断），否则会与 `tool_result` 重复镜像、污染事件流与观测计数（修复见 `agent_runtime/impl.py` `_fire_post_tool_hook`）。hook_result 不进 LLM 上下文（build_messages 不消费），纯观测/审计产物。

## Consequences

- **正面**：mission/OTel 获得唯一扩展点；client 间 hook 互通；错误处理统一；语义可测试；业务隔离边界清晰。
- **代价**：迁移期间双轨并存（特设 + 注册表）；contextvars 引入隐式状态（需文档化）；hook 全局作用域需要 hook 内部按 `ctx.agent` 等字段自行隔离。
- **明确不做**：不做分布式 hook 注册（跨进程）；不做热插拔（运行时卸载 hook）；不做 hook 依赖排序（只做优先级）；不做业务服务共享化（§8）。

## References

- ADR-035（Gateway 授权 + PreHook/PostHook）
- ADR-006（显式编程——hook 是确定性扩展点，不替 agent 做控制流）
- I-08/I-09（issue 清单）
