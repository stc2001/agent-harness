# 人类审核进度

> 本文件记录对 AI 生成代码的逐模块人类审核状态。
> 已审核 → 已审核但待修复 → 未审核

---

## harness 模块

| 模块 | 文件 | 状态 | 发现问题 | 已修复 |
|------|------|:---:|------|:---:|
| agent_runtime | `impl.py` | ✅ 已审核 | `tool_call_id` 冗余 → 统一用 `parent_event_id`；`_emit_event` 加 `event_id` 参数；未知工具 `allowed:True` 已修正；`_run_agent_loop` 重复 `agent_failed` 死代码；`agent_failed` 全部路径加 `parent_event_id=tc.id` | ✅ |
| agent_spec | `impl.py` | ⬜ 未审核 | — | — |
| coordinator | `impl.py` | ✅ 已删除 | ADR-038：DAG 编排移除 | — |
| gateway | `gateway.py` | ⬜ 未审核 | — | — |
| gateway | `hooks.py` | ⬜ 未审核 | — | — |
| gateway | `gate_manager.py` | ⬜ 未审核 | — | — |
| gateway | `rules.py` | ⬜ 未审核 | — | — |
| orchestrator | `impl.py` | ✅ 已删除 | ADR-038：DAG 编排移除 | — |
| orchestration/pool | `pool.py` | ✅ 已审核 | AgentRuntime 对象池缓存；沙箱复用委托 SandboxManager；职责边界清晰 | ✅ |
| orchestration/executor | `impl.py` | ✅ 已删除 | ADR-038：DAG 编排移除 | — |
| orchestration/scheduler | `impl.py` | ✅ 已删除 | ADR-038：DAG 编排移除 | — |
| sandbox | `impl.py` | ✅ 已审核 | `execute_timeout` 硬编码 60s → 已配置化；`__init__.py` 补依赖关系图 | ✅ |
| sandbox | `manager.py` | ✅ 已审核 | `if self._audit_log:` 空列表不记录 → `is not None`（5 模块 17 处）；模块层次文档化；TOCTOU 竞态：spawn 后等待进程就绪再 unlock | ✅ |
| sandbox | `backends.py` | ✅ 已审核 | 正确：薄工厂注册表；与 docker_impl 平行 | ✅ |
| session | `impl.py` | ✅ 已审核 | `rewind()`: 修复多次 rewind 事件复活 bug（迭代截断算法）；状态机简化（CREATED→RUNNING→COMPLETED/FAILED） | ✅ |
| session_store | `impl.py` | ✅ 已审核 | 简化：去掉 tmp 文件 → 单次原子 write+fsync；理由见 ADR-037 | ✅ |
| tools | `sandbox_exec.py` | ⬜ 未审核 | — | — |
| tools | `file_io.py` | ⬜ 未审核 | — | — |
| tools | `hashline.py` | ⬜ 未审核 | — | — |
| tools | `declare_output.py` | ⬜ 未审核 | — | — |
| tools | `agent_mgmt.py` | ⬜ 未审核 | — | — |
| tools | `install_skill.py` | ⬜ 未审核 | — | — |
| sdk | `client.py` | ⚠️ 部分审核 | `_GatesClient` 改直调 Gateway；`_MemoryClient.compress()` 重写为 pool.acquire + execute_task；`_JobsClient` 删除 | ✅ |
| sdk | `config.py` | ⬜ 未审核 | — | — |
| sdk | `models.py` | ⬜ 未审核 | — | — |
| sdk | `patterns.py` | ✅ 已删除 | ADR-038：Pipeline 移除 | — |
| prompt_assembly | `prompt_assembly.py` | ⬜ 未审核 | — | — |
| tool_registry | `tool_registry.py` | ⬜ 未审核 | — | — |
| skills | `registry.py` | ⬜ 未审核 | — | — |
| skills | `__init__.py` | ⬜ 未审核 | — | — |
| cli | `main.py` | ⚠️ 部分审核 | 删除 `run`/`status`/`cancel` 命令；`gate` 保持 approve/reject/modify | ✅ |
| interfaces | `interfaces.py` | ⬜ 未审核 | — | — |
| audit_log | `audit_log.py` | ⬜ 未审核 | — | — |
| agent_runtime | `__init__.py` | ⬜ 未审核 | — | — |

## app 层

| 文件 | 状态 | 发现问题 | 已修复 |
|------|:---:|------|:---:|
| ~~`hl_config.py` 等 app/ 全部~~ | ✅ 已删除（2026-09-06） | 旧 heuristic-learning 原型：API 过时、任务设计与 ADR-038 mission 编排定论相悖 | — |

> **2026-09-06**：遗留 `app/` 目录整体删除，本表审核项作废。

---

> 状态标记：✅ 已审核 / ⚠️ 已审核，待修复 / ⬜ 未审核
