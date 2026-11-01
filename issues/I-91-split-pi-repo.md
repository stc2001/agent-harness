---
id: I-91
title: 拆分 pi 上层应用到工作区外独立仓库；harness 仓库收敛为 kernel
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景 / What & Why

用户决定：harness 内核与 pi 上层应用拆成两个 repo；pi 移到工作区外单独文件夹；
其余按既有建议执行（Phase 0 文档/README + 拆分流程）。GitHub 账号待发布时提供。

拆分边界（已核实）：pi 仅 2 个文件 import harness.sdk；harness 不 import pi；
pi 测试 11 个文件 + pi_fake_llm；单 pyproject 双入口。

## 范围

1. 在工作区外创建 /home/gexingdeyun/pi-app（独立 git 仓库，latest 快照初始提交）：
   - pi/ 包、11 个 pi 测试 + pi_fake_llm + conftest + tokenizer fixture；
   - docs/adr（app 001/002）、docs/runbooks/memory-compaction.md；
   - README/CHANGELOG/LICENSE/CONTRIBUTING/SECURITY/CODEOWNERS/AGENTS/.gitignore/pyproject。
2. harness 仓库收敛：
   - 删除 pi/、pi 测试、pi fixture、app ADR、app runbook；
   - pyproject 只保留 harness 包与 harness 入口；
   - README 重写（除 DAG 过时内容、修死链、更新计数）；
   - CHANGELOG/CONTRIBUTING/SECURITY/CODEOWNERS/AGENTS/domain 更新；
   - 新增 docs/README.md 与 docs/plans/repo-split-plan.md。
3. 验证：harness 测试（无 pi）通过；pi 仓库测试通过；repo_audit 无 WARN。

## 验收标准

- [ ] /home/gexingdeyun/pi-app 是独立 git 仓库，pi 测试全绿
- [ ] OH_workspace 无 pi/ 与 pi 测试；pytest 通过；repo_audit WARN=0
- [ ] harness README 不再含 DAG/coordinator/orchestrator 描述与死链
- [ ] ADR 计数与 domain.md 一致（44）
- [ ] pi 仓库含 README/CHANGELOG/LICENSE/AGENTS 等文件

## 不在范围内

- 发布到 GitHub（等用户提供账号）
- pi 仓库自带 file-tracker（GitHub 原生 issue/PR 即可）
- harness 历史整理（已有 CHANGELOG 摘要）

## 关联

- 用户指示（2026-11-01）；I-89/I-90（治理文件）；ADR-002（pi 模式）
