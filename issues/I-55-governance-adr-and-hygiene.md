---
id: I-55
title: 治理 — ADR-051 exec-guard 启用注记（E 组收敛后）
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

E 组原范围包含 ADR-051 修订与仓库卫生（本地脚本清理、`.gitignore`、merge_TODO
追踪文档、README/docs/skills 等）。维护者裁定：OH 只纳入 **`1cfea03` 对 ADR-051
的 exec-guard 启用注记**；其余提交只改本地脚本/本地追踪文档，OH 无对应路径或
不属共享契约，全部排除。

I-52 已给 ADR-051 追加过「pre-call 门收敛」注记；本 MR 追加的是**另一条**
（exec-guard 首次为应用层长程 agent 启用），不合并、不重复。

## 验收标准 / Acceptance criteria

- [ ] ADR-051 末尾追加独立注记「exec-guard 首次为应用层长程 agent 启用」，位于 I-52 的 pre-call 注记之后
- [ ] 注记含规则：rw `["/ws/out","/dev"]`、ro `["/ws/src"]`（历史 app 配置示例），并说明 `/dev` 必须 rw（否则 `2>/dev/null` 误伤）
- [ ] 注记说明挂载（`mode:"ro"`）与 guard 的分工/互补
- [ ] 注记记录 `fs_paths` 的 `sys_read_deny` 遗留冲突（会连带禁读 `/dev`）
- [ ] 注记不再把能力归属写成「翻译 agent」；改为应用层长程 agent，并以 **I-52 的 `tests/test_exec_guard.py`（14 项）** 作为能力验证
- [ ] 不修改 `tests/test_workspace_boundary.py`（翻译专用）；本 MR 无代码/测试变更
- [ ] `pytest tests/test_exec_guard.py -q` → 14 passed（I-52 已合并，作为该注记的验证引用）

## 不在范围内 / Out of scope

- `aebac3c`（本地脚本清理）、`0f46aff`（本地输出 .gitignore）
- `4fdfd7c` / `676b020` / `4c6143f`（merge_TODO 本地追踪文档）
- `4f67e1c` / `6f83d4d`（本地 .gitignore/docs/skills）
- 翻译专用 `tests/test_workspace_boundary.py` 改动
- 任何运行时代码或测试文件变更

## 关联

- 相关 ADR：`docs/adr/architecture/051-policy-exec-stage-enforcement.md`
- 原始提交：`1cfea03`（仅取 ADR-051 部分）
- 能力验证：I-52 `tests/test_exec_guard.py`
- 阻塞关系：`Blocked by: 无`（I-52 已合并）
