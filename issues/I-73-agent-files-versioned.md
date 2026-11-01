---
id: I-73
title: 版本化 agent 指令入口 AGENTS.md，并把外部 education-agent-skills 移入忽略
labels: [tooling]
author: lead
created: 2026-10-20
status: closed
---

## 背景 / What & Why

用户指示：agent 相关文件应随仓库版本化，做法对齐参考仓库
`/home/gexingdeyun/deepseek-harness/`（该仓库跟踪 `AGENTS.md`、`CLAUDE.md`、
`.agents/skills`，只忽略本机 `settings.json` / `commands` / worktrees）。

当前本仓库已跟踪 `.dsh/skills`、`.dsh/plugin/session-link`、`skills/`、
`docs/agents/`，但根目录 `AGENTS.md` 被 `.gitignore` 忽略且未入库——
换会话 / 换机器 / 新 clone 后 agent 拿不到项目级指令（CodeGraph 使用规则等），
是本次调研（Git 治理/仓库卫生）里确认的最大治理缺口之一。

另：`education-agent-skills/` 是独立 git 仓库（远程为
`garethmanning/education-agent-skills`，经 gh-proxy 克隆），当前既未入库也未忽略，
导致 `git status` 常驻未跟踪噪声；按 I-71 的结论「该外部仓库本身不纳入」，
应显式忽略。

## 范围

- `.gitignore`：移除 `AGENTS.md`；新增 `education-agent-skills/`（外部 clone，仅作参考）。
- 将现有 `AGENTS.md`（35 行，仅通用 agent 指令，无本机路径/密钥）纳入版本控制。
- 保持 `.opencode/`（57MB，含 node_modules）与 `opencode.jsonc`（含本机绝对路径）忽略：
  二者属本机运行配置，对齐 DSH 只忽略 settings 的做法。
- 不改运行时代码。

## 验收标准

- [ ] `git ls-files AGENTS.md` 有输出；`git check-ignore AGENTS.md` 返回非 0
- [ ] `git check-ignore education-agent-skills` 返回 0；`git status` 不再出现该目录
- [ ] `harness/`、`pi/`、`tests/` 零 diff；无新增密钥/本机绝对路径
- [ ] MR 合并后 `origin/main` 上存在 `AGENTS.md`

## 不在范围内 / Out of scope

- `.opencode/`、`opencode.jsonc` 入库（本机配置；如需共享 MCP 配置另开 issue 做可移植版本）
- 把 `education-agent-skills` 改为 submodule
- 新建 `CLAUDE.md`（本仓库 agent 客户端均读 AGENTS.md）

## 关联

- 用户直接指示（2026-10-20）
- I-71（`.dsh` 版本化 + `education-agent-skills` 不入库的既有结论）
- 本会话调研：`.scratch/skillsurvey/git-governance-repo-hygiene-skills.md`
