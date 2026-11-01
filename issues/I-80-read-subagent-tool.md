---
id: I-80
title: 实现 read_subagent 工具（P2-4，APP 层拉取子 agent session 结果）
labels: [needs-triage]
author: lead
created: 2026-10-21
status: open
---

## 背景 / What & Why

迁移自 TODO/unsolved_issue.md 的 P2-4（另见 TODO/enhancements.md 工具生态）。
框架已提供 SessionStore 原语，ADR-036 定义了「spawn_subagent 推 + read_subagent 拉」的
分工，但工具本身未实现；agent_mgmt.py 只有文档提示。

## 验收标准

- [ ] APP 层提供 read_subagent(child_id/session_id) 工具，通过 client.tools.register 注册
- [ ] 读取子 agent 的 session 结果（完成/失败/进行中三态），错误语义明确
- [ ] 依赖注入 store 与 parent_session_id（ADR-036 示例），不直接读物理路径
- [ ] 测试：真实 SessionStore + fake LLM 的子 agent 场景；无 child 时返回明确错误
- [ ] 文档：ADR-036 / SDK 契约补充工具用法

## 不在范围内

- 推模式回传（ADR-036 已定拉模式）
- 子 agent 调度策略

## 关联

- 迁移来源：docs/archive/todo/unsolved-issue-2026-09-07.md（P2-4）
- ADR-036（subagent push vs pull）、ADR-018（共享文件系统）
