---
id: I-85
title: 修正 agent-trace 分层 — sessions/ 是产品运行数据，不是 coding agent 轨迹
labels: [needs-triage]
author: lead
created: 2026-10-21
status: closed
---

## 背景 / What & Why

用户指出：sessions/ 不是 DSH（coding agent）的运行轨迹，而是本 harness 项目自己的运行记录。
经核实：

- sessions/ 由 harness/session_store 在**产品运行时**写入：
  sessions/<uuid>/events.jsonl、meta.json、audit.jsonl，另有 sessions/audit_log.jsonl；
  SDK 默认 sessions_dir=./sessions/。当前 6,035 目录 / 5,971 个 events.jsonl / 约 50MB，已被 .gitignore 忽略。
- coding agent（DSH）自己的会话存储是 ~/.dsh/sessions（DSH_HOME），不在本仓库。

此前 .dsh/skills/agent-trace-governance 把 sessions/ 列为「L2 可蒸馏成 ADR/issue/handoff」，
属于错误归因，会误导后续会话把产品运行日志当治理轨迹。

## 范围

1. .dsh/skills/agent-trace-governance/SKILL.md：
   - description 去掉「distil sessions into ...」，改为 distil agent work；
   - L2 行改为「产品运行数据，只做 debug/replay/metrics/保留期，不提炼成治理产物」；
   - 增加说明：sessions/ 是 harness SessionStore 产物，DSH 轨迹在 ~/.dsh/sessions；
   - Do not 增加「不得从 sessions/ 提炼治理产物」。
2. .dsh/skills/repo-hygiene-audit/SKILL.md：Trace volume -> Runtime volume（产品运行数据）。
3. .dsh/skills/repo-hygiene-audit/scripts/repo_audit.py：sessions 提示改为 runtime data（保留/轮转/勿提交）。
4. 调研文档 .scratch/skillsurvey/agent-trace-reassessment.md 同步更正（不入库）。

## 验收标准

- [ ] agent-trace-governance 不再声称 sessions/ 可蒸馏成 L1
- [ ] 明确写出 sessions/ 的产品运行数据属性与 DSH 会话的存放位置
- [ ] repo_audit.py 提示更新，运行 exit 0
- [ ] harness/、pi/、tests/ 零 diff

## 不在范围内

- 为 sessions/ 建立正式保留期/清理策略（归 I-77 可观测性范围）
- 历史 ADR/issue 改写

## 关联

- 用户澄清（2026-10-21）
- I-74（skill 安装）、I-77（运行时观测）
