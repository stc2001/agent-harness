# ADR-040：沙箱锚点生命周期——PDEATHSIG 并发误杀修复与孤儿清理约定

## Status

Accepted (2026-08-04)

## Context

`spawn()` 的锚点 no-op 容器（`sleep infinity`）最初带 `--die-with-parent` 和 `--new-session` 两个 bwrap 标志。并发场景下（4 线程 `get_or_create`）偶发 **spawn_count=2**：第一个锚点被判定死亡，触发 respawn 双 spawn。

排查（详见 `docs/technical-report-parallel-toctou.md`）发现根因在 `--die-with-parent` 的 PDEATHSIG 语义，而非 TOCTOU 等待窗口。

### 锚点进程树的真实结构

```
锚点主进程  (bwrap, 宿主 pidns, Popen 返回的 pid)
└─ bwrap 子进程  (进入新 pidns，PID 1)
   └─ sleep infinity  (空转容器)
```

`--unshare-all` 的 `--unshare-pid` 对**当前进程不生效**（`unshare(CLONE_NEWPID)` 只影响后续 fork 的子进程），因此 Popen 返回的主进程留在宿主 pidns，fork 出的子进程才进入隔离 pidns。

### 根因：PDEATHSIG 并发误杀

bwrap 的 `--die-with-parent` 用 `prctl(PR_SET_PDEATHSIG)`，语义是"**bwrap 或 bwrap 的父进程死亡时，级联 SIGKILL 整棵树**"（bwrap man page）。

2×2 对照实验（锚点 DWP × 探针 DWP）确认：

| 锚点 DWP | 探针 DWP | 锚点存活率 |
|:---:|:---:|:---:|
| ✓ | ✓ | 1/10 |
| ✓ | ✗ | 3/10 |
| ✗ | ✓ | **10/10** |
| ✗ | ✗ | 10/10 |

**锚点自身带 `--die-with-parent` 是并发死亡唯一元凶**。锚点主进程在宿主 pidns，并发环境下 PDEATHSIG 被误触发 → SIGKILL(9) → bwrap 级联语义连根拔起整树 → 后续线程 `is_alive=False` → respawn 双 spawn。

## Decision

### 1. 锚点容器去掉 `--die-with-parent` 和 `--new-session`

`_build_bwrap_cmd(anchor_container=True)`：

- **去掉 `--die-with-parent`**：消除 PDEATHSIG 并发误杀
- **去掉 `--new-session`**：bwrap 子进程不再独立成组，整棵树保持单一进程组（pgid=锚点 pid）
- **保留 Popen 的 `start_new_session=True`**：作为进程组锚点，`killpg()` 一次回收整树

`execute()` 命令**保留**两个标志（每命令独立会话 + 父进程退出清理）。

### 2. 就绪探针（readiness probe）

`get_or_create()` spawn 后执行 `execute("echo ready")` 探针，成功才注册。探针验证的是"沙箱可执行命令"而非"进程存活"——因为 `is_alive()`（`proc.poll()`）无法区分"已 fork 未完成 setup"和"稳定运行"。

### 3. 孤儿进程需显式清理（重要约定）

去掉 `--die-with-parent` 的**代价**：宿主进程退出/崩溃后，锚点树**不会被自动清理**（无 PDEATHSIG 兜底）。

**因此：孤儿清理是显式操作，不是自动行为。**

| 场景 | 清理方式 | 机制 |
|------|---------|------|
| 正常 destroy / destroy_all | `killpg()` | 整树同组，自动回收 |
| 宿主退出后残留 | **必须显式调用** `SandboxManager.reap_orphans()` | 扫描 `/proc` 带 `HARNESS_SANDBOX_ANCHOR=1` 标记的进程，按 pgid 定位整树根，孤儿判定（父进程 ≠ 当前 harness）后 killpg |

**约定**：
- APP 层应在**启动时**调用 `reap_orphans()`（在创建新沙箱之前）
- 孤儿清扫依赖锚点 env 标记，execute 命令**不带**该标记（短命进程，非锚点）
- bwrap `--setenv` 只注入沙箱内层进程，锚点根进程 environ 无标记 → 清扫必须**按 pgid 定位整树根**，而非直接 kill 带标记进程
- 孤儿被 reparent 到 systemd --user（ppid=1166）而非 init（ppid=1）→ 孤儿判定不能只认 ppid==1，改用"父进程 ≠ 当前 harness 进程"

### 4. 接口契约补全

`SandboxInterface` 协议补 `execute()` 声明（两个真实 backend 本就有，是历史欠账；探针依赖它，协议必须声明）。

### 5. I-26 对齐注记（2026-09-04）

沙箱身份在**管理器层统一为 sandbox_id（uuid）**：SandboxInterface 按 sandbox_id 寻址，OS 句柄（锚点 PID / 容器 id）绝不跨出后端（后端内部 `sandbox_id → OS id` 私有映射）。

- **实体**：SandboxEntity(sandbox_id, config_name, backend)；同配置可多实体（uuid 互异）；respawn 在管理器内以**同一 sandbox_id 覆盖式**重 spawn（实体逻辑身份稳定——锚点可换、身份不变，本 ADR「execute 自建 namespace、锚点死亡不影响可用性」语义在实体层的延续）。
- **reap_orphans 不变**：宿主级按 pgid + env 标记清扫，与实体 uuid 无关。
- **TTL/idle reaper**：后端内部按 OS id 回收；`is_alive(sandbox_id)` 转 false → manager respawn 同 uuid。

## Consequences

- **正面**：并发双 spawn 消除（380 测试通过，原 flake 测试连续 5 次通过，耗时从秒级降至 0.18s）
- **正面**：整树同进程组，正常销毁零泄漏
- **代价**：宿主崩溃后锚点残留，必须依赖**显式** `reap_orphans()`（启动清扫）
- **明确不做**：锚点保活（watchdog 自动重启）。execute 每次自建 namespace，锚点死亡不影响沙箱可用性，自动重启是伪需求且可能误触发 workspace 重建

## 参考

- ADR-030：sandbox spawn no-op 容器（本 ADR 修正其"同一挂载视图"表述与 superseded 误标）
- ADR-039：任务边界与崩溃恢复（孤儿清理是健康机制第三件套之一）
- `docs/technical-report-parallel-toctou.md`：完整排查过程
