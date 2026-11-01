---
id: I-69
title: sys_read_deny 的 /dev 回补实现（I-67 方案 A）
labels: [needs-triage]
author: admin
created: 2026-10-19
status: open
---

## 背景 / What & Why

ADR-051 的 2026-10-19 评估（I-67）结论：`fs_paths` 规则的 `sys_read_deny` 在当前
agent 未启用，因为 execguard 的 `BASE_RO` 含 `/dev`，`roex` 精确排除 `/dev` 后
`2>/dev/null` 会被 Landlock 拒绝（`FS_RO` 无 `write_file`）。

推荐方案 A：排除 `/dev` 的同时，显式把 `/dev/null`（必要时 `/dev/zero`、
`/dev/urandom`、`/dev/tty`）以 **rw** 回补到 allow 集；`/dev/mem` 等敏感节点仍被
deny。该实现依赖 Landlock 文件级 `PATH_BENEATH` 规则在目标内核/CI 可用（需实测；
不可用则回退 I-67 方案 C/wontfix）。

## 验收标准 / Acceptance criteria

- [ ] 启用 `fs_paths`/`sys_read_deny` 后：`bash -c '... 2>/dev/null'` 放行；
      `/dev/mem` 等敏感设备仍被内核拒绝（EACCES）
- [ ] 回补清单最小化并有论证（`/dev/tty` 单独评审）；不改变默认禁用状态
- [ ] kernel e2e 测试（`tests/test_exec_guard.py`）：允许/拒绝两侧各一条
- [ ] ADR-051 更新实现注记；若内核不支持文件级规则则回退方案 C 并记录
- [ ] 通过 SOP：issue → mr/I-69-* → R1/R2 评审 → 合并 → 审计

## 关联

- 评估：`issues/I-67-sys-read-deny-dev-conflict.md`、ADR-051（2026-10-19 评估段）
- 状态：**规划中，尚未排期**
