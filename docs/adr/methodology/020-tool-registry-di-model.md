# ADR-020: ToolRegistry 依赖注入模型 — 显式声明 vs 上下文对象

**日期**: 2026-05-19  
**状态**: 已接受  

## 上下文

Agent Harness 原先将 10 个工具硬编码在 `AgentRuntime` 中——方法绑定在类上，访问 `self._sandbox` / `self.spec.permissions` / `self.token_service` 等隐式状态。P2-1（ToolRegistry + 内置工具重构）将工具抽离为独立模块，但工具函数不再拥有 `self` 引用，如何注入运行时依赖（sandbox、权限、审计日志）成为核心设计选择。

## 决策

**采用显式依赖声明（DI Container 模式）**。工具函数通过 `deps=["execute", "permissions"]` 声明所需依赖，`ToolRegistry.inject()` 在运行时绑定。

```python
# 工具声明
@registry.register("read_file", "Read a file", {"path": str},
                   deps=["execute", "permissions", "audit_log"])
def read_file(path, execute=None, permissions=None, audit_log=None):
    ...

# 运行时注入
registry.inject({
    "execute": lambda cmd: sandbox.execute(instance_id, cmd),
    "permissions": spec.permissions,
    "audit_log": audit,
})
```

## 被否决的替代方案

### 方案 B：ToolContext 上下文对象

```python
def read_file(path, ctx):
    ctx.execute(f"cat {path}")       # ctx 聚合所有依赖
    ctx.permissions.check(path)
```

**否决理由**：可测试性退化。测试 `read_file` 的权限拒绝逻辑需要构造完整 `ctx`，即使被测路径只依赖 `ctx.permissions`。函数签名（`def read_file(path, ctx)`）不揭示真实依赖。

### 方案 C：AgentRuntime 保持内联方法

**否决理由**：工具无法独立注册、独立测试。与 ADR-010（原生 tool calling）方向一致——工具应该脱离 AgentRuntime 成为可组合单元。

## 理由

1. **可测试性优先**（ADR-006 显式编程精神的延伸）。纯函数工具（echo、sum）零依赖声明，可直接单测。带依赖的工具只需 mock 声明的 deps。
2. **签名即文档**。`deps=["execute", "permissions", "audit_log"]` 比 `ctx: ToolContext` 更清晰地表达「这个工具会执行命令、检查权限、写审计日志」。
3. **工具文件独立性**。工具不 import 任何 harness 内部模块（SandboxInterface、CapTokenService 等），依赖通过闭包注入——工具可移植到其他运行时。
4. **spawn_subagent 特殊处理保持简洁**。spawner 是 AgentRuntime 实例级配置，可能为 None。走调用侧传入（`handler(**args, spawner=self._spawner)`）而非 DI，避免在 inject 时处理 Optional 依赖的语义模糊。

## 后果

- `ToolRegistry.inject()` 需维护 `_original` 引用以保证双重注入幂等（每次 inject 从原始函数重新包装）
- 新增依赖类型时需在 `AgentRuntime._inject_tool_deps()` 中追加键值，而非修改共享的 Context 类
- 工具函数签名增加关键字参数（`execute=None, permissions=None, ...`），对 LLM 不可见——`get_definitions()` 不输出 deps 参数
- 审计日志 `append` 签名差异（list vs AuditLog）由工具层 try/except 兼容，未引入 DI 层抽象

### 2026-05-31 更新

服务级 DI 补全：Coordinator、Orchestrator、AgentInstancePool 的构造函数类型标注从具体实现类改为 `interfaces.py` 中已有的 Protocol（如 `AgentSpecInterface`、`SessionInterface`、`CoordinatorInterface`）。Scheduler 加入 Coordinator 构造函数作为可选注入参数。组合根（`HarnessClient`）仍负责具体类的实例化——DI 树的根必然知道所有具体实现。
