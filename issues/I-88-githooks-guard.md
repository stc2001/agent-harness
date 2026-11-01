---
id: I-88
title: 本地 pre-push 护栏（.githooks 不入库）：阻止非本地远端推送全量历史
labels: [needs-triage]
author: lead
created: 2026-10-26
status: closed
---

## 背景 / What & Why

用户确认：只上传 latest 内容，不连历史；已有的 push 语义仍会把被推 ref 的全部可达历史发出去。
用本地 pre-push hook 做护栏：本地 soft-serve 正常推送放行；任何非本地远端只允许
「root 快照提交」（无父提交，通常是 commit-tree/archive 生成的 latest 快照），
并拒绝包含会话/密钥/虚拟环境目录的快照。

按用户要求：.githooks 不纳入 git（本地文件），仅 .gitignore 增加忽略规则。

## 范围

1. 本地创建 .githooks/pre-push（executable），git config core.hooksPath .githooks。
2. .gitignore 增加 .githooks/（唯一的 tracked 变更）。
3. hook 规则：
   - remote URL 为 localhost/127.0.0.1/::1/file:// 或本地路径 -> 放行（私有 soft-serve）；
   - 非本地远端：只允许无父的 root 提交（快照）；历史提交一律拒绝并提示用快照方式；
   - 非本地远端：快照树包含 sessions/、.soft-serve/、.venv/、.scratch/、diary/、
     node_modules/ 或 docs/harness_doc/deepseek_keys_for_test 时拒绝；
   - 紧急放行：ALLOW_PUBLIC_PUSH=1（带警告）。
4. 直接模拟 hook 调用做测试（localhost 放行 / github 全量拒绝 / snapshot 放行）。

## 验收标准

- [ ] git ls-files .githooks 为空；git status 不显示 .githooks
- [ ] 模拟 github + main 全量提交 -> exit 1，提示快照方式
- [ ] 模拟 github + root 快照提交 -> exit 0
- [ ] 模拟 localhost + main -> exit 0
- [ ] 本地 soft-serve push 不受影响

## 不在范围内

- 修改服务端 pre-receive 策略
- scripts/export-latest.sh（可另开 issue）
- 历史密钥清理（本地历史不发布即可）

## 关联

- 用户指示（2026-10-26）；push 语义讨论
