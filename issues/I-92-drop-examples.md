---
id: I-92
title: 删除 examples/ 与 scripts/install_education_skills.sh（与 kernel 无关）
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景 / What & Why

用户提出：examples/（llm_bash_roundtrip + PDF）与 scripts/install_education_skills.sh
与 harness 内核无关；examples 的能力已由 pi 应用层承接，因此删除，本地与云端同步。

## 范围

1. 删除 tracked 文件：
   - examples/llm_bash_roundtrip/{README.md,run.py}
   - examples/Domain adaptation via transfer component analysis.pdf
   - scripts/install_education_skills.sh
2. 保留 .gitignore 中 examples/.keys 等规则（本机 ignored key 文件保留，避免破坏
   本地真实 LLM 测试；不随仓库发布）。
3. 更新活跃引用：
   - tests/conftest.py 注释（examples/.keys 改为 local-only 说明）
   - docs/developer-sdk-contracts.md（删除 examples/<scenario> 指向）
   - .dsh/skills/secret-audit/SKILL.md（examples 例外改为 docs/ 与本地忽略文件）
   历史 issue/MR/archive 中的引用不改写。
4. 测试与审计；提交 MR。
5. 更新 GitHub agent-harness 最新快照（父提交 = f0b71eb，fast-forward），
   验证远端不含 examples/ 与该脚本。

## 验收标准

- [ ] git ls-files examples 为空；scripts/install_education_skills.sh 不再跟踪
- [ ] 活跃文件无指向 examples/ 或该脚本的引用（历史记录除外）
- [ ] pytest 通过；repo_audit WARN=0
- [ ] GitHub agent-harness 新快照无 examples/ 与脚本，且为 f0b71eb 的 fast-forward

## 不在范围内

- 删除本机 examples/.keys/auth（ignored 真实 key，按需另行处理）
- pi 仓库变更

## 关联

- 用户指示（2026-11-01）；I-91（拆分）
