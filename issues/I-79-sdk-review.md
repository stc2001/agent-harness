---
id: I-79
title: SDK 人工审核收尾 — _SandboxClient / _PromptsClient（P2-3）
labels: [ready-for-human]
author: lead
created: 2026-10-21
status: open
---

## 背景 / What & Why

迁移自 TODO/unsolved_issue.md 的 P2-3。SDK 客户端逐模块人工审核已完成
_GatesClient、_MemoryClient、_SessionsClient、_AgentsClient；剩余 _SandboxClient 与
_PromptsClient 未完成逐行审核与测试覆盖。

## 验收标准

- [ ] _SandboxClient：逐方法核对公开契约（生命周期、复用、错误语义），补测试
- [ ] _PromptsClient：逐方法核对组装/预览契约，补测试
- [ ] 审核记录（模块、结论、发现、修复）写入本 issue 或 docs/agents/human-review-tracker.md
- [ ] 发现的缺陷另开 issue，不在本 issue 内顺手改
- [ ] 全量 SDK 公开面清单核对完成（docs/developer-sdk-contracts.md）

## 不在范围内

- 新增 SDK 能力；ADR 改写

## 关联

- 迁移来源：docs/archive/todo/unsolved-issue-2026-09-07.md（P2-3）
- docs/agents/human-review-tracker.md
