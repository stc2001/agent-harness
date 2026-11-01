---
id: I-83
title: 增强路线图盘点（迁移自 TODO/enhancements.md）
labels: [needs-triage]
author: lead
created: 2026-10-21
status: open
---

## 背景 / What & Why

TODO/enhancements.md 是 2026-08-02 的路线图，尚未按 issues/ 新规范登记；部分条目已完成，
部分与其它 issue 重复，部分仍开放。迁移为一张带处置结论的盘点表，后续挑中项再开独立 issue。

原文归档：docs/archive/roadmaps/enhancements-2026-08-02.md

## 条目处置

| 条目 | 处置 |
|---|---|
| 推模式 token 截断、Skills 系统、Prompt 组装 SDK | ✅ 已完成（见 ADR-021/022/034/053） |
| read_subagent 工具 | → I-80 |
| 进程崩溃恢复、可观测性、Docker 集成测试 | → I-77 |
| web_search / web_fetch 工具 | 开放，未立项（需先定沙箱网络策略） |
| MCP 协议支持 | 开放，未立项（ToolRegistry 已支持 DI，缺 client 适配） |
| hashline_read 结构摘要 | 开放，方案见 docs/plans/hashline-structure-read-plan.md |
| Agent metrics（token/工具调用统计） | → I-77 的可观测性范围 |
| Context rot 检测 | 开放，未立项 |
| 拉模式上下文策略 | 开放，未立项（与 skills 拉取模式相关） |
| 记忆层升级（跨 session 查询/断点续传） | 开放，未立项（ADR-034 基线之上） |
| events.find_by_parent / sessions.rewind / fork SDK 暴露 | 开放，未立项 |
| Sandbox cgroup、OpenSandbox、信息流控制（IFC） | 开放，未立项（cgroup 并入 I-77 评估） |
| MemPrivacy、分布式调度、多租户/配额、AI4S | P3 远期，未启动 |
| DAG/Pipeline 相关 | 已由 ADR-038 移除，不再跟踪 |

## 验收标准

- [ ] 上表每一项都有明确处置（issue 引用 / 开放 / 已废弃）
- [ ] 挑中的条目另开独立 issue，不在本 issue 内实现
- [ ] 本 issue 只作路线图索引，关闭时机由维护者决定

## 关联

- 迁移来源：docs/archive/roadmaps/enhancements-2026-08-02.md
- I-77、I-80
