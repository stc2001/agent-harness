---
id: I-87
title: 移动 issues-evidence/ 到 docs/evidence/（证据属文档资产）
labels: [needs-triage]
author: lead
created: 2026-10-25
status: closed
---

## 背景 / What & Why

用户确认：issues/ 与 merge-requests/ 留在根目录（是 pre-receive 策略锚定的 tracker，
不是文档）；只有 issues-evidence/ 属文档型资产，移入 docs/。

issues-evidence/ 目前只有 l3-03-grill-evidence.md（8.4KB，L3-03 规格收敛的原始实测数据），
被 ADR-034、ADR-053、harness/prompt_assembly.py docstring、两个测试的注释引用；
没有任何代码按路径读取，移动不影响运行与测试行为。

## 范围

1. git mv issues-evidence/l3-03-grill-evidence.md -> docs/evidence/l3-03-grill-evidence.md。
2. 新增 docs/evidence/README.md：说明 evidence 目录用途（ADR/issue 的实测证据，非 tracker，保持只读）。
3. 更新活跃引用（6 处）：
   - harness/prompt_assembly.py（docstring）
   - tests/test_prompt_assembly_invariants.py（注释）
   - tests/test_context_round_truncation.py（docstring）
   - docs/adr/architecture/034-memory-compression.md（2 处）
   - docs/adr/architecture/053-current-task-round-truncation.md（1 处）
4. 已关闭记录（I-45、merge-requests/I-45.md）中的历史引用保持原样。

## 验收标准

- [ ] origin/main 不再有 issues-evidence/；docs/evidence/l3-03-grill-evidence.md 存在
- [ ] 活跃文件 grep issues-evidence 零命中（历史记录除外）
- [ ] repo_audit.py exit 0
- [ ] 无逻辑变更（仅注释/docstring/文档路径；harness 代码逻辑零 diff，测试通过性不变）

## 不在范围内

- 移动 issues/、merge-requests/（需同步改 pre-receive 策略，另案）
- 历史提交改写

## 关联

- 用户直接指示（2026-10-25）
- I-45（证据首次落盘）、ADR-034/ADR-053
