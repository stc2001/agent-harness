---
id: I-90
title: 补齐 CONTRIBUTING / SECURITY / CODEOWNERS（governance-files 模板）
labels: [needs-triage]
author: lead
created: 2026-10-26
status: closed
---

## 背景 / What & Why

I-89 之后 repo_audit 仍报 3 个治理文件缺失：CONTRIBUTING.md、SECURITY.md、CODEOWNERS。
用户指示使用 .dsh/skills/governance-files 模板补齐。

## 范围

1. 新增 CONTRIBUTING.md：soft-serve issue/MR 流程、分支/提交规范、测试与评审要求、
   本地环境、文档/ADR 要求、agent 贡献同规。
2. 新增 SECURITY.md：私下报告渠道（本仓库仅本地 soft-serve，无公开 issue 通道）、
   支持版本 0.1.0、凭据规则、secret 扫描入口、历史不随快照发布说明。
3. 新增 CODEOWNERS：soft-serve 不解析，作为评审归属约定；单维护者 @gexingdeyun，
   按 harness/pi/.dsh/docs/scripts/issues/merge-requests 分路径。
4. README 增加「参与贡献」小节，链接三个文件。

## 验收标准

- [ ] 三个文件存在且被跟踪，模板占位符全部替换
- [ ] repo_audit 的治理文件 WARN 3 -> 0（INFO 仍 3）
- [ ] README 链接有效
- [ ] 无运行时代码变更

## 不在范围内

- VISION.md（可选，不阻塞）
- 引入多人 CODEOWNERS 映射（当前单维护者）

## 关联

- 用户直接指示（2026-10-26）；I-89（LICENSE/CHANGELOG）
- .dsh/skills/governance-files（模板来源）
