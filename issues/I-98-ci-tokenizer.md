---
id: I-98
title: 修复 CI：提供 hermetic tokenizer fixture（避免依赖本机 tokenizer_dir）
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景

CI 第三次运行失败：大量测试依赖 tokenizer_dir/deepseek 回退 tokenizer；
该目录是本机真实 model 文件（未跟踪、不进仓库），CI 干净环境没有。

本地以 pi 仓库同一份最小 byte-level fixture 验证：相关 41 个测试通过。

## 范围

- 将 tests/fixtures/tokenizer/tokenizer_dir/deepseek/tokenizer.json（4.9KB）纳入仓库。
- ci.yml test job 设置 HARNESS_TOKENIZER_DIR 指向该 fixture 根目录。
- .gitignore 增加 .coverage（pytest-cov 产物）。
- docs/runbooks/ci-cd.md 记录 hermetic tokenizer 说明。
- 合并后 fast-forward 追加，跟踪 CI。

## 验收

- [ ] CI test (3.11)/(3.13) success
- [ ] 本地 pytest 不回归
- [ ] repo_audit WARN=0

## 关联

I-95/I-96/I-97
