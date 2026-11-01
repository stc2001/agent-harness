# ADR-018：Agent 间通信——共享文件系统模型

**日期**：2026-05-16（更新 2026-06-18）
**状态**：已采纳（简化）

> 2026-06-18：DAG 编排模块已移除。Agent 间通信改为工具驱动
> （spawn_subagent → read_subagent），不再通过 coordinator 的
> 并行节点合并。文件系统共享模型保留用于工具层面。
**参考**：Claude Code Agent Teams（Anthropic），[Shared State 协调模型](https://www.mindstudio.ai/blog/claude-code-agent-teams-parallel-shared-task-list/)

## 上下文

Harness 支持多 Agent 并行执行（`ThreadPoolExecutor` + `group_by_level`）。当 A、B、C 并行执行后，D、E 作为审查者需要获取 A/B/C 的产物。两个核心问题：

1. **Agent 间如何传递信息？** 选项：消息传递（事件队列）vs 共享状态（文件系统）
2. **Session 事件 vs 业务产物如何分工？** `events.jsonl` 记录过程，`/workspace/` 存放产物

行业实践对比：

| 框架 | 通信方式 | 适用场景 |
|------|---------|---------|
| **LangGraph** | Message Passing（事件流） | 对话式任务、条件路由 |
| **CrewAI** | Message Passing + 共享记忆 | 协作式任务、角色扮演 |
| **Claude Code Agent Teams** | **Shared State（文件系统）** | 编码任务、产物是文件 |

## 决策

**Agent 间不直接通信，通过共享文件系统协调。Session 事件记录过程，业务产物写入约定路径。**

### 设计原则

```
Agent A ──┐
Agent B ──┼──► /workspace/modules/  ───► Agent D (Reviewer)
Agent C ──┘     (共享文件系统)            Agent E (Reviewer)
         │
         └── events.jsonl (各自 session，不共享)
```

1. **Agent 零直接通信**：A 不知道 D 的存在，D 不知道 A 的存在
2. **文件系统即通信层**：A 写 `/workspace/modules/feature_a.py`，D 读同一文件
3. **Session 事件隔离**：A 的调试输出、LLM 思考过程留在 A 的子 session，不污染 D 的上下文
4. **约定路径**：产物位置由 `AgentSpec.system_prompt` 或 `DAGNode.config` 约定

### 配置示例

```yaml
# registry/coder.yaml — Worker Agent
name: coder
system_prompt: |
  你是模块实现 Agent。
  完成后将代码写入 /workspace/modules/{module_name}.py
  将测试结果写入 /workspace/tests/{module_name}_results.txt
tools: [bash, read_file, write_file]

# registry/reviewer.yaml — Reviewer Agent
name: reviewer
system_prompt: |
  你是代码审查 Agent。
  从 /workspace/modules/ 目录读取所有模块代码进行审查。
  审查结果写入 /workspace/reviews/{module_name}_review.md
tools: [read_file, write_file, bash]
```

```yaml
# job.yaml — 并行实现 + 并行审查
phases:
  - name: implement
    nodes:
      - id: A
        agent: coder
        config:
          module_name: "feature_a"
      - id: B
        agent: coder
        config:
          module_name: "feature_b"
  - name: review
    nodes:
      - id: D
        agent: reviewer
        depends_on: [A, B]
        config:
          review_targets: ["feature_a", "feature_b"]
      - id: E
        agent: reviewer
        depends_on: [A, B]
        config:
          review_targets: ["feature_a", "feature_b"]
```

## 理由

### 1. AI4S 场景的产物是文件

科学计算流水线产生的是代码、数据、实验报告，不是对话消息。文件系统是最自然的产物容器。

> *"The shared task list is typically stored as a file in the project directory... Every agent in the team has read/write access to this file. That's how they communicate — not through direct message passing, but through a shared persistent state."*
> — Claude Code Agent Teams Deep Dive

### 2. 与 Session 隔离互补不冲突

ADR-008（节点级 Session 隔离）防止并行节点的 LLM 上下文交叉污染。Shared State 模型与之互补：

| 维度 | Session 事件 (`events.jsonl`) | 共享文件系统 (`/workspace/`) |
|------|------------------------------|------------------------------|
| **内容** | LLM 上下文、工具调用过程 | 代码文件、测试结果、审查报告 |
| **消费者** | 同 Agent 的下一轮 LLM 调用 | 下游 Agent（Reviewer、Validator） |
| **生命周期** | 与 Agent 执行同生同灭 | 持久保留，跨 job 可见 |
| **隔离性** | 子 session 隔离 | 全局共享 |

### 3. 沙箱文件系统天然配合

Bubblewrap 沙箱通过 `shared_folders` 挂载宿主机目录：

```yaml
sandbox.yaml:
  main:
    shared_folders:
      - host_path: "/home/user/project"
        sandbox_path: "/workspace"
```

所有 Agent 共享同一个 `/workspace`，无需额外的 IPC 机制。

### 4. 可审计性与可调试性

文件系统通信天然可审计：
- `ls -la /workspace/modules/` 看到全部产物
- `git diff` 跟踪产物变更
- 人类开发者可直接打开文件检查 Agent 输出

对比消息传递：事件队列需要专门的日志查看工具，且消息可能过期丢失。

### 5. 与追加优先存储哲学一致

ADR-005 选择 JSONL 而非 SQLite，因为追加写简单、人类可读。Shared State 模型延续这一哲学：
- Agent 追加写文件（`write_file`）
- 审查者追加写审查报告
- 无需事务、无需锁、无需消息队列

## 后果

### 正面

- **零新增基础设施**：不需要消息队列、不需要事件总线、不需要进程间通信
- **天然持久化**：文件系统本身就是持久化存储
- **人类可介入**：开发者可直接修改 `/workspace/` 中的文件，干预 Agent 行为
- **跨 job 复用**：上一轮 job 的产物可被下一轮 job 直接读取

### 负面

- **延迟较高**：下游 Agent 需重新读取文件，而非直接消费内存中的消息
- **无原子性**：并发写同一文件可能冲突（需应用层约定"一个文件一个 Agent"）
- **不适合对话式任务**：如果 Agent 产物是文本回复而非文件，Shared State 不自然

## 未采纳的替代方案

- **方案 A：消息传递（EventBus 扩展）**：Agent 通过 `EventBus` 发送消息，下游 Agent 订阅。适用于对话式任务，但引入复杂度（消息格式、订阅机制、过期策略）。
- **方案 B：共享内存**：Agent 通过共享内存块传递数据。性能最高，但无法持久化、无法人类可读、调试困难。
- **方案 C：数据库共享状态**：产物写入 SQLite/Redis，Agent 通过 SQL/Key-Value 查询。支持结构化查询，但引入数据库依赖，与 MVP 的零配置哲学冲突。

## 与其他 ADR 的关系

| ADR | 关系 |
|-----|------|
| **ADR-008（节点级 Session 隔离）** | 互补。Session 隔离 LLM 上下文，文件系统共享业务产物。 |
| **ADR-005（JSONL vs SQLite）** | 一致。追加优先、人类可读、零配置。 |
| **ADR-004（Agent-Sandbox 解耦）** | 支撑。Agent 通过沙箱的 `shared_folders` 访问共享文件系统。 |
| **ADR-017（SDK 编排模式）** | `MapReview` 模板基于 Shared State 模型实现产物路径约定和结果聚合。 |

## 产物路径约定（最佳实践）

应用层使用 Shared State 模型时应遵循的路径约定：

```
/workspace/
├── modules/           # Worker 产物（代码、数据）
│   ├── feature_a.py
│   └── feature_b.py
├── tests/             # 测试结果
│   ├── feature_a_results.txt
│   └── feature_b_results.txt
├── reviews/           # Reviewer 产物
│   ├── feature_a_review.md
│   └── feature_b_review.md
└── summary.md         # 汇总报告（由 SDK 模板生成）
```

SDK `MapReview` 模板负责：
1. 自动生成 `{output_dir}/{task_id}.py` 路径并注入 Worker `config`
2. Worker 执行后将产物写入约定路径
3. Reviewer 从约定路径读取并生成审查报告
4. 聚合所有审查报告到 `summary.md`
