---
id: I-89
title: 补齐 LICENSE（MIT）与 CHANGELOG（含 git 历史摘要）
labels: [needs-triage]
author: lead
created: 2026-10-26
status: closed
---

## 背景 / What & Why

用户决定：LICENSE 采用 MIT。由于发布方式是 latest 快照、不携带 git 历史，
需要把历史信息凝练写入 CHANGELOG，供公开快照保留项目演进脉络。

## 范围

1. 新增 LICENSE：MIT 全文，Copyright (c) 2026 gexingdeyun。
2. 新增 CHANGELOG.md（Keep a Changelog 风格）：
   - 0.1.0（2026-10-26）首个整合快照的 Added/Changed/Fixed 摘要；
   - 项目历史摘要：2026-05-17 至 2026-10-26，739 个提交，按月分布；
   - 里程碑表：软服务工作流以来的全部 merge 提交（I-44…I-88）；
   - 说明：完整 git 历史仅保留在本地私有仓库，公开快照只带本摘要。
3. pyproject.toml：增加 license 字段（MIT）。
4. README.md：许可证小节链接 LICENSE/CHANGELOG；顺手修正「15 个 ADR」为 46。

## 验收标准

- [ ] LICENSE、CHANGELOG.md 存在且被 git 跟踪
- [ ] README 许可证小节指向 LICENSE 与 CHANGELOG
- [ ] pyproject 含 license = MIT
- [ ] repo_audit.py 的治理文件 WARN 从 5 降为 3（仅剩 CONTRIBUTING/SECURITY/CODEOWNERS）
- [ ] 无运行时代码变更（tests/ 零 diff）

## 不在范围内

- CONTRIBUTING / SECURITY / CODEOWNERS（另案）
- 创建 tag / 正式 release 流程

## 关联

- 用户直接指示（2026-10-26）
- I-87/I-88（最近的治理整理）
