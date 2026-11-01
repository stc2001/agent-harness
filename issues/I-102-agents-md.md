---
id: I-102
title: 重写 AGENTS.md 为本仓库专用的 agent 操作手册
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景 / Why

云端 AGENTS.md 目前只有一行仓库边界说明 + CodeGraph 通用块，缺少本项目
agent 必需的：环境/测试命令、issue/MR 流程、main 保护与发布规则、代码约定、安全边界。

依据 AGENTS.md 开放格式（https://agents.md，README for agents；只要求 Markdown，
无固定 schema）与 GitHub Copilot 的 root/nested 约定，重写为项目专用。

## 范围

- 重写 AGENTS.md：定位/环境/测试与 lint/工作流/main 保护/发布/代码约定/安全/文档地图/完成标准。
- 保留 CodeGraph 工具块（本机 MCP 仍在使用）。
- 不修改其他文件。合并后 fast-forward 追加到 GitHub，CI 复跑。

## 验收

- [ ] AGENTS.md 含可执行命令与仓库专属规则
- [ ] 链接指向存在的 docs/脚本
- [ ] repo_audit WARN=0；CI 3.11/3.13 success

## 关联

I-95~I-101（CI/CD）；docs/plans/repo-split-plan.md
