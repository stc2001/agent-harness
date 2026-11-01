---
id: I-78
title: AgentSpec 权限字段补全 — capabilities.session_write 与 version（P1-8）
labels: [ready-for-agent]
author: lead
created: 2026-10-21
status: open
---

## 背景 / What & Why

迁移自 TODO/unsolved_issue.md 的 P1-8。AgentSpec 的权限模型缺少只读写约束字段与
Spec 版本号，Validator 无法拒绝越权写入，Phase 2 技能系统也无法做版本兼容。

## 验收标准

- [ ] AgentSpec 增加 capabilities.session_write（bool，默认 false）与 version（string）
- [ ] Validator 对 session_write=false 的 spec 拒绝写会话（CapToken 强制执行路径有测试）
- [ ] registry YAML 向后兼容（缺字段使用默认值，不破坏既有 spec）
- [ ] 新增测试覆盖：默认值、显式 true/false、版本字段解析
- [ ] 文档更新：docs/adr 或 registry README 说明字段语义

## 不在范围内

- 其它权限字段（filesystem/net 等）的设计
- UI

## 关联

- 迁移来源：docs/archive/todo/unsolved-issue-2026-09-07.md（P1-8）
- ADR-014（AgentSpec 与凭证分离）、ADR-046（实例身份）
