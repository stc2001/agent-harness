---
id: I-100
title: CI 硬化：补 seccomp/cedarpy 依赖并排除非 hermetic 测试
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景

CI 在 tokenizer 修复后仍有 23 个失败：
- mephi42-seccomp 未声明（test_i34_execguard、policy_cedar guard）；
- cedarpy 未声明（test_policy_cedar）；
- 真实 LLM 测试在 CI 用无效 key（401）；
- test_sandbox 网络用例在 CI runner 不可用（已知 flake）。

## 范围

- dev deps 增加 cedarpy>=4.8.7、mephi42-seccomp>=2.5.6。
- ci test 命令 deselect：真实 LLM 用例、host network 用例。
- docs/runbooks/ci-cd.md 更新非 hermetic 清单。
- 合并后 fast-forward 追加并跟踪 CI。

## 验收

- [ ] CI test (3.11)/(3.13) success
- [ ] repo_audit WARN=0

## 关联

I-95~I-99
