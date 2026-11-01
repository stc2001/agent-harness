---
id: I-96
title: 修复 CI：声明缺失的 httpx 依赖
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景

GitHub Actions 首次运行 test (3.11)/(3.13) 均失败于 collection：
harness/orchestration/model_pool.py 直接 import httpx，但 pyproject 未声明；
本地 venv 因传递依赖碰巧存在，CI 干净环境没有。

## 范围

- pyproject dependencies 增加 httpx>=0.27。
- 合并后正常 fast-forward 追加到 GitHub，触发 CI 复跑；跟踪结果。

## 验收

- [ ] CI test (3.11) / test (3.13) 均 success
- [ ] 本地 pytest / lint 不回归
- [ ] repo_audit WARN=0

## 关联

I-95（CI 工作流）
