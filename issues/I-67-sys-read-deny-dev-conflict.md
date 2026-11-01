---
id: I-67
title: sys_read_deny 与 /dev 冲突 — 设计评估
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

`fs_paths` preset 的 `sys_read_deny` 会从 execguard 自动 RO 集排除 `/dev`，
从而打断 `2>/dev/null`（ADR-051 遗留）。本 issue 只做设计评估与文档更新，
不写行为代码。

## 机制核实

- `execguard.c`：`BASE_RO` 含 `/dev`；`roex <path>` 只跳过**完全同名**的 base
  路径，随后显式 `ro/rw PATH` 仍 `landlock_add_rule`。
- 因此「排除 `/dev` + 显式回补 `/dev/null`」机制上可行，前提是 Landlock ABI 支持
  文件级 `PATH_BENEATH`；需目标内核/CI 实测，失败则回退 C。
- `FS_RO` 不含 `write_file`，故 `2>/dev/null` 的回补必须 **rw**，不能只 ro。

## 三选项与取舍

| 选项 | 做法 | 优点 | 风险/成本 |
|---|---|---|---|
| **A（推荐）** | 排除 `/dev`，显式回补 `/dev/null`（必要时 `/dev/zero`、`/dev/urandom`、`/dev/tty`）到 **rw** | 保留 fs_paths 内核 deny；只开放安全设备；修复重定向 | 依赖文件级 Landlock 支持；需最小实现与内核 e2e |
| B | `KERNEL_DENY` 去掉 `/dev` | 自动 RO `/dev` 恢复 | `2>/dev/null` 仍只读失败；需 app 声明 `/dev` rw，等于部分放弃 deny |
| C | wontfix，保持禁用 | 零风险、pi 不受影响 | 文档化风险；fs_paths 内核覆盖不可用 |

**推荐 A**；本 issue 不实现，实现/内核验证另开 **I-69**。若目标内核不支持文件规则，
回退 C 并在 ADR 记录。

## 验收标准 / Acceptance criteria

- [ ] ADR-051 遗留段更新为 2026-10-19 评估：机制、A/B/C、推荐 A、理由、I-69
- [ ] 不改 `harness/`（零 diff）；不启用 `fs_paths`/`sys_read_deny` 默认行为
- [ ] translate/merge_TODO/部署物零 diff
- [ ] 全量测试数不变（docs-only）
- [ ] 链接/编号正确（I-67 → I-69）

## 关联

- 前置：ADR-051、I-52（execguard 能力验证）、I-55、I-61
- 后续实现：I-69（待开）
