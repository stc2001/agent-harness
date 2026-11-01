# HANDOFF — python harness 扩展工作交接（2026-08-19 更新）

> 给下一个会话：从 **I-15（token 计量原语）** 继续实现 `issues/python-harness-issues.md` 的剩余 issue（4 项执行原语：I-15/18/20/21）。
> 上一个会话（本会话）已完成 **I-01…I-14 + I-23 + I-24**（17 项闭环 16 项），ADR 先行 + TDD，全量测试 **528 passed / 2 skipped**（另有 1 个网络环境依赖测试剔除）。
> **2026-08-23 范围定论**：mission 编排（I-16/17/19/22）归 **app 层**，py harness 只做执行原语（ADR-038 框架提供原语、APP 决定调度）。

---

## 1. 下一步会话要做什么

按 `issues/python-harness-issues.md` 剩余 issue 推进（**已删除已完成的 I-01…I-12 及 I-23/I-24，4 项归 app 附录**）：

```
第一层 混合推理：✅ I-13/I-14 已完成（backend 声明+本地后端+单级回退+模型服务池）→ I-15（token 计量原语）
第二层 引擎能力：I-18（Program）独立可并行；I-20（A2A/插话窗口）；I-21（OTel，依赖 I-15）
第三层 SDK 执行面：✅ 完成（I-23 client.run + I-24 装配收敛含 sessions.stop 迁移）
归 app 层：I-16（路由）/ I-17（mission 图+JIT）/ I-19（状态机+Wave）/ I-22（模板复用）
```

**首选起点：I-15（token 计量原语）**——HookContext 已有 tokens_in/out 字段（I-09），`llm.call` payload 已带 backend/attempt（I-13）；I-15 捕获 usage 填充 + 审计归因（硬依赖仅 I-09）。I-18（Program）完全独立可随时并行。

## 2. 已产出工件（直接引用，勿重复）

| 工件 | 路径 |
|---|---|
| Issue 清单（剩余 4 项执行原语 I-15/18/20/21 + 4 项 app 附录，依赖序，验收标准） | `issues/python-harness-issues.md` |
| 实现操作指南（每 issue 代码位置/行号/改法；执行顺序总览已补 I-22/23/24，标注完成状态） | `python-harness-implementation-guide.md` |
| mission 设计文档 | `mission-control-implment-plan.md` |
| 本阶段完成记录（I-01…I-14 + I-23 + I-24 + ADR 明细） | `docs/archive/solved-issues-through-I-43.md`（末尾 2026-08-19 章节） |
| 未解决清单（P0-4 崩溃恢复 checkpoint task 块已落地 = I-11；剩余项见 issues/I-77…I-80） | `docs/archive/todo/unsolved-issue-2026-09-07.md` |
| 已定 ADR | `docs/adr/`：024 修订 / 034 Rev2 / 036 注记 / **041**(reasoning 契约) / **042**(hooks) / **043**(生命周期) / **044**(Stop 双轨) |
| DSH deepseek 适配器参考（reasoning 回传） | `deepseek_workspace/appimg/resources/resources/dsh/node_modules/@deepseek-ai/dsh-llm-deepseek/lib/index.js:48-61` |

## 3. 项目与代码位置

- **可写副本**：`/home/gexingdeyun/OH_workspace`（本会话所有改动落此，git commit `54c4f12` 起，I-11 已提交）。
- `deepseek_workspace/base_harness_py/` 为**只读对照**（旧会话副本，harness/tests 与本目录一致，但**不含本会话改动**——勿再引用为基线）。
- **venv**：`/home/gexingdeyun/OH_workspace/.venv/`（Python 3.13.7，依赖齐全）。
- 测试基线：**528 passed / 2 skipped**；`pyproject.toml` testpaths 已收敛为 `["tests"]`（裸 `pytest` 即可，不再收集 app/）。

### 运行测试的正确方式

```bash
cd /home/gexingdeyun/OH_workspace
.venv/bin/python -m pytest tests/<目标文件> -q -p no:cacheprovider   # 每轮 TDD
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider              # 全量 ~3min
```

**已知环境 flake**：`tests/test_sandbox.py::TestFirewallPropagation::test_host_network_allows_connections` 依赖外网（curl httpbin.org），无外网时失败——全量回归时用 `--deselect` 剔除。

## 4. 本会话已核实的关键事实（可直接采信）

1. **I-01…I-14 + I-23 + I-24 全部完成并有测试**（详见 `docs/archive/solved-issues-through-I-43.md` 2026-08-19 章节）——不要再重复做。
2. **hooks 已统一**：`harness/hooks/`（四模式/优先级/错误隔离/进程级注册表）+ `client.hooks` 视图；特设钩子（register_hook/_tool_hook/set_content_filter）**已全部删除**，无兼容层；`install_sandbox_mutex` 在 `harness/gateway/hooks.py`。
3. **新 hook 点已接入**：`llm.call`（around，_call_llm 内 trigger + final 包裹）、`agent.pre_step`（loop 步前）、`agent.lifecycle`（start/stop）；**Stop 双轨已落地（I-12）**：`agent.stop.framework`（硬闸门，内置 veto 最低优先级 + fail-closed 复查）+ `agent.stop.app`（默认裁决表 priority 0，app 可覆盖）；`request_stop`/`_framework_stop_check`/`_app_stop_check`/`agent_stop_denied` 事件/`client.agents.stop`。
4. **上下文链路已修**：`prompt_assembly.build_messages` 锚点保护 + `truncate_output`(8K) + reasoning 回传（`reasoning_passback`/`thinking` 在 AgentSpec）；`compress` system 常量。
5. **生命周期与 wake 已建（I-11）**：AgentRuntime 状态机（compiled/idle/running/stopped + `stop()` + `is_idle_expired` + **收件箱 + 懒创建 daemon worker 线程 + `wake()`/`dispose()` + `_loop_lock` + `_resume_events`（ADR-039 幂等配对）+ `_current_task`**）；pool 复用守卫 + `get_by_instance_id` + 丢弃路径 dispose；`client.agents.status` 池感知；`events.send` 自动唤醒绑定 agent；`checkpoint(task=...)` task 块（P0-4 地基）。
6. **ADR-017 不存在**（DAG 移除时删除），"框架不做控制流"由 ADR-006 承载。

## 5. 关键设计决策（已写入 ADR，实现时遵守）

1. `reasoning_content` 不硬编码：`AgentSpec.reasoning_passback`/`thinking` 显式 bool；仅工具调用轮回传（DSH 规则）。（ADR-041）
2. hook 注册表进程级单例，SDK 只是 `sdk.hooks` 视图；四模式错误隔离不可违反。（ADR-042）
3. stop 是请求不是结论：`agent.stop.framework` 硬闸门（veto 优先、app 不可覆盖）+ `agent.stop.app` 软裁决（可否决转 follow-up）；两轨落同一终态。（ADR-044）
4. IDLE 必须配套 wake + dispose；wake 复用 ADR-039 恢复重放算法。（ADR-043 — **I-11 已落地**：收件箱/worker/`_resume_events`/`_current_task`/checkpoint task 块）
5. 上下文修复 = 缓存修复 = 正确性修复：锚点不可截断、system 常量、8K 输出上限、assistant 完整回传。（ADR-034 Rev2）
6. 混合推理省钱公式：本地跑 + 强 verify + 快速升级路径 + Program Agent 零 token；不是"能本地就本地"。

## 6. 建议下一会话使用的技能

- **`tdd`**（`.dsh/skills/tdd/`）：I-13/14 等必须测试先行；项目 SOP `docs/sop/testing-strategy.md` 规定三层策略。
- **`to-issues`** / **`triage`**：如需再拆 issue / 管理 tracker（`docs/agents/triage-labels.md`、`.scratch/<slug>/issue.md`）。
- mission 实现前重读 `mission-control-implment-plan.md` 与 ADR-006/018/019/036/039。

## 7. 注意事项 / 坑

- 本会话沙箱：`OH_workspace` 可写；`deepseek_workspace/base_harness_py` **只读**（勿写）。
- 全量回归 ~3min；每改一处先跑相关单测文件。每 issue 走红→绿→重构，issue 收尾才全量。
- `app/` 是 ADR-038 前遗留（testpaths 已排除），**不改代码**。
- 新增测试沿用 conftest fixtures（`client`/`tmp_home`/`sample_spec`、`pool`/`store`/`mock_sandbox_manager`/`_spec()`）。
- `tests/test_prompt_assembly.py` 的 `content_filter` 参数是纯函数能力（prompt_assembly 层），保留；hooks 层的 `tool.result.transform` 才是迁移后的扩展点。
- DSH AppImage 挂载点临时；需要时从 `/home/gexingdeyun/桌面/deepseek-harness-desktop-0.2.1.AppImage` 提取（副本在 `deepseek_workspace/appimg/`）。

---

## 8. Planner–Executor 分层与翻译 MVP（mission 相关，保留）

py harness = 小模型执行引擎（mission 执行端）；DSH = 大模型驱动端（薄工具消费 mission 契约，不移植）。mission 契约（I-19，**app 层设计约束**）：`POST /mission`、`GET /mission/{id}/status`、`verify|interject|stop`、`/result`。mission 声明层走 **Jinja 前端渲染**（ADR-045：模板→YAML→Schema→JIT，双模式静态渲染/JIT 动态构建）。翻译 MVP（wave1→2→3）为 app 层参考验收场景——详见 `python-harness-implementation-guide.md` 附录 A（已标注归 app）。
