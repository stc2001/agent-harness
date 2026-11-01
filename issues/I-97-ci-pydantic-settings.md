---
id: I-97
title: 修复 CI：声明缺失的 pydantic-settings 依赖
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景

CI 第二次运行仍失败：harness/sdk/config.py 直接 import pydantic_settings，
但 pyproject 未声明（本地 venv 有传递/手动安装）。I-96 已补 httpx，本次补
pydantic-settings。

## 范围

- pyproject dependencies 增加 pydantic-settings>=2.0。
- 合并后 fast-forward 追加并跟踪 CI。

## 验收

- [ ] CI test (3.11)/(3.13) success（或暴露下一处真实失败并继续修）
- [ ] repo_audit WARN=0

## 关联

I-95（CI 工作流）、I-96（httpx）
