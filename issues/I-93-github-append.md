---
id: I-93
title: GitHub 发布改为正常追加（fast-forward）：发布脚本 + 本地护栏调整
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景 / What & Why

用户提醒：日后 GitHub 云端只做正常追加更新，不需要删除全部内容再重新 push。
此前为建立首个快照用过一次 force-with-lease；从现在起 GitHub main 在已有快照之上
fast-forward 追加，不再 force。

本地历史（739+ 提交、含旧 key）仍不发布；做法是「以 GitHub 当前 main 为父提交，
用本地 HEAD 的 tree 生成一个同步提交」，fast-forward 推送。这样云端从首个快照起
正常累积提交，且不泄露本地历史。

## 范围

1. 新增 scripts/publish_github.sh：
   - fetch GitHub main tip；
   - 若 tree 未变则跳过；
   - 以远端 tip 为父、本地 HEAD tree 为内容创建同步提交；
   - fast-forward 推送，禁止 force；
   - 使用 ssh.github.com:443 + ~/.ssh/github_ed25519（绕过加速器对 github.com:22 的干扰）。
2. 新增 docs/runbooks/github-publish.md：发布流程、首次快照与后续追加的区别、禁止 force。
3. 本地 .githooks/pre-push（不入库）调整：非本地远端允许「首个 release 快照 + 其上的追加链」
   （提交数上限 200），继续拒绝本地全量历史与含敏感路径的快照。
4. 用新脚本把本次变更追加到 GitHub（父提交 1a16f6b），验证 fast-forward。

## 验收标准

- [ ] scripts/publish_github.sh 可执行；dry-run/no-change 行为正确
- [ ] GitHub main 新提交的 parent = 1a16f6b，提交数 2（非 force）
- [ ] 本地 pre-push 模拟：全量历史拒绝、快照 root 允许、追加链允许、含 sessions 的快照拒绝
- [ ] repo_audit WARN=0

## 不在范围内

- 改写本地 soft-serve 历史
- 把 739 条历史逐条迁到 GitHub

## 关联

- 用户提醒（2026-11-01）；I-91/I-92；当前远端 main=1a16f6b
