# ADR-016：审计日志 — 全局 + per-session 双写与强制全量审计

**日期**：2026-05-16
**状态**：已采纳

## 上下文

Agent Harness 作为 AI4S（AI for Science）场景的编排框架，安全审计是底线要求。MVP 阶段需要覆盖以下审计轨迹：
- Agent 生命周期（创建/复用/销毁）
- Sandbox 生命周期（创建/销毁）
- Job 生命周期（创建/完成/失败/取消）
- 权限决策（文件访问拒绝、工具调用拒绝）
- Human Gate 决策（approve/reject/modify）
- Token 签发

最初设计考虑将所有审计信息作为附加字段写入 session 的 `events.jsonl`，但很快发现两个问题：
1. **归属模糊**：Agent 创建发生在 session 之前（先有 Agent 实例，后有 session），沙箱销毁可能不归属任何 session
2. **权限混合**：session 事件是业务数据（用户可读），审计事件是安全数据（仅管理员/审计系统可读），混在同一个文件里会导致权限边界泄漏

## 决策

### 1. 全局 + per-session 双写

```
sessions/
├── audit_log.jsonl          ← 全局：所有事件
├── sess_001/
│   ├── events.jsonl         ← 业务事件（对话上下文）
│   ├── audit.jsonl          ← 该 session 的审计事件
│   └── meta.json
└── sess_002/
    └── ...
```

**全局日志**记录系统级事件：Agent 创建/销毁、Sandbox 创建/销毁、Job 生命周期。这些事件可能不归属某个 session。

**per-session 日志**记录 session 内发生的安全事件：权限拒绝、Token 签发、Gate 决策。与全局日志内容完全一致，只是按 session 分片，便于独立归档和并发写入。

```python
audit.append(
    level="warn",
    category="permission",
    event="file_access_deny",
    session_id="sess_001",      # ← 存在时同时写入 per-session
    detail={"path": "/etc", "reason": "not_in_permissions"},
)
```

### 2. 审计日志与 session 事件严格分离

| 维度 | Session 事件 (`events.jsonl`) | 审计日志 (`audit_log.jsonl`) |
|------|------------------------------|------------------------------|
| **本质** | 业务事件 | 安全事件 |
| **记录内容** | "Agent 说了什么、调用了什么工具" | "谁拒绝了文件访问、谁审批了 Gate" |
| **消费者** | 用户（对话回放）、rewind/fork | 审计系统、合规检查、事后溯源 |
| **生命周期** | 与 session 同生同灭 | 永久保留 |
| **权限** | 普通用户可读 | 仅管理员/审计系统可读 |

两者唯一关联是共享 `session_id`/`job_id`，绝不合并存储。

### 3. 强制全量审计，无开关

MVP 阶段审计不是可选功能。`HarnessClient` 初始化时自动创建 `AuditLog` 实例，所有模块通过构造函数注入，没有 `audit_enabled` 开关。

### 4. threading.Lock 保护写入

每个 `AuditLog` 实例持有一把 `threading.Lock`，`append()` 方法加锁后写入。全局实例被多线程共享时串行化，per-session 实例仅在单线程内使用也安全。

当前架构是单机同步代码，`ThreadPoolExecutor` 引入多线程但仍在单进程内，threading.Lock 足够。Phase 3 分布式若引入多进程，届时再评估文件级锁或独立审计服务。

## 理由

1. **消费方不同决定存储分离**。session 事件给 LLM 上下文用（需要 rewind），审计日志给外部审计系统用（需要按时间线全局查询）。消费方不同 → 存储分离。

2. **并发写入分散**。全局日志在 Job 并行执行时会有多线程竞争，per-session 日志天然按 session 分片，竞争面缩小到单个 session 内。双写策略下，全局日志的竞争集中在系统级事件（Agent/Sandbox 生命周期），这些事件频率低；高频的权限决策事件写在 per-session 文件，无全局竞争。

3. **强制审计避免"忘记打开"**。如果提供开关，生产环境可能因配置遗漏而关闭审计。AI4S 场景要求可审计性是不可协商的底线。

4. **最小固定字段 + 自由 detail**。审计事件格式固定 `ts/level/category/event/job_id/session_id/detail`，不定义严格的 Pydantic 子类。新增事件类型只需在调用方传入新的 `event` 字符串和 `detail` 字段，无需改模型。审计日志是"写得多、读得少"，事后用 `grep`/`jq` 查询，不需要程序化的类型安全。

## 后果

- 新增 `harness/audit_log.py`（~60 行），`AuditLog` 类提供 `append()` 方法
- `HarnessSettings` 新增 `audit_log_dir` 字段（默认 `"./sessions/"`）
- `HarnessClient` 初始化时创建 `AuditLog` 实例，注入 `Orchestrator`、`Coordinator`、`AgentInstancePool`、`SandboxManager`、`NodeExecutor`、`AgentRuntime`
- 各模块在关键生命周期点埋点，共 16 个审计事件
- CLI 新增 `harness audit [--tail N] [--follow]` 命令查看审计日志
- 新增 `tests/test_audit_log.py`（12 个测试）覆盖双写、格式、并发安全

## 未采纳的替代方案

- **方案 A：单文件全局日志**。所有事件写入一个 `audit_log.jsonl`。简单，但 Job 并行执行时多线程竞争同一文件，高频权限决策事件会成为瓶颈。
- **方案 B：审计事件写入 session 的 `events.jsonl`**。在现有业务事件中插入 `audit_` 前缀的事件类型。避免了新文件，但混淆了业务事件和安全事件，权限边界泄漏，且 Agent/Sandbox 生命周期事件无法归属 session。
- **方案 C：可配置开关**（`audit_enabled: bool`）。灵活，但生产环境可能因配置遗漏关闭审计。MVP 阶段明确拒绝。
- **方案 D：Pydantic 子类模型**（`FileAccessDenyEvent`、`TokenIssuedEvent` 等）。类型安全，但每加一个事件类型要改序列化鉴别器，审计日志的消费方是外部系统（ELK/Splunk），不需要程序化的类型安全。
