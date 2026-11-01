# ADR-046：Agent 实例身份与运行时分派（I-25，同模板多实例并发）

## Status

Accepted (2026-09-04)

## Context

同一 agent 模板（AgentSpec）需要作为多个**相互独立**的 agent 实例同时运行：编排者注册两个同模板实例、各自跑互不干扰的任务。I-25 之前存在三重身份错位：

1. **注册侧**：每个 `agents.register(spec)` 生成唯一注册 id（`inst_{uuid}`，`sdk/client.py` `_InstanceRegistry`），session metadata 绑定该 id。
2. **运行时侧**：`AgentInstancePool.acquire(spec)` 按 **spec 名**扫描复用，新建时另生成**随机 uuid** 作池键（`harness/orchestration/pool.py`）——注册 id 与运行时 uuid 两个命名空间互不关联。
3. **公共 API 塌缩**：`agents.run`/`agents.stop`/`agents.status`/`events.send` 唤醒按 spec 名查找（`list_instances(spec_name)`/首个实例），导致同名兄弟实例互相误伤：stop 可能停错实例、status 返回兄弟状态、第二个同模板 run 被「忙」拒绝。

## Decision

### 1. 身份合并：运行时 = 注册实例的专属运行时（I-25/D1）

每个已注册实例拥有一个**懒建常驻运行时**：池键与 `runtime.instance_id` = 该注册 inst_id（`pool.acquire` 新增可选 `instance_id` 参数，精确键控 `(spec_name, instance_id)`；首跑懒建，此后复用）。同模板两实例的运行时 uuid **天然互异**（构造性保证，非约定）。

- `instance_id=None`（legacy）：保留随机 uuid 键语义，仅供非 SDK/测试用法——其运行时无法被按注册实例定位，SDK 执行面一律走 instance-keyed。
- 池内实现注记：`get_or_create` 返回的沙箱后端 id 复用局部名 `instance_id`，函数开头以 `requested_instance_id` 捕获防遮蔽。

### 2. 分派键：run/wake/stop/status 按 inst_id 精确定位（I-25/D2）

| API | 改造 |
|-----|------|
| `agents.run(instance_id, ...)` | `pool.acquire(spec, instance_id=inst.instance_id)`——执行该实例自己的运行时 |
| `agents.run(spec, ...)` | spec 自动注册 → **每次都是新实例**（不再塌缩到同模板运行中实例） |
| `_wake_session`（`events.send`） | 按 session metadata 的 inst_id 精确唤醒/懒建该实例运行时 |
| `agents.stop(instance_id)` | `pool.get_by_instance_id` 精确命中（running 才接受 app stop，ADR-044 契约不变） |
| `agents.status(instance_id)` | `get_by_instance_id` → 运行时状态；无运行时回落 registry COMPILED |

### 3. Busy 语义 = 实例级（I-25/D3）

同一实例 RUNNING 时再次 `run(inst_id)` 保持 raise（同步契约不变，不排队）；同实例 follow-up 走 `events.send`→wake 收件箱队列（I-11 FIFO）。不同实例互不误伤。

### 4. 明确不做

- 不做 run 排队（busy 即 raise；排队是 `events.send` 收件箱语义）。
- 不做沙箱按实例隔离——同模板实例共享 `spec.sandbox` 配置容器，workspace 按 session 隔离（沙箱 uuid 身份见 **I-26**，本 ADR 不涉）。
- 不做动态重绑/迁移（I-27 候选）。

## Consequences

- **正面**：公共 SDK 路径支持「注册两个同模板实例、各自 run 并发跑」；stop/status/唤醒不再误伤同名兄弟；单 id（注册 inst_id）贯穿 register→run→session→stop/status/wake，消除双命名空间。
- **代价**：`AgentRuntime.instance_id` 语义从「池内随机 uuid」变为「注册 inst_id」（审计 detail 与线程命名随之携带注册 id）；`acquire(spec)` 无 instance_id 的随机 uuid 运行时成为受控 legacy 用法（无法被按实例定位，需文档约束）。
- **依赖**：I-23/I-24（SDK 执行面）先决；行为修正牵连 `tests/test_sdk_run.py`（busy 测试改为同实例语义）。
- **衔接**：I-26（沙箱 uuid 身份 + agent→sandbox 关联）与 I-27（绑定策略层）在本 ADR 的「注册实例 ⇄ 运行时」不变量之上叠加。

## References

- ADR-015（SDK 单一入口——run 语义随 I-25 修正，见其 I-25 实现注记）
- ADR-043（生命周期 idle/wake/多实例放行——§4 注记指向本 ADR）
- ADR-044（Stop 双轨——stop 按实例命中，仅 running 接受 app stop）
- Issues: I-25（本决策来源）/ I-26 / I-27
