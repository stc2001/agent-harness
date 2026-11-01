---
id: I-86
title: 个人学习进度移出 docs/（保留本地，移至 diary/learning/）
labels: [needs-triage]
author: lead
created: 2026-10-25
status: closed
---

## 背景 / What & Why

用户指出：docs/learning/ 是个人的学习进度（学习计划、检索题库与个人作答/置信度、Mermaid 讲解图），
理论上不应放入 docs/（项目文档区）。它已在 git 跟踪中（3 个文件）。

处理原则：不删除内容，移出跟踪；本地文件保留到 diary/（已被 .gitignore 忽略的个人目录）。

## 范围

1. 物理移动 docs/learning/ -> diary/learning/（保留本地全部内容）。
2. git rm -r --cached docs/learning/，从索引移除（工作区文件已在 diary/learning/）。
3. 删除空的 docs/learning/ 目录。
4. 不做历史改写；不改动已关闭 issue/MR 中的历史引用。

## 验收标准

- [ ] git ls-files docs/learning 为空；origin/main 不再包含 docs/learning
- [ ] diary/learning/ 下 3 个文件仍在本地（README.md / RETRIEVAL.md / diagrams/m1-session-binding.md）
- [ ] git status 干净（diary/ 已被忽略）
- [ ] 无代码变更（harness/、pi/、tests/ 零 diff）

## 不在范围内

- 把学习内容迁到仓库外（如需可再移）
- 历史 git 记录清理
- 个人笔记的脱敏/发布

## 关联

- 用户直接指示（2026-10-25）
- I-84（此前引用过 docs/learning/README.md）
