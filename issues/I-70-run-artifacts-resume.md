---
id: I-70
title: pi run 产物在 resume 时被覆盖 — per-run vs 追加
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

I-68 runbook 复核时发现：`runs/<sid>/` 的产物实际是 **per-run**，与 ADR-002 §3.6
「每次压缩追加 / 每步观测追加」不一致，`--resume` 后运维判读会失真：

- `PiRuntime.run` 每次重置 `_rows = []`，`write_observe` 以 `"w"` 覆盖 `observe.csv`；
- `pi/artifacts.py::append_memory`：本次有压缩时以 `"w"` 截断，仅写新记录；
  本次无压缩时只 `touch()`，旧 `memory.jsonl` 保留（stale）；
- `--resume`/新进程创建新 `PiRuntime`/`PiMemory`，`compactions` 从空开始。

后果：resume 后 `observe.csv` 只剩当前 run；`memory.jsonl` 要么丢失历史、
要么保留旧文件造成误读。跨 resume 的权威压缩历史目前只能读
`sessions.events.logical(sid)` 的 `memory_summary`（append-only）。

## 选定语义（I-70 实施）

**选 (b) per-run 分段**：`runs/<sid>/<run_id>/`，每次 `PiRuntime.run` 生成唯一
run_id；`run_id` 写入 `meta.json`/`run.json`。`--resume`/新进程产生新段，旧段
保留、不覆盖。理由：auditability 最好；避免给 observe.csv 改 schema/合并旧行；
与「每次 run」天然对齐。旧扁平 `runs/<sid>/{run.json,meta.json,observe.csv,memory.jsonl}`
保留为 **legacy 只读**（`read_meta` 回退），不自动迁移。

## 验收标准 / Acceptance criteria

- [x] 选定 (b) per-run 分段并实现；旧扁平产物只读兼容
- [ ] 同一 session 连续两次 run（resume）→ 两段俱在，旧段 run/memory 字节不变
- [ ] 无压缩 run 不再出现 touch 保留 stale：每段新建自己的 memory.jsonl
- [ ] resume 回归测试：旧 compactions/observe 按约定保留（或按 run 分段），
      不再出现「有压缩截断/无压缩 stale」的不一致
- [ ] 文档：ADR-002 §3.6 更新（与 244678f/runbook 一致）；`docs/runbooks/memory-compaction.md`
      §0.1 同步新语义
- [ ] 不改 harness；translate/merge_TODO/部署物零 diff
- [ ] OH 全量绿（6 项既有 401 除外）

## 关联

- 发现于：I-68 R1（runbook §0.1 真实边界）
- 相关：ADR-002 §3.6、I-63（PiMemory true-release）
