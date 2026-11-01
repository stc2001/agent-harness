---
id: I-71
title: 版本化 .dsh 开发工具（session-link 插件 + skills）并把 .scratch 移入忽略
labels: [tooling]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

`.dsh/` 目前被 `.gitignore` 忽略，但其中已有**辅助 DSH 开发的直接上手工具**：
`plugin/session-link`（新开发的 session link 插件）与 `skills/`（30 个 skill，
其中 16 个是指向 `education-agent-skills/` 的符号链接）。这些应随仓库版本化，
避免换机器/重装后丢失。同时 `.scratch/` 是本地草稿与测试日志，应整体忽略。

## 范围

- `.gitignore`：新增 `.scratch/`；移除 `.dsh/`（保留 `.dsh/appimg/`）。
- 纳入 `.dsh/`：`plugin/session-link/`（9 文件）与 `skills/`。
- 16 个指向 `education-agent-skills/` 的**绝对符号链接**替换为真实副本，使仓库自洽
  （`education-agent-skills/` 本身是独立 git 仓库，不纳入）。
- 清理 skill 文档中的本机绝对路径（`/home/gexingdeyun/...` → `~/...`）。

## 验收标准

- [ ] `.scratch/` 被忽略；`.dsh/` 不再被忽略且已跟踪
- [ ] `.dsh/plugin/session-link` 与 `.dsh/skills`（含 16 个原链接目标）完整入库
- [ ] 无本机绝对路径、无密钥/令牌/大文件/`appimg`
- [ ] 不改运行时代码，测试不受影响

## 关联

- 用户直接指示（2026-10-19）；DSH 开发辅助工具版本化
