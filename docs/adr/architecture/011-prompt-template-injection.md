# ADR-011：系统提示词动态注入 — vars 模板 + prompt_append 双机制

**日期**：2026-05-14  
**状态**：已采纳  

## 上下文

Agent 的系统提示词需要在运行时注入动态值（时间、用户名、优先级）和单次任务特定指令（"只验证能量守恒"）。两种注入方式对 KV Cache 的影响不同：模板插值保持前缀结构不变，内容追加改变提示词长度和后续 token 序列。

## 决策

**同时提供两种机制，分阶段执行：先模板插值（`vars`），后内容追加（`prompt_append`）。**

### 机制 1：`vars` — 模板变量插值（KV Cache 友好）

```yaml
system_prompt: "Now time is {time}. Priority: {priority}."
config.vars = {"time": "00:20", "priority": "high"}
→ "Now time is 00:20. Priority: high."
```

使用 Python `str.format_map(_SafeDict)`。未定义的变量保留原占位符 `{unknown}`，不抛 KeyError。模板结构不变，只有变量值位置的少数 token 变化，前缀完全可用于 KV Cache。

### 机制 2：`prompt_append` — 内容追加

```yaml
system_prompt: "You are a helper."
config.prompt_append = "只验证能量守恒，忽略力场参数"
→ "You are a helper.\n\n[追加指令]\n只验证能量守恒，忽略力场参数"
```

追加在模板插值之后执行。每次追加内容不同会使后续 token 全部失效，但前缀仍可复用。适用于无法预制成模板的单次任务指令。

### 执行顺序

```
system_prompt (YAML)
  → format_map(vars)      # Phase 1: 模板插值
  → + "\n[追加指令]\n" + prompt_append  # Phase 2: 内容追加
```

## 理由

1. **Claude Code 验证**。逆向分析显示 Claude Code 使用 stable system prompt + task-specific injection 模式，92% prefix reuse rate。这正是 vars（stable）+ prompt_append（injection）的分离策略。
2. **适用场景分离**。高频动态值（时间、用户）适合 vars，低频单次指令适合 prompt_append。一个机制无法同时优化两种场景。
3. **命名空间隔离**。`vars` 和 `prompt_append` 都在 `config` 命名空间下，与 Phase/DAGNode 系统字段（id、agent、depends_on）完全隔离。
4. **CLI 可操作**。`--var key=value` 和 `--append text` 直接映射到 config 字段，无需 YAML 中间层。

## 后果

- `AgentRuntime.execute_task` 新增 `config` 参数（透传 DAGNode.config）
- `_build_messages` 每次调用执行两步注入
- `_SafeDict` 类确保缺失变量不抛异常
- 未来若系统提示词极长且变量变化频次过高，可考虑将 vars 注入点限制在 prompt 末尾而非全文 `format_map`
