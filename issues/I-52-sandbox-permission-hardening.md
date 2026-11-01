---
id: I-52
title: 沙箱与文件权限的健壮性（fail-closed + 内核 guard）
labels: [needs-triage]
author: admin
created: 2026-10-19
status: closed
---

## 背景 / What & Why

本地实测发现 3 个真实缺陷，其中 1 个可**改写宿主机文件**：

1. `sandbox.execute()` 用 `text=True` 严格解码；日文输出触发 `0xe3` 解码错误，
   整次工具调用失败并破坏 tool_call 配对（引出后续 400）。
2. 文件权限判定 **fail-open**：未映射路径直接放行（带外宿主机路径被允许）；
   `..` 按字面前缀匹配 ⇒ `/ws/out/../src` 被当作可写。
3. `bash` 把所有路径一律按 **write** 检查 ⇒ 只读输入目录下 `cat`/`ls` 被拒。

真实越权证据（本地翻译工作区）：

```
默认共享目录 mode="rw" ⇒ echo PWN > /ws/out/../src/chunk_000.txt
   ⇒ 成功，且宿主机源文件被改写
改 mode="ro" 后 ⇒ 内核返回 "Read-only file system"，`..` 同样被挡
```

设计结论：**执行点 = 挂载 + guard（有界：路径集合）**；pre-call 门收敛为
**咨询性**（无界：命令形式）。Landlock 能挡住与命令形式无关的越权
（`python3 -c open(...,'w')`、变量展开、`cd`、`dd of=`）；`/dev` 必须进
rw 集合，否则 `2>/dev/null` 被误伤。

## 验收标准 / Acceptance criteria

- [ ] `pytest tests/test_sandbox_encoding.py -q` → 2 passed；非 UTF-8 输出不再使工具调用失败（bytes + errors=replace）
- [ ] **R1 回归**：`pytest tests/test_permission_path_denial.py -q` → 4 passed；经公开 `bash` / `sandbox_execute` handler，映射但未授权路径返回 `PermissionError`（非 `RecursionError`），合法只读读放行

- [ ] `pytest tests/test_access_boundary.py -q` → 14 passed, 1 skipped（含内核 e2e；skip 为 OH 无翻译脚本时的 mount 断言，见不在范围内）：未映射路径 **fail-closed**、`..` 规范化、只读目录不可写、fail-closed 对带外宿主机路径生效
- [ ] `pytest tests/test_exec_guard.py -q` → 14 passed：Landlock exec-guard 阻断与命令形式无关的越权（`python3 -c`、变量展开、`cd`、`dd of=`），并放行 `2>/dev/null`
- [ ] `pytest tests/test_builtin_tools.py -q` 通过；`bash` 在只读目录可执行 `cat`/`ls`，写操作仍被拒
- [ ] 源挂载声明 `mode: "ro"`（内核级只读，挡住 `..` 穿越写）
- [ ] pre-call 门删除命令分类启发式，收敛为「读优先」咨询性检查；执行点交给挂载 + guard
- [ ] 全量测试由 Lead 在真实工作区执行；本 MR 不引入新的失败类型

## 不在范围内 / Out of scope

- 本地翻译 app 脚本（`scripts/translate_longhorizon.py` 等）：OH 无对应路径，且该 app 已弃用（ADR-002）；本 MR 过滤相关改动
- `tests/test_workspace_boundary.py`：翻译专用工作区断言，依赖本地翻译目录布局；本 MR 过滤
- `tests/test_access_boundary.py::test_source_mount_is_kernel_read_only`：读取翻译脚本；OH 无脚本时 skip（harness 级 e2e 仍覆盖）
- 本地数据/路径/产物、`configs/llm.yaml`、部署脚本
- app 层记忆/装配改动（A/C/G 等其它 MR）

## 关联

- 相关 ADR：`docs/adr/architecture/051-policy-exec-stage-enforcement.md`
- 原始提交：`41fb9ec`、`1165b00`、`dd41567`、`78737a3`、`f3e60f0`
- 阻塞关系：`Blocked by: 无`（I-51 已合并）
