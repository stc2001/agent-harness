# 技术报告：并行 TOCTOU 问题排查全过程

> **项目**：Agent Harness 沙箱系统
> **日期**：2026-08-03 ~ 2026-08-04
> **问题**：`test_get_or_create_thread_safe` 并发 flake——并发 `get_or_create` 应只 spawn 一次，偶发 spawn 两次
> **根因**：bwrap 的 `--die-with-parent`（PDEATHSIG）在并发环境下误杀锚点进程
> **状态**：已修复，380 测试通过，附通用 SOP 与 skill

---

## 1. 问题现象

### 1.1 表面症状

```
tests/test_sandbox.py::TestSandboxManager::test_get_or_create_thread_safe
FAILED: Expected 1 spawn, got 2
```

4 个线程并发 `manager.get_or_create("test")`，期望只 spawn 一个沙箱，实际偶发 spawn 两个。

### 1.2 关键迷惑点

- **单独跑永远通过**，全量回归时偶发失败 → 依赖真实时序，无法稳定复现
- 失败时第二个 spawn 来自 manager 的 **respawn 分支**（`is_alive=False` → kill → 重新 spawn）
- 即：第一个锚点被判定"死亡"，触发了误判 respawn

### 1.3 初始假设（错误）

沿用了 commit `36b3e76` 的修复思路——"TOCTOU 竞态"：spawn 后进程未就绪，其他线程看到 `is_alive=False` 而 respawn。假设"等待窗口太短"，尝试加长等待。

---

## 2. 排查方法论总览

排查遵循 **"先确定性命中，再变量隔离，最后机制验证"** 三层递进：

```
Layer 1  确定性复现    真实时序无法复现 → 构造 stub 确定性触发
Layer 2  变量隔离      逐步剥离无关变量，用对照实验定位触发条件
Layer 3  机制验证      从内核/进程树层面确认根因，而非停留在现象
```

每一层都用**三元组**记录：**(事件 → 目标 → 对象)**。

---

## 3. 失败路径（被证伪的假设）

### 路径 F1：加长等待窗口（TOCTOU 直觉）

| 事件 | 目标 | 对象 |
|------|------|------|
| 并发下锚点未就绪被判定死亡 | 用更长等待窗口覆盖慢启动 | `manager.py` 的 `for _ in range(10)` 循环 |

**操作**：把 100ms 固定窗口改为阻塞等待直到 `is_alive`（带 5s deadline），超时抛 `SandboxSpawnError`。

**结果**：❌ 确定性 stub 测试通过，但真实并发下 **spawn_count=2 依旧**。等待窗口不是根因。

**教训**：修"等待时长"是修现象。如果问题与等待无关，加多长的等待都无效。

### 路径 F2：稳定窗口探测

| 事件 | 目标 | 对象 |
|------|------|------|
| 单次 `is_alive` 探测可能误判（进程已 fork 未 exec） | 连续 N 次 alive 确认稳定 | `_SPAWN_STABLE_PROBES` 常量 |

**操作**：连续 3 次 `is_alive=True` 才注册，中途 False 重置计数。

**结果**：❌ 被用户否决——魔法数字，且只是降低概率不是消除。

**教训**：用"多次探测"掩盖不确定，是治标。真正的机制问题不会因为多测几次就消失。

### 路径 F3：锚点不带 `--die-with-parent` 后直接丢弃（半修复）

| 事件 | 目标 | 对象 |
|------|------|------|
| 去掉 DWP 后并发 20/20 存活 | 验证 DWP 是元凶 | `_build_bwrap_cmd` |

**操作**：全局去掉 `--die-with-parent`。

**结果**：⚠️ 并发 bug 消失（20/20 存活），但引入**新问题**：bwrap 子进程因 `--new-session` 独立成组，`killpg` 只杀主进程 → **孤儿进程泄漏 10/10**。

**教训**：修复 A 却破坏 B，说明修复不完整。去掉 DWP 后必须同时处理进程组清理。

---

## 4. 成功路径（逐步定位根因）

### 路径 S1：确定性复现（stub 隔离真实时序）

| 事件 | 目标 | 对象 |
|------|------|------|
| 真实 bwrap 时序无法稳定复现 | 构造可重复触发的测试 | `SlowReadySandbox` stub backend |

**操作**：stub 的 `is_alive` 前 N 次返回 False（模拟慢启动），并发 4 线程必然触发 respawn。

**结果**：✅ 确定性复现 spawn_count=2。证明问题是**逻辑性**的，不是偶发时序噪声。

**关键认知**：能确定性复现，说明存在明确的触发条件；不能，才是真 flake。

### 路径 S2：变量隔离（stub 抽象层）

| 事件 | 目标 | 对象 |
|------|------|------|
| 探针方案（execute 验证）后真实并发仍失败 | 隔离"探针"是否是变量 | `execute` 探针调用 |

**操作**：真探针（execute 真实执行）vs 假探针（立即返回成功）：

| 探针类型 | 锚点存活率 |
|---------|:---:|
| 真探针（启动 bwrap） | 1/10 |
| sleep 0.05s | 4/10 |
| 空探针（立即返回） | 10/10 |

**结果**：✅ 探针耗时越长越容易死 → 不是探针内容，是**时间窗口**内发生了别的事。

**关键认知**：存活率与探针时长正相关，说明锚点死亡是**概率性异步事件**，与探针本身无关。

### 路径 S3：进程树观测（从现象到机制）

| 事件 | 目标 | 对象 |
|------|------|------|
| 锚点被 SIGKILL(9)，整树连根拔起 | 弄清"谁杀了锚点" | 锚点 bwrap 进程树 |

**操作**：监控 `/proc` 进程树 + SIGCHLD handler 抓退出信号。

**发现**：
```
锚点主进程 (宿主 pidns)  wchan=do_sys_poll
└─ bwrap 子进程 (新 pidns, PID 1)
   └─ sleep infinity
```
锚点被 **SIGKILL(9)**，整个树消失，且**无任何 kill 调用**（已用 `os.killpg` 追踪确认）。

**关键认知**：非 killpg 的 SIGKILL → 只有内核能发 → **PID namespace 崩溃** 或 **PDEATHSIG**。

### 路径 S4：四象限对照（决定性实验）

| 事件 | 目标 | 对象 |
|------|------|------|
| 需要确定"锚点 DWP"和"探针 DWP"谁是元凶 | 2×2 对照实验 | `--die-with-parent` 标志 |

**操作**：锚点/探针各自带不带 DWP 的四种组合：

| 锚点 DWP | 探针 DWP | 锚点存活率 |
|:---:|:---:|:---:|
| ✓ | ✓ | 1/10 |
| ✓ | ✗ | 3/10 |
| ✗ | ✓ | **10/10** |
| ✗ | ✗ | 10/10 |

**结果**：✅ 决定性——**锚点自身的 `--die-with-parent` 是并发死亡唯一元凶**，探针带不带无关。

**关键认知**：bwrap 的 `--die-with-parent` 用 `prctl(PR_SET_PDEATHSIG)`，语义是"**bwrap 或 bwrap 的父进程死亡时，级联 SIGKILL 整棵树**"。锚点主进程在宿主 PID namespace，并发下 PDEATHSIG 被误触发 → 锚点被 SIGKILL → 级联杀全树。

### 路径 S5：完整修复（DWP 移除 + 进程组统一）

| 事件 | 目标 | 对象 |
|------|------|------|
| 去掉 DWP 后并发修复但泄漏 | 同时解决并发误杀和孤儿泄漏 | `_build_bwrap_cmd` + kill 路径 |

**操作**：
1. 锚点容器去掉 `--die-with-parent` + `--new-session`（`anchor_container=True` 参数）
2. 保留 Popen 的 `start_new_session=True` 作为进程组锚点
3. execute 命令保留两个 flag（会话隔离 + 清理语义）

**结果**：✅ 并发 20/20 存活 + 零泄漏（整树同进程组，killpg 一次回收）。

**关键认知**：
- `--new-session` 让 bwrap 子进程独立成组 → killpg 打不到 → 泄漏
- 去掉后整树同组（pgid=锚点 pid）→ killpg 完整回收
- **"并发正确性"和"资源清理"必须一起设计**，不能只修一半

### 路径 S6：接口契约补全

| 事件 | 目标 | 对象 |
|------|------|------|
| 探针依赖 `execute`，但协议未声明 | 补全 `SandboxInterface` 协议 | `interfaces.py` |

**操作**：协议加 `execute()` 声明（真实 backend 本就有，是历史欠账）；red team stub 补 `execute`。

**结果**：✅ 380 测试通过。

### 路径 S7：孤儿清扫（健康机制第三件套）

| 事件 | 目标 | 对象 |
|------|------|------|
| 宿主退出后锚点树残留 | 启动时回收孤儿 | `SandboxManager.reap_orphans()` |

**操作**：锚点注入 `HARNESS_SANDBOX_ANCHOR=1` 标记 → 扫描 `/proc` → 按 pgid 定位整树根 → 孤儿判定（父进程 ≠ 当前 harness）→ killpg。

**端到端发现**：
- bwrap `--setenv` 只注入沙箱内层，锚点根进程无标记 → 必须按 **pgid** 定位整树根
- 孤儿被 reparent 到 systemd --user（ppid=1166）而非 init（ppid=1）→ 判定不能只认 ppid==1

**结果**：✅ 含端到端测试，宿主崩溃后残留可回收。

---

## 5. 根因总结

```
并发 4 线程 get_or_create
  → 锚点 bwrap 主进程带 --die-with-parent (PDEATHSIG)
  → 并发环境下 PDEATHSIG 被误触发
  → 锚点被 SIGKILL(9)
  → 整个进程树连根拔起 (bwrap 级联语义)
  → 后续线程 is_alive=False → respawn → spawn_count=2
```

**三层真相**：

| 层 | 真相 |
|----|------|
| 现象 | 并发 get_or_create 双 spawn |
| 机制 | PDEATHSIG 并发误杀锚点 → respawn |
| 根因 | `--die-with-parent` 与 Python 多线程 fork 的交互，锚点被误判死亡 |

**排除的假设**（三层递进中每一层都被实验推翻）：
- TOCTOU 等待窗口太短（F1）
- 单次探测不稳（F2）
- 探针启动 bwrap 干扰（S2 证伪）
- 孤儿进程干扰（实验清理后依旧）

---

## 6. 修复方案与验证

### 6.1 代码改动

| 文件 | 改动 |
|------|------|
| `harness/sandbox/impl.py` | `anchor_container` 参数；锚点去 DWP + `--new-session`；锚点注入 env 标记；孤儿清扫模块 |
| `harness/sandbox/manager.py` | 就绪探针；`reap_orphans()` |
| `harness/interfaces.py` | 协议补 `execute()` |
| `tests/test_sandbox.py` | 探针测试、flag 契约测试、孤儿清扫测试（含端到端） |
| `tests/test_gateway_red_team.py` | stub 补 execute |

### 6.2 验证结果

- 全仓回归：**380 passed / 0 failed**
- 原 flaky 并发测试：连续 5 次通过，耗时从秒级降到 0.18s
- 孤儿清扫端到端：子进程退出 → 锚点残留 → 启动清扫 → 整树回收
- 零进程泄漏

### 6.3 架构决策沉淀

| ADR | 内容 |
|-----|------|
| ADR-030（修正） | 移除错误的 superseded 标注；更正"同一挂载视图"为"每次自建 namespace" |
| ADR-040（新增） | 锚点生命周期：PDEATHSIG 并发误杀根因、`anchor_container` 决策、孤儿进程需显式 `reap_orphans()` 的约定 |
| ADR-039（新增） | 任务边界与崩溃恢复（孤儿清理为健康机制第三件套） |

---

## 7. 方法论沉淀

本报告的通用价值不在"PDEATHSIG 这个具体 bug"，而在排查路径本身。四条核心原则：

1. **确定性优先**：真实时序无法复现时，用 stub 构造确定性触发。先证明"能复现"，再谈修复
2. **变量隔离**：一次只动一个变量，用对照实验（尤其 2×2 四象限）定位触发条件
3. **现象→机制→根因三层剥离**：不停留在"加等待窗口"这类现象层修复，用进程树/信号/内核行为验证机制
4. **修复完整闭环**：修 A 必须检查是否破坏 B（本例 DWP 移除 → 泄漏）。"并发正确性"与"资源清理"一体设计

---

## 8. 附件

- **附录 A**：[并发问题排查通用 SOP](./sop/concurrent-bug-investigation-sop.md)
- **附录 B**：SOP 已固化为 skill（`concurrent-bug-investigation`）
