---
id: I-59
title: 基线补齐 — 对齐本地 6eddb16 前置（ADR-054/055 + runtime/hooks/session_id）
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

本地工作区与 OH_workspace 历史不同源：OH main (`652fed0`) 不含本地基线提交
`60bf33a..6eddb16`，因而缺少 A–H（I-51..I-58）补丁依赖的前置：

- ADR-054（AgentSpec thinking 三态 / `resolve_spec_model`）
- ADR-055（LLM driver 层：`harness/llm_driver.py`、`client.llm`）
- `session_id` 注入 hooks（`agent.pre_step` 等）
- runtime `finish_reason=length` 的保留/续跑与无进展 fail-loudly

实测直接应用 A 组补丁会得到「OH 旧 impl + 新 driver」的混合状态，测试不可信。

## 验收标准 / Acceptance criteria

- [ ] OH main 分支合并后存在 `docs/adr/architecture/054-*.md`、`055-*.md`、`harness/llm_driver.py`、`tests/test_llm_driver.py`
- [ ] `harness/agent_runtime/impl.py` 含 `resolve_spec_model` 与 driver 调用；`harness/hooks/context.py` 暴露 `session_id`
- [ ] `harness/`、`tests/`、`docs/adr/` 与本地 `6eddb16` 对应文件一致（OH 既有 I-47..I-50 tokenizer 行为保留）
- [ ] `harness/config/sandbox.yaml` 保持 OH 部署路径（`/home/gexingdeyun/OH_workspace → /workspace`、`/home/gexingdeyun/outputs → /outputs`）
- [ ] MR 不包含 `harness/config/models.yaml`（OH 未跟踪部署物）与 `configs/llm.yaml`（真实密钥）
- [ ] MR 不新增硬编码 `/mnt/disk2/ds_workspace_infer_engine` 的脚本；本地部署脚本移出本 MR
- [ ] 全量测试由 Lead 在真实工作区执行；本 MR 不做权威回归判定

## 不在范围内 / Out of scope

- A–H 业务改动（I-51..I-58；依赖本 MR 合并后另开分支）
- 本地部署脚本（`run_harness.sh`、`serve.sh`、`start_server.sh` 等）与部署配置
- 数据、运行产物与实验文档副本

## 关联

- 原始提交：`60bf33a`、`e4a9d57`、`c80d2a3`、`c5975b9`、`6eddb16`
- 阻塞关系：`Blocks: I-51..I-58`；`Blocked by: 无`
