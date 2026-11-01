---
id: I-99
title: 修复 CI：声明 transformers（本地回退 tokenizer 依赖）
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景

CI 设置 HARNESS_TOKENIZER_DIR 后，加载 fixture 需要 transformers；
本地 venv 已装，pyproject 未声明。

## 范围

- dev extra 增加 transformers>=4.40（CI 使用 .[dev]）。
- 新增 local extra（transformers）供本地回退 tokenizer 部署使用。
- 合并后 fast-forward 追加，跟踪 CI。

## 验收

- [ ] CI test (3.11)/(3.13) success
- [ ] repo_audit WARN=0

## 关联

I-95/I-96/I-97/I-98
