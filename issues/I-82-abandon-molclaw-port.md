---
id: I-82
title: 【wontfix】放弃 MolClaw → Agent Harness 移植计划（ADR-038 移除 Pipeline 编排）
labels: [wontfix]
author: lead
created: 2026-10-21
status: wontfix
---

## 原计划（2026-05-20）

TODO/molclaw_port_plan.md 规划四阶段移植：技能文件、Agent 定义 + Pipeline 编排、
SCP/MCP 工具接入、MolBench 评估。

原文归档：docs/archive/plans/molclaw-port-plan.md

## 放弃原因

1. **ADR-038（Accepted 2026-06-18）移除固定 DAG/Pipeline 编排**：coordinator、
   orchestrator、scheduler、executor、sdk/patterns.py 均删除；原计划 Phase 2 的
   「Pipeline 编排」前提消失。
2. **从未落地**：仓库中无 molclaw/molbench 代码，仅在 diary 与 TODO 文档中出现；
   该计划是探索性分析，不是已批准的实现路线。
3. 技能系统已由 ADR-021 落地（skills 沙箱注入 + 渐进披露），与 MolClaw 技能文件
   移植目标重叠；MCP 需求已在 I-80/enhancements 单独跟踪。

## 处置

- 不实现；原文移入 docs/archive/plans/ 保留追溯。
- 若要引入外部 agent 框架，先写新 ADR 说明与 ADR-038 的关系。

## 关联

- ADR-038（DAG removal）、ADR-021（skills 注入）
- docs/archive/plans/molclaw-port-plan.md
