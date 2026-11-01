# Agent Harness — 未解决问题与当前缺口

> 本文档记录当前代码中存在的阻塞项、质量债务和功能缺口。

---

## P0 — 阻塞 MVP 闭合 / 生产安全

### P0-4. 缺少生产运维能力

> **2026-08-03 更新**：沙箱锚点生命周期排查后（`--die-with-parent` PDEATHSIG 并发误杀已修复），
> 明确健康检查机制的真实边界：**不需要锚点保活**（execute 每次自建 namespace，
> 不依赖锚点存活），需要的是 探活 + 崩溃恢复 + 孤儿清理 三件套。
> 详见下方「健康检查机制拆分」。
>
> **2026-08-19 更新（I-11）**：崩溃恢复地基已落地——`checkpoint(task=...)` task 块
> （start_event_id/agent_spec/prompt/max_iterations）+ `AgentRuntime._resume_events`
> （ADR-039 tool_called↔tool_result 幂等配对）+ `_current_task` 五元组记录。
> **剩余**：进程重启后的 rewind + 重放恢复入口（P0-4 收尾）。

| 能力 | 现状 | 影响 |
|------|------|------|
| **进程崩溃恢复** | 设计已定（ADR-039），**task 块地基已落地（I-11）**，重启恢复入口缺失 | 无法在生产环境持续运行 |
| **健康检查（探活）** | **模型服务已落地（I-14）**：`ModelServicePool` acquire 实时 `/health` 探活 + 30s 周期轮询 + unhealthy 标记；**沙箱仍无周期探活**（仅有 get_or_create 就绪探针 + I-12 步检查 is_alive） | 沙箱僵死无周期告警 |
| **孤儿清理** | ✅ 已实现（2026-08-03） | 长期运行累积泄漏 |
| **可观测性** | 无 metrics、tracing、结构化日志（I-21 OTel 待办） | 故障排查困难 |
| **资源隔离** | 无 cgroup 限制 | Agent 可耗尽宿主机资源 |
| **Docker 后端集成测试** | 存在但从未跑过 | 无法确认 Docker 后端可用性 |

### 健康检查机制拆分（2026-08-03 排查结论）

| 子项 | 回答的问题 | 当前状态 | 方案方向 |
|------|-----------|---------|---------|
| **探活（readiness/liveness）** | 沙箱当前是否可用 | get_or_create 已有就绪探针（execute 验证）+ I-12 步检查 is_alive | **模型服务周期探活已落地（I-14）**：acquire 实时探活 + 30s 轮询（`ModelServicePool`，镜像 ADR-040 §2 探针）；沙箱周期探活可复用同模式 |
| **崩溃恢复（任务级）** | 宿主崩溃后任务能否续跑 | 设计已定（ADR-039），task 块地基已落地（I-11），重启重放入口缺失 | checkpoint + 重放未完成事件 |
| **孤儿清理** | 宿主退出后残留的锚点树谁回收 | ✅ 已实现（2026-08-03）：`SandboxManager.reap_orphans()`，锚点带 `HARNESS_SANDBOX_ANCHOR=1` 标记，启动时清扫整树（含端到端测试） | APP 启动时调用 |

> **明确不做**：锚点保活（watchdog 自动重启锚点）。因为 execute 每次自建 namespace，
> 锚点死亡不影响沙箱可用性，自动重启是伪需求且可能误触发 workspace 重建。

### 崩溃恢复实现待办（源自 ADR-039）

**任务定义**：Task = (session_id, start_event_id, agent_spec, prompt, max_iterations)。
Session 是任务容器，不是任务本身。

**checkpoint task 块**（`SessionManager.checkpoint(task=...)` 已支持，I-11 落地）：

| 字段 | 说明 |
|------|------|
| `task.start_event_id` | 任务在事件流中的起点，恢复定位用 |
| `task.agent_spec` | 重建 AgentRuntime 用 |
| `task.prompt` | 重建上下文用 |
| `task.max_iterations` | 防止僵尸 loop |

**恢复重放算法**（幂等判定基于事件追溯链 parent_event_id）：

```
1. wake(session_id)                        # 从 events.jsonl 重建逻辑视图
2. 读 checkpoint task 块                   # start_event_id / agent_spec / prompt
3. 重建 AgentRuntime(spec)
4. 扫描 [start_event_id → 末尾]：
   - 已有 agent_completed/failed → 任务已结束，无需恢复
   - 否则找最后一个未完成 tool_called（无匹配 tool_result）
5. 从未完成点继续 agent_loop
```

**实现缺口**（2026-08-19 更新：checkpoint task 块字段已由 I-11 完成，见上方）：
- `_agent_loop` 支持从指定事件点继续（`_resume_events` 是事件过滤，缺 start_event_id 定位入口）
- 恢复入口 API（如 `session.resume()` 或 pool 层封装）
- 崩溃恢复的集成测试（子进程执行 → 崩溃 → 新进程恢复）

> **2026-09-07 更新（I-31 后）**：SessionManager 已无状态化——`wake` = store 读+逻辑视图、`checkpoint(session_id, task)` 现为日志级运算（落 `checkpoint_created` 事件）；恢复重放入口仍未实现，恢复时直接以 store 逻辑视图 + checkpoint 事件重建即可（原状态机不参与）。

---

## P1 — 质量与覆盖

### P1-8. Agent Spec 权限字段不完整

| 缺失字段 | 说明 |
|----------|------|
| `capabilities.session_write` | Validator 只读约束（CapToken 强制执行） |
| `version` | Spec 版本号（Phase 2 技能系统需要） |

### P1-11. 缺失测试用例 ✅ 2026-05-22 基本完成

| 模块 | 场景 | 状态 |
|------|------|:----:|
| agent-runtime | `max_iterations` 终止、`_sandbox_execute` 权限拒绝 | ✅ 已覆盖 |
| orchestrator | human_gate modify 决策、多 Phase job | ✅ 已覆盖（模块已删除） |
| SDK | `sessions.show()` / `rewind()`、`fork()` | ✅ 已覆盖 |
| CLI | `logs --follow`、`gate reject/modify`、`session rewind` | ✅ 已覆盖 |
| hashline | read + replace + insert + delete + 锚点验证 | ✅ 6 测试 |

## P2 — 死代码与清理（DAG 移除后续）

### P2-1. SDK _GatesClient 死代码 ✅ 已完成（2026-08-19，issue I-01/I-03）

- `sdk/client.py` 473/486/514/524 的 `_coordinator` 引用已删除，`list_pending`/`get` 改走 `self._client._store`
- `interfaces.py` 的 `DAGNode`/`Phase`/`JobSpec` 等 dataclass 已全部删除（I-03）
- 遗留：`GateInfo.phase_name` / `node_id` 字段保留（`list_pending` 仍构造，向后兼容；Phase/Node gate 语义已无生产者）

### P2-2. SDK 文档残留 ✅ 已完成（2026-08-19，issue I-01）

`sdk/client.py` 546/704、`sdk/__init__.py`、`tools/declare_output.py`、`tools/agent_mgmt.py` 的 Pipeline/coordinator/node_result 残留文档已全部清理。

### P2-3. SDK 完整人工审核 🔶 部分完成（2026-08-19）

`_GatesClient`（I-01 修复 + 测试）、`_MemoryClient`（I-01 estimate_tokens + I-05 compress system 解耦）已审核并测试。**2026-09-07 更新**：`_SessionsClient` 经 I-31 系统性重写并测试（show 新契约/holder/守卫/日志级 rewind/fork）；`_AgentsClient` 增 history_* 三查询（I-32，含 DRT）并复用测试覆盖。**仍待审核**：`_SandboxClient`、`_PromptsClient`（`_AgentsClient.status` 已加，I-10）。

### P2-4. `read_subagent` 工具未实现（保持待办）

APP 层主动拉取子 agent session 结果的工具。框架已提供 SessionStore 原语，APP 层通过工具依赖注入构建。I-01 仅更新了 `agent_mgmt.py` 的文档语义（node_result → read_subagent 拉取），工具本身未实现。

### P2-5. `test_integration.py` 残留 ✅ 已完成（2026-08-19，随 I-03 清理）

已确认无 coordinator/Pipeline 引用残留。

---

## 功能依赖链

```
MCP 工具 ← 增强 exec/run
```

---

## 建议执行顺序（2026-08-19 更新）

```
I-15  token 计量原语（HookContext tokens 字段已建 + backend/attempt 已带）   python-harness-issues.md
I-18/I-20/I-21  引擎能力 / A2A / OTel（OTel 依赖 I-15）                    python-harness-issues.md
P0-4  生产运维能力（崩溃恢复：重启重放入口 = I-11 之后剩余）                 ~200 行
P2-3  SDK 完整审核                       ~1000 行
P2-4  read_subagent 工具                 ~50 行
P1-8  Agent Spec 权限字段                ~15 行
```

> 2026-08-19 更新：I-11（wake + checkpoint task 块地基）、I-12（Stop 双轨）、I-13（模型配置+本地后端+回退）、
> I-14（模型服务池，探活能力在模型服务侧落地）、I-23/I-24（SDK 执行面）均已完成并移出 issues；
> P0-4 剩余为**进程重启重放入口 + 集成测试**；沙箱周期探活仍开放（可复用 I-14 轮询模式）。
