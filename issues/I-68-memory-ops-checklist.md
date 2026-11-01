---
id: I-68
title: 记忆压缩运维 checklist — usage 回落与旧 summary 迁移
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

I-63 把 true-release 迁入 `pi/memory` 后，需要一套可复制的运维判读方法：
真实长任务压缩后 usage 是否回落、旧格式（无 slot）summary 如何迁移。
本 issue 只交付文档/checklist，不改代码。

## 交付

新增 `docs/runbooks/memory-compaction.md`：

1. **I-57 观测**
   - 数据源：`PI_HOME/runs/<sid>/{run.json,meta.json,observe.csv,memory.jsonl}`
   - 字段与口径：`observe.csv.assembled_cost` 仅 content；`memory.jsonl.cost_before`
     才是完整 usage（content+reasoning+tool args）
   - 触发/回落判读：85% 高水位 → 一次 drop 到 25% → 下一步完整 usage 回落
   - 可复制命令：CSV 事件对比、jq、完整 usage 重算 snippet
   - 异常排查：compactions=0、尺寸守卫拒绝、slot 数量、tokenizer 守卫、
     投影/实际释放差异、`dropped_rounds` 兜底
2. **旧 summary 迁移**
   - 检测：`meta.ignored_legacy_summary_count` + legacy event 列举
   - 迁移：按 ADR-001 region 重新 `inject_summary(..., slot=...)`，
     prefix→before_task、suffix→after_task；旧事件留审计
   - **计数边界**：append-only 在线 API 下旧事件不会删除，新增 slot 摘要不会让
     `ignored_legacy_summary_count` 归零；验收应为「count 不增长 + 新 `<mem>` 装配」；
     物理归零需离线 store 迁移（破坏性、另行评审）
3. **链接**：ADR-001 §10 验证行、ADR-002 §3.6 指向 runbook

## 验收标准 / Acceptance criteria

- [ ] runbook 覆盖上述两部分，命令可复制、字段含义准确
- [ ] 明确 `assembled_cost` vs `cost_before` 口径差异，避免误判「未回落」
- [ ] 明确旧 summary「不装配≠删除」与 append-only 计数边界
- [ ] ADR-001 §10 / ADR-002 §3.6 链接指向 runbook
- [ ] docs-only：harness/pi/测试/translate/merge_TODO/部署物零 diff
- [ ] 全量测试数与 main 相同

## 关联

- 前置：I-57 R2、I-62、I-63
- 参考：ADR-001/002/053
