---
id: I-74
title: 安装 workspace 级 agent 治理/仓库卫生 skills（.dsh/skills，7 个）
labels: [tooling]
author: lead
created: 2026-10-21
status: closed
---

## 背景 / What & Why

按用户指示，把调研筛出的「agent 轨迹保留」所需 skill 安装到本工作区
（DSH project-dsh 根：`.dsh/skills`，rank 100），只在本仓库生效，不装到
`~/.dsh`。上游多为 GitHub/Claude Code 绑定，故按本仓库 soft-serve 流程改写，
保留 MIT 归属。

对应 5 个来源：dayu-harness、laurigates git-plugin、rjwalters/repo、
Skillkeel、repo-standardizer；另加本项目自研的 trace 治理与审计脚本。

## 范围

新增 7 个 skill（`.dsh/skills/<name>/SKILL.md`）：

1. `agent-trace-governance` — L1/L2/L3 轨迹分级、蒸馏流程、issue/MR schema
2. `git-commit-workflow` — conventional commit + issue/MR 关联 + 提交前验证
3. `mr-review-workflow` — MR 评审、head SHA、approve/merge 与 tracker 更新
4. `repo-hygiene-audit` — 离线卫生/轨迹审计（含 `scripts/repo_audit.py`）
5. `secret-audit` — 工作树与历史密钥扫描（含 `scripts/secret_scan.sh`）
6. `dependency-audit` — 依赖漏洞/过期/锁文件审计
7. `governance-files` — 治理文件模板（5 个 template，soft-serve 版）

另加 `.dsh/skills/_attribution/NOTICE.md`（MIT 归属）。

## 验收标准

- [ ] 7 个 SKILL.md 通过 frontmatter 校验（kebab-case、与目录同名、description 非空）
- [ ] 本会话 skill catalog 已出现 7 个新 skill（实测）
- [ ] `python3 .dsh/skills/repo-hygiene-audit/scripts/repo_audit.py` 可运行（预期 exit 1，因已知历史 key）
- [ ] `bash .dsh/skills/secret-audit/scripts/secret_scan.sh` 可运行（tree 3 hits / history 22 hits）
- [ ] `harness/`、`pi/`、`tests/` 零 diff
- [ ] MR 合并后 `origin/main` 含 `.dsh/skills/agent-trace-governance` 等 7 个目录

## 不在范围内 / Out of scope

- 修 `docs/agents/issue-tracker.md` 与 `domain.md` 的漂移（审计已报，另开 issue）
- 清理历史 legacy key（I-72 已登记，另案处理）
- 安装到用户级 `~/.dsh/skills`

## 关联

- 用户直接指示（2026-10-21）
- I-73（agent 指令入库）
- 调研与再评估：`.scratch/skillsurvey/git-governance-repo-hygiene-skills.md`、
  `.scratch/skillsurvey/agent-trace-reassessment.md`
