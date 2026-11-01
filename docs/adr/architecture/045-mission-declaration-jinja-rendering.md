# ADR-045：mission 声明层——Jinja 前端渲染与双模式编排

## Status

Accepted (2026-08-19)

## Context

mission 编排已定论归 **app 层**（DSH 驱动端），py harness 只提供执行原语。mission 的图声明面临两个问题：

1. **图固定性**：图在声明时形状固定，但真实任务（如翻译）的并行数量 N（chunk 数）是**运行期**才知道的——图无法预写「几个 worker」。
2. **声明与运行分层不清**：模板参数化（同图不同输入）、批量 worker 预声明、条件节点（是否加 verify/抽检）等「声明期」能力与 JIT 动态构建、运行期路由混在一起。

Jinja2 的 `{% for %}` / `{% if %}` 能力正好覆盖声明期形状生成（Ansible/Helm 先例），因此选定 Jinja 作为 mission 图的前端渲染引擎。

## Decision

### 1. 渲染流水线（app 层）

```
Jinja 模板（.j2）──渲染──► 图 YAML（纯数据）──Schema 校验──► JIT 编译──► mission 执行
   {{ params }}              （可审计/diff）   （JSON Schema/   （环/双根/缺父/深度）
   {% for %}/{% if %}                            Pydantic）
```

### 2. 声明层弱化（Jinja 覆盖的能力）

| 能力 | 原设计 | 弱化方式 |
|------|--------|----------|
| 模板参数化（原 I-22） | mission 自研模板机制 | **直接删**——Jinja 就是模板引擎，`{{ params }}` + `{% for %}` 承担同图多实例 |
| 批量 worker 预声明（翻译场景） | 图里静态定义 N 个 translator | `{% for i in range(M) %}` 渲染期生成 M 个槽位；任务量 N 由运行期 manifest 消费（guide 附录 A 方案 1） |
| 静态条件节点 | ConditionalRouteHook 承担静态分支 | `{% if use_cloud_spot_check %}` 渲染期决定图形态；运行期数据依赖分支仍留给 hook |

### 3. 双模式并存（关键）

- **模式 A（模板化批次任务）**：Jinja 渲染**静态图**（M 固定、N 归 manifest）→ 一次编译 → 执行。`/mission append` 的主路径弱化为「改模板 → 重新渲染」。
- **模式 B（agent 自主编排）**：JIT 动态构建保留（`/mission append/detach/modify` 逐条插入、增量编译），适合长程协同、图随执行演化。
- 两者不冲突：**Jinja 是「提交前」的图生产器，JIT 是「提交后」的图裁判与演进器**，时间轴不重叠。

**确认（2026-08-19）：CLI/JIT 命令式与 Jinja 声明式不可合并**——时间点（提交前 vs 提交后）、操作对象（整图 vs 单条指令）、错误类型（模板语法/参数错 vs 图语义错）三者不同，硬合并会丢失各自确定性。保持 A/B 双模式：

- **Jinja = 图的声明式源码语言**（离线、全量、模板即图程序；长期资产模板 + 一次性 `mission run <tpl> -p k=v` 都用它）；
- **CLI/JIT 指令 = 图的运行控制语言**（在线、增量、`append/modify/wave -modify/interject` 每条过 JIT）；
- **运行中调整桥**：改模板 → 重新渲染出新图 → diff（旧 vs 新）→ 生成 JIT 指令序列逐条应用（terraform plan 模式）。Jinja **不直插运行时**（全量渲染会碾掉 RUNNING/PAUSED 执行状态）。

### 4. 沙箱红线

Jinja 必须用 `SandboxedEnvironment` + 白名单过滤器/函数（默认 Environment 可访问 Python 对象属性，模板即任意代码执行）。渲染产物是纯 YAML——**拒绝非数据形状的输出**。

### 5. 明确不弱化（运行期语义）

Jinja 不承担、也不替代以下功能（ADR-006 确定性核心，全在提交后）：

- JIT 校验（环/双根/缺父/深度/manage_scope）——Jinja 渲染的 YAML 仍需校验
- Agent 级状态机（PENDING→READY→RUNNING→PAUSED→终态 + 锁定规则）
- AutoTrigger / ConditionalRoute（运行期依赖执行结果，时间点与渲染不同）
- wave -modify / interject（运行期对已渲染图的手术）
- 权限矩阵（Root/Parent/Member、manage_scope）

## Consequences

- **正面**：图固定性问题系统性解决（模板参数化 + 数量归运行期 + 运行期可调三层）；声明层瘦身（自研模板机制删除）；渲染产物可审计/diff；静态分支上移降低运行期 hook 复杂度。
- **代价**：双模式并存增加理解成本（静态渲染 vs JIT 动态构建）；Jinja 沙箱配置是安全红线；渲染产物与 JIT 之间的错误定位需要分层（模板错→渲染层，图错→JIT）。
- **归属**：本 ADR 的决策在 app 层实现；py harness 不感知 Jinja（只消费已编译图对应的执行原语）。

## References

- ADR-006（显式编程——JIT 校验仍是确定性核心，不被模板绕过）
- ADR-038（框架提供原语、APP 决定调度）
- `mission-control-implment-plan.md`（mission 设计文档，本 ADR 为其声明层决策）
- guide 附录 A（翻译 MVP：固定池 + manifest 消费 + wave-modify）
