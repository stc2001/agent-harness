---
id: I-94
title: README 添加 DeepWiki badge（agent-harness）
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景 / What & Why

用户要求在项目 README 添加 DeepWiki 徽章，指向
https://deepwiki.com/stc2001/agent-harness 。

## 范围

- 在 README.md 标题下方添加：
  [![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/stc2001/agent-harness)
- 合并后用 scripts/publish_github.sh 正常 fast-forward 追加到 GitHub。

## 验收标准

- [ ] README 首屏含 DeepWiki 徽章与正确链接
- [ ] GitHub agent-harness main 通过追加提交更新（无 force）
- [ ] repo_audit WARN=0

## 关联

- 用户指示（2026-11-01）；I-93（GitHub 追加发布）
