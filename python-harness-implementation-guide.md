# python-harness 实现操作指南（含代码位置）

> 配套文件：`issues/python-harness-issues.md`（issue 清单）
> 代码基线：`base_harness_py/`（与 `OH_workspace/` 的 `harness/`、`tests/` 完全一致；`OH_workspace` 另有 `pyproject.toml`、`registry/`、`tokenizer_dir/`）。**2026-09-06**：遗留 `app/`（旧 heuristic-learning 原型，HL）已删除——其 API 过时且任务设计与 ADR-038 mission 编排定论相悖。
> 测试基线：393 passed / 2 skipped（`tests/`）
> 设计文档：`mission-control-implment-plan.md`（mission 模块）；ADR 体系在 `docs/adr/`（重点：017 框架不做控制流、018 共享文件系统通信、019 完成信号、036 推拉分离、038 移除 DAG、039 任务边界与崩溃恢复）
> 参考实现：DSH 完整源码见 `deepseek-harness/`（只读参考，gitignored）

---

## 执行顺序总览

> **2026-08-23 范围定论**（`issues/python-harness-issues.md`）：mission 编排归 **app 层**（I-16/17/19/22 移出 py harness），py harness 只保留 **执行原语**。以下为更新后的三层顺序。

```
已完成：I-01 … I-14 + I-23 + I-24（2026-08-19，见 docs/archive/solved-issues-through-I-43.md）
第一层 混合推理：✅ I-13/I-14（backend 声明+本地后端+单级回退+模型服务池）→ I-15（计量原语；I-15 硬依赖仅 I-09）
第二层 引擎能力：I-18（Program）独立可并行；I-20（A2A/插话，依赖已满足）；I-21（OTel，依赖 I-15）
第三层 SDK 执行面：✅ 完成（I-23 client.run + I-24 装配收敛含 sessions.stop 迁移）
归 app 层：I-16（路由）/ I-17（mission 图+JIT）/ I-19（状态机+Wave+工具集）/ I-22（模板复用）
```

> 已完成项见 `docs/archive/solved-issues-through-I-43.md`（2026-08-19 章节）；剩余 issue 见 `issues/python-harness-issues.md`（4 项 py harness + 4 项 app 附录）。

---

## I-01 SDK 死代码与运行时 bug

**代码位置与问题**

| 位置 | 问题 | 修复 |
|---|---|---|
| `harness/sdk/client.py:473, 486`（`_GatesClient.list_pending`） | 引用已删的 `self._client._coordinator._session_manager._store` → AttributeError | 改用 `self._client._store`（client 上已有） |
| `harness/sdk/client.py:514, 524`（`_GatesClient.get`） | 同上 `._coordinator._gate_phases` → AttributeError | 删除 `_gate_phases` 引用（Phase gate 已删） |
| `harness/sdk/client.py:916`（`_MemoryClient.estimate_tokens`） | `_count_tokens(str(payload))` 缺必填 `provider` 参数 → TypeError | 补 `provider="deepseek"`；或给 `prompt_assembly.py:29` 的 `_count_tokens` 加默认值 |
| `harness/sdk/__init__.py:20-23` | docstring 展示已删的 `JobSpec/jobs.submit_spec` | 改为当前 API 示例 |
| `harness/sdk/client.py:546, 704` | docstring 提已删 Pipeline/coordinator | 清除 |
| `harness/tools/declare_output.py:1,5` | "for Pipeline" 残留描述 | 改为独立工具语义 |
| `harness/tools/agent_mgmt.py:17,25` | "Results appear as node_result events"（node_result 已无生产者） | 改为"经 read_subagent 拉取"（对齐 ADR-036） |

**测试**：`tests/test_sdk_client.py`（补 `list_pending`/`get`/`estimate_tokens` 调用测试，先红后绿）。

---

## I-02 会话与沙箱边界修复

| 位置 | 问题 | 修复 |
|---|---|---|
| `harness/session/impl.py:171`（`fork()`） | 复制事件时复用原 `event_id` → 跨 session 不唯一，破坏 `parent_event_id` 溯源链 | 生成新 event_id，可选保留 `payload["forked_from_event"]` 映射 |
| `harness/sandbox/docker_impl.py:62`（`kill`） | 签名 `kill(self, container_id)` 缺 `keep_workspace`，与 `interfaces.py:528` 的 `SandboxInterface.kill(container_id, keep_workspace=False)` 不一致 | 补参数（Docker 后端可忽略 keep_workspace） |
| `harness/sandbox/manager.py:70` | 以 `backend.kill(id, keep_workspace=...)` 调用 → Docker 后端必炸 | 随上条修复 |

**测试**：`tests/test_session.py`（fork 后事件 ID 唯一性）、`tests/test_sandbox.py`（kill 签名一致性，接口级 mock）。

---

## I-03 接口层残留清理

**删除清单（全部无生产消费者，删除即验收）**

| 位置 | 符号 |
|---|---|
| `harness/interfaces.py:209-277` | `DAGNode` / `DAG` / `Phase` / `PhaseResult` / `JobSpec` / `JobStatusInfo` |
| `harness/interfaces.py:702-837` | `CoordinatorInterface` / `OrchestratorInterface`（含 `compile_dag`/`start_job`/`get_job_status`） |
| `harness/interfaces.py:86-96, 288-297` | `CapabilityToken` + `TokenExpiredError`/`TokenInvalidError`/`TokenPermissionError` |
| `harness/interfaces.py:883-890` | `SubagentHookContext`（SubagentStop 已随 coordinator 删除） |
| `harness/gateway/gate_manager.py:47-92` | `GateManager` 类（无调用点；**保留 `EventBus`**，`gateway.py:16` 仍用它） |
| `harness/gateway/rules.py` | 0 行空文件，删除 |
| `harness/prompt_assembly.py:173-178` | `node_result` 分支（无生产者） |
| `harness/agent_runtime/impl.py:30-33` | 未使用的 `_SafeDict`（`prompt_assembly.py:35` 有活副本） |
| `harness/gateway/gate_manager.py:13` | 未使用的 `PhaseStatus` import |

**注意**：删除前用 grep 确认无残留引用（含 `tests/`、`docs/`、`app/`）；`interfaces.py` 文件头的模块依赖图注释（line 9-35）同步更新。

**测试**：全量套件保持全绿 + 新增一个静态检查测试（grep 断言关键符号不再出现）。

---

## I-04 上下文锚点保护（与 I-05/06/07 同处 `build_messages`）

**核心位置**：`harness/prompt_assembly.py:126-137`

```python
recent: list = []
for ev in reversed(other_events):          # ← 从最新往回装
    ev_tokens = _count_tokens(str(ev.payload), provider, tokenizer_dir)
    if used + ev_tokens > budget_tokens:
        break                               # ← 问题：短路，旧事件整体丢弃
    recent.insert(0, ev)
```

**问题**：`agent_started`（最早事件，任务描述）最先被挤掉 → 模型丢失任务（正确性）+ 前缀崩塌（缓存）。实测：大输出下 iter3 起 shared prefix 从 4/6 跌到 1/5，任务消息消失。

**改法**：
1. `agent_started` 事件不可截断（锚点，等同 system prompt 级别）；其 payload 已很小（~几十 token）。
2. 截断策略改为"中间/尾部优先"：保留 system + 任务锚点 + 最新尾部，超预算时从中间丢弃（或对最旧的 tool_result 做摘要，见 I-06）。
3. `memory_summary` 仍插在任务之后（位置 2，而非位置 1）。

**测试**：`tests/test_prompt_assembly.py` 新增——① 长会话下任务消息恒在；② 相邻迭代消息前缀对比（shared prefix 不崩塌）；③ 预算边界测试。

---

## I-05 system prompt 与任务解耦

| 位置 | 现状 | 改法 |
|---|---|---|
| `harness/agent_runtime/impl.py:469-513`（`execute_task`） | `system_prompt` 每调用一传，app 可每次重建 | 约定：`system_prompt=None` 时取 `spec.system_prompt`；显式传入仅限身份类内容；任务差异走 `task_input` |
| `harness/sdk/client.py:832-845`（`_MemoryClient.compress`） | 事件 JSON 嵌进 system prompt → 每次全 miss | 事件 JSON 作为 user 消息传入，system prompt 保持常量 |
| ~~`app/hl_pipeline.py`~~（已删 2026-09-06） | 每 step 传不同 system prompt（GENERATOR/EVALUATOR） | ~~迁移为 spec.system_prompt + task_input 差异~~——遗留 app 已整体删除，不再迁移 |

**测试**：`tests/test_sdk_prompts.py`、`tests/test_sdk_client.py`（TestMemoryCompress：两次压缩 system prompt 字节一致）。

---

## I-06 tool_result 体积上限

| 位置 | 现状 |
|---|---|
| `harness/tools/file_io.py:106-113`（`read_file`） | 全量返回文件内容 |
| `harness/tools/sandbox_exec.py:50-55, 72-77`（`bash`/`sandbox_execute`） | 全量返回 stdout |

**改法**（推荐在工具层截断，比在组装层更早止损）：
- 单条输出上限（如 8K 字符），超出截断头部 + 追加 `[... truncated N chars, M lines ...]`。
- `read_file` 支持范围参数（对齐 `docs/plans/hashline-structure-read-plan.md` 的结构摘要方向，可后续做）。
- 组装层兜底：`build_messages` 对超长 tool_result 同样截断（防第三方工具）。

**测试**：`tests/test_tools.py`、`tests/test_builtin_tools.py`、`tests/test_prompt_assembly.py`。

---

## I-07 assistant 消息序列化完整化 + thinking 配置

**这是通用框架的硬性契约（供应商要求，不回传会报错）**。DeepSeek 规则（[思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)）：
- 两 user 消息之间模型**进行了工具调用** → `reasoning_content` **必须回传**给 API；
- **未进行工具调用** → 无需回传，传入会被忽略（省略以省 token）。

**DSH 参考实现**（已提取）：`appimg/resources/resources/dsh/node_modules/@deepseek-ai/dsh-llm-deepseek/lib/index.js:48-61`（`serializeAssistant`）：

```js
return {
  role: "assistant",
  content: text,
  ...toolCalls.length > 0 && reasoning.length > 0 ? { reasoning_content: reasoning } : {},
  ...toolCalls.length > 0 ? { tool_calls: toolCalls } : {}
};
```
流式捕获在同文件 `:276`（`delta?.reasoning_content` → `reasoning` block）。

**python 侧改动**：

| 位置 | 改法 |
|---|---|
| `harness/agent_runtime/impl.py:222-230`（`_emit_event("agent_thought")`） | payload 增加 `reasoning_content` 字段（非流式：`msg.reasoning_content`；流式：累积 delta） |
| `harness/prompt_assembly.py:139-200`（`build_messages`） | 新增分支：`agent_thought` → assistant 消息；`agent_completed` → assistant 消息（最终答案回传，跨任务续跑不失忆）。序列化按 DSH 规则：有 tool_calls 且有 reasoning → 带 `reasoning_content`；否则不带 |
| `harness/interfaces.py:133-161`（`AgentSpec`） | 新增显式配置字段：`reasoning_passback: bool = True`（或 `thinking: bool = False`），**框架不硬编码 thinking 行为**——通用框架对任意 OpenAI 兼容供应商生效 |
| `harness/agent_runtime/impl.py:111-122`（`_call_llm`） | 按配置透传 `extra_body={"thinking": ...}`（V4 思考模式开关，默认关或按 spec） |

**注意**：`reasoning_content` 只用于回传与审计（OTel），**绝不进普通消息回放**（费 token 且 API 不接受塞回 content）。

**测试**：`tests/test_prompt_assembly.py`（三种序列化分支）、`tests/test_mock_llm.py`（含 reasoning_content 的响应）、`tests/test_agent_runtime.py`（thinking 配置透传）。

---

## I-08 hooks 核心模块（TDD）

**新建**：`harness/hooks/`
```
harness/hooks/
├── __init__.py
├── registry.py    # HookRegistry: on(point, mode, fn, priority) / trigger(...)
├── context.py     # HookContext 数据类 + contextvars 承载当前上下文（含 trace 字段）
└── points.py      # 规范 hook 点名常量（tool.execute / llm.call / agent.pre_step / ...）
```
`harness/interfaces.py` 增加 `HookRegistryInterface` Protocol。

**四条不可违反语义（先写红测试 `tests/test_hooks.py`）**：
1. 四模式：observe（返回值忽略）/ transform（返回值替换 payload）/ veto（非 None = 拒绝短路）/ around（`call()` 包裹前/后/异常）。
2. 优先级确定性：同点按 priority 降序，同 priority 按注册序。
3. 错误隔离：observe 异常吞记；transform 异常回退原值；veto 异常 fail-closed（deny）；around 异常透传。
4. contextvars 传播：嵌套 hook、跨线程、跨 async 边界不丢字段。

**红线**：模块是叶子依赖（不 import session/gateway/runtime）；注册表在**进程级模块级单例**（`HookRegistry.get()`），SDK 只是视图（见 I-09、ADR-042 §8 混合模式边界）。

---

## I-09 现有 hook 迁移 + 新增 hook 点 + SDK 视图

**迁移来源（每迁一个删一处特设代码）**：

| 现有特设 | 位置 | 迁移到 |
|---|---|---|
| Gateway pre/post hook | `harness/gateway/gateway.py:346`（`register_hook`） | `authorize.veto` / `authorize.observe` |
| `_tool_hook`（PostToolUse） | `harness/agent_runtime/impl.py:75, 413-424, 455-465` | `tool.execute.around` |
| `content_filter` | `harness/sdk/client.py:648-657`（`_PromptsClient`）+ `impl.py:162-168` | `tool.result.transform` |
| `SandboxMutexHook` | `harness/gateway/hooks.py:49-102` | 保留为 builtin hook，改挂新注册表 |

**新增 hook 点（mission/OTel 消费方）**：`llm.call`（around，路由+计量+span）、`agent.pre_step`（插话窗口）、`agent.lifecycle`（start/idle/stop）、`agent.stop.framework` / `agent.stop.app`。

**SDK 视图**：`harness/sdk/client.py` 增加 `self.hooks = _HooksClient(self)`。**已选型：模块级注册表单例（混合模式，ADR-042 §8）**——hooks 全局共享、业务服务（gateway/store/pool）维持 per-client 实例隔离；**明确不做共享 core**（`client.py:104-120` 的 per-client 实例化保持不变）。

**测试**：迁移等价性（旧行为不变，393 全绿）+ 新增 hook 点行为测试 + "两个 client 注册互相可见"测试。

---

## I-10 Agent 生命周期：IDLE + dispose

| 位置 | 改法 |
|---|---|
| `harness/sdk/models.py:15-19`（`AgentStatus`） | 扩展：`COMPILED → IDLE ⇄ RUNNING → STOPPED`（当前只有 COMPILED） |
| `harness/agent_runtime/impl.py:469-501`（`_run_agent_loop`） | loop 退出 → 状态置 IDLE（不是销毁） |
| `harness/orchestration/pool.py:175-207`（release/destroy） | 显式 `stop()` 才进 STOPPED 并释放；新增 idle timeout 回收（对称 `sandbox/impl.py:46-62` 的 TTL reaper 机制） |

**状态查询 API**：`client.agents.status(instance_id)`。

**测试**：`tests/test_pool.py`、`tests/test_sdk_client.py`（状态流转 + idle timeout 释放）。

---

## I-11 wake 机制 ✅ 已完成（2026-08-19）

| 位置 | 改法 | 状态 |
|---|---|---|
| `harness/sdk/client.py`（`events.send`） | append 后 `_wake_session`：session metadata instance_id → spec → pool.acquire（复用守卫）→ `agent.wake()`；未绑定 session 仅追加 | ✅ |
| `harness/agent_runtime/impl.py` | `_resume_events`（ADR-039 幂等配对，剔除悬挂 tool_called/agent_thought）；`_agent_loop` 读事件经 resume 视图 | ✅ |
| 新增 | 每 agent 懒创建 daemon worker 线程 + `queue.Queue` FIFO 收件箱；`wake()`/`dispose()`/`_loop_lock`；`_current_task`（Task 五元组） | ✅ |
| `harness/orchestration/pool.py` | `get_by_instance_id`；acquire 丢弃/destroy/destroy_all/stop_instance 全部 dispose（join 线程） | ✅ |
| `harness/session/impl.py` + `session_store/impl.py` | `checkpoint(task=...)` task 块（P0-4 恢复点地基）；`get_session_metadata()` | ✅ |

**设计参考**：ADR-039（任务边界与崩溃恢复）的恢复重放算法就是 wake 的自然复用；DSH 的 inbox → turn 机制是同一语义。

**测试**：`tests/test_wake.py`（14 测试：状态机往返/RUNNING 排队/dispose/`_resume_events` 三场景/pool dispose/MockTransport 上下文累积/SDK 链路）+ `tests/test_session.py` checkpoint task 块（2 测试）。基线 450 → 466。

---

## I-12 Stop 双轨制 ✅ 已完成（2026-08-19）

**依赖**：I-09（两个 stop hook 点）、I-10（STOPPED 状态）、I-11（worker/dispose 终止路径）。

| 轨 | 语义 | 落地 |
|---|---|---|
| `agent.stop.framework`（硬闸门） | 沙箱死/超时/资源超限/预算熔断；veto 优先、不可被 app 覆盖 | `_framework_stop_check`（`agent_runtime/stop.py` 内置 veto **最低优先级 -100** + fail-closed 复查）；新增 **sandbox_death** 探活条件 |
| `agent.stop.app`（软裁决） | 编排者判定"是否可以停"；请求-裁决语义，否决转 follow-up | `request_stop(reason)`（fail-closed：非 RUNNING/非法 reason 拒绝）+ `_app_stop_check`（步窗口裁决）；默认裁决表 priority 0，app 可覆盖（veto 否决 / around 批准需确认） |

**事件/终态**：批准 → `agent_completed`{reason:"stopped", stop_reason} → loop `stopped` → IDLE（可恢复，SSE 语义）；否决 → `agent_stop_denied` → 续跑；framework → `agent_failed`。`client.agents.stop(instance_id, reason=...)` 为 SDK 请求入口。

**测试**：`tests/test_stop_dual.py`（14 测试）——硬闸门触发+hook 触发+app 不可覆盖 / 裁决批准·否决·非法拒绝·IDLE no-op / 覆盖（veto、around）/ 终态一致 / SSE 停→wake 续跑 / SDK 端到端。基线 466 → 480。

---

## I-13 模型配置扩展 + 本地后端

| 位置 | 改法 |
|---|---|
| `harness/interfaces.py:141, 151-159`（`AgentSpec`） | 保留 `model`/`llm_profile`/`llm_fallback`；新增 thinking 显式控制（见 I-07） |
| `harness/sdk/config.py:21-36`（`LLMConfig`） | 扩展：`backend: "cloud"|"local"`、`endpoint`、`lora_adapter`、`quantization`、`warm` |
| `harness/agent_runtime/impl.py:67, 111-122`（`_call_llm`） | 按 backend 解析 client（本地 vLLM/Ollama 是 OpenAI 兼容 API，同一 client 换 base_url）；`extra_body` 透传 thinking/effort |
| `harness/orchestration/pool.py:112-122`（`resolve_llm`） | 扩展 profile 解析路径 |

**测试**：`tests/test_sdk_config.py`（新字段解析）、mock 本地端点的调用测试。

---

## I-14 双池化

| 位置 | 改法 |
|---|---|
| 新增 `harness/orchestration/model_pool.py` | `ModelServicePool`：本地模型服务进程（vLLM）的拉起/保活/LoRA 热切换/驱逐（GPU 稀缺资源）；与任务沙箱解耦 |
| `harness/orchestration/pool.py` | agent 解析 backend 后路由：cloud → 直连；local → 从 ModelServicePool 取 |

**测试**：进程级 mock（假 vLLM 进程）的生命周期测试；并发获取/释放无竞态。

---

## I-15 token 计量（原语）✅ 已完成（2026-09-04 — grill 收敛：审计归 app，py harness 缩为捕获原语）

| 位置 | 改法 | 状态 |
|---|---|---|
| `harness/agent_runtime/impl.py`（`_call_llm_once` → `do_call`，约 :224） | 捕获 `response.usage` → `set_context_fields(tokens_in=usage.prompt_tokens, tokens_out=usage.completion_tokens)`；`usage=None` 时跳过（字段保持 None，无估算兜底） | ✅ 已实现 |
| `harness/hooks/context.py`（I-09） | `tokens_in/tokens_out` 字段已有（默认 None），I-15 填充服务端实测值 | ✅ 无需改 |
| ~~`harness/audit_log.py`~~ | ~~调用级计量落盘~~ → **归 app**（grill 决策 B）：审计由 app around hook 落 | 归 app |
| `llm.call` around hook（app 消费点） | `next_fn()` 后读 `current_context()` 的 tokens_in/out——注意 **ctx 在 final 前构建，`ctx.tokens_in` 恒 None**（隐性契约，见 issues I-15 备注） | 归 app 消费 |

**测试**：`tests/test_llm_backend.py::TestTokenMetering`（2 测试：带 usage 成功 → (in,out)；后端不报 usage → (None,None)）。
**设计边界（grill 2026-09-04 决策 A/B/C/D）**：A 框架只填 contextvars、编排归 app；B 审计归 app；C 服务端原值透传；D 逐 attempt 独立、失败 attempt=None、不跨 trigger 累积。

---

## I-16 路由策略（云监工 / 本地工人）[HITL] ✅ 归 app 层（py harness 不实现）

**策略本身归 app 层**（DSH 驱动端，人工校准：本地/云分配、verify 严度、升级阈值）。
**py harness 侧触发点已就绪**（无需再建）：
- `llm.call` around hook（I-09 已接入 `_call_llm`）——app 在此做路由/计量消费；
- I-15 计量（token 占比可观测）+ I-13/14 双后端。

**验收（app 层）**：策略可插拔；质量门失败升级路径打通；成本基线可观测。

---

## I-17 mission 数据模型 + JIT 编译器 ✅ 归 app 层（py harness 不实现）

图模型（统一 Agent Class）与 JIT 校验是 **app 层（DSH 驱动端）** 编排逻辑，不在 `harness/` 实现：
- 声明层：Jinja 前端渲染（ADR-045）→ 图 YAML → Schema 校验；
- 编译层：JIT（环/双根/缺父/深度/manage_scope）——设计依据 `mission-control-implment-plan.md`（§0/§2/§4.1）。
**py harness 只提供执行 API 原语**（I-23 run/wake/stop、hooks）。

---

## I-18 Program 执行器（SDK 执行面包装）✅ 已完成（2026-09-05）— grill 收敛：非新引擎

**实现文件**：`harness/sdk/program.py`（`with_retry`/`run_script_host`/`run_script_sandbox`/`ProgramResult`）+ `harness/sdk/client.py` `_ProgramClient`（`client.program.run`，挂 `HarnessClient.__init__`）；**测试**：`tests/test_program.py`（13）。

**定位**：Program = agent 类的**配置子集**（无 LLM/无推理循环），以 **SDK 子客户端包装下层原语**表达——不改执行引擎、不新建 `harness/orchestration/program.py`。

**落点**：
- 新入口 `client.program.run(script, session_id=None, sandbox_id=None, timeout=…, retries=…)`（独立子客户端，`sdk/client.py` 新 `_ProgramClient` + models：ProgramResult）；沙箱可选——给了则经 manager 解析 sandbox 实体（I-26 `ensure`/规范）于沙箱实体内执行（脚本可写 FS artifact，ADR-018）；不给则宿主直跑（subprocess + 同套超时/重试）
- 执行复用 sandbox `execute(sandbox_id, cmd)`（含 execute_timeout）；**新小件：重试策略 util**（瞬时失败重试 N 次、超时/永久失败不重试——mission §2 TimeoutGuard/RetryPolicy 语义）——建议放 `harness/sdk/` 或 `harness/util/`（实现时定）
- 输出写 **session 事件**（program_started / program_completed/output + exit code），下游/后续 run 经事件流读取；不构造 AgentRuntime、不触 llm.call/agent loop → 天然零 token、可经 audit/hooks 观测（agent.lifecycle 可另行挂）

**测试**：单元（重试/超时/失败分类）+ SDK 集成（fake 执行器 + 真 bwrap 1-2 条按可用性）。

---

## I-19 mission 状态机 + Wave 调度 + 工具集 ✅ 归 app 层（py harness 不实现）

状态机（PENDING→READY→RUNNING→终态）、Wave 定义/verify/阻塞/动态增删、AutoTrigger、`/mission` 工具集与权限矩阵均为 **app 层编排逻辑**（设计见 `mission-control-implment-plan.md` §1/§2；声明层走 Jinja 渲染，ADR-045）。
**py harness 提供其执行原语**（全部已就绪/计划中）：I-11 wake、I-12 stop（ADR-044）、I-15 计量、I-18 Program、I-20 A2A、I-23 run。

---

## I-20 A2A + 插话

**落点**：
- A2A：共享文件系统（ADR-018）+ 向目标 session `events.send` 注入消息（`sdk/client.py:396`）；通信原语在 py harness，权限矩阵（同 Group 可通/跨 Group 需授权）为 **app 配置**（mission 计划 §3.1）。
- 插话：`agent.pre_step` hook 窗口（I-09）注入 modify/abort/skip/inject_context（计划 §4.2）；abort 走 framework/app stop（I-12）。

**验收**：同组互通、跨组拒绝；插话四种 action 生效；端到端演示。

---

## I-21 OTel 追踪与成本可视化

**落点**：
- 纯 observe hook（I-08），零侵入。
- `contextvars` 跨线程/async 传播；沙箱子进程经 W3C `traceparent` env 传进 bwrap（`harness/sandbox/impl.py` 的 spawn/execute 处注入）。
- 导出 I-15 的 token 计量 → 成本视图（按 agent/会话；mission/wave 维度归 app 编排层）。

**验收**：调用链 span 完整；跨进程 trace 关联；成本视图可渲染。

---

## I-23 SDK 公共执行入口（client.agents.run）✅ 已完成（2026-08-19）

**依赖**：I-01（SDK 清理，已完成）、I-10/11/12（执行栈完整）。边界已写入 `issues/python-harness-issues.md` I-23 条目。

**决策落点（grill 11 项闭合）**：

| 决策 | 落地 |
|------|------|
| 同步 run + RunResult | `_AgentsClient.run(spec\|instance_id, task, session_id=None, config, system_prompt, tools)` → `RunResult{session_id, instance_id, status, output, error, stop_reason}`（`sdk/models.py`）；异步沿用 `events.send`/wake |
| 双形态 | spec 自动 register；instance_id 复用已注册实例；结果带 instance_id 可接 `agents.stop` |
| session 续用 | None → `sessions.start` 新建绑定；session_id → 轻校验（存在即可，SessionNotFoundError → ValueError） |
| 装配 raise / 结果返回 | acquire/校验/session 错误 raise（fail-fast）；completed/failed/stopped 进 RunResult |
| 忙时 raise | RUNNING → RuntimeError("busy")（不阻塞不排队） |
| tools 过滤（Q7 bug 修复） | `AgentRuntime._spec_tools()`：tools=None → spec.tools 白名单 + subagent_spawn 条件追加；`_run_agent_loop` 归一化（execute_task/run_loop/wake 三入口一致）；修复 `tools or []` 掩盖的 execute_task 无工具 schema 缺口 |
| output 事件流 | `_terminal_output`：最后 agent_completed.output 回落最后 agent_thought.content；stop_reason 仅 stopped |

**迁移（验收 #2）**：`_MemoryClient.compress` → `agents.run`（自建 session 保留自定义 metadata）；`test_sdk_client.TestMemoryCompress` mock `agents.run`；`TestLLMProfileResolution` 迁 `test_pool.py`。

**测试**：`tests/test_sdk_run.py`（11 测试）。基线 480 → 491。

---

## I-24 装配引导收敛 + sessions.stop 迁移 ✅ 已完成（2026-08-19）

**保底优先级（显式 > sandbox.yaml > 内置 main）**：`HarnessClient._load_sandbox_configs()` 收敛了原 CLI `_client()` 的 53 行样板——sandbox.yaml 读取、技能挂载扫描（**双向**：registry 目录 + 内存注册；顺带修复 `svc.list()` → `list_all()` 的潜伏 bug——扫描此前从未生效）、保底 main（bubblewrap/network=host）。

**三前置 fail-fast**：

| 前置 | 落点 |
|------|------|
| hmac_secret | 构造期 RuntimeError（含指引，锁定测试） |
| sandbox | 构造期保底 main（永不缺，acquire 不再 ValueError） |
| LLM profile | `register` 字段非空校验 + `run()` acquire 解析失败重抛指引（`pool.acquire` 不再吞 resolve 错误——缺失 profile 不再以 OpenAIError "Missing credentials" 运行期随机炸） |

**sessions.stop 迁移（必须项）**：始终追加 `session_stopped` 标记（审计）+ 绑定实例且池内 running → 转调 `request_stop("user_requested")`（I-12 双轨真裁决）；未绑定/未运行 → 仅标记（向后兼容）；CLI `harness stop` 同步生效。

**最小可执行 agent（验收 #4 示例）**：
```python
from harness.sdk import HarnessClient
from harness.interfaces import AgentSpec
client = HarnessClient()                      # 保底 main 沙箱 + local.yaml/env 读取
inst = client.agents.register(AgentSpec(
    name="minimal", description="t", tools=["read_file"],
    model="deepseek-v4-flash", sandbox="main",
    llm_profile="default", max_iterations=3,
    permissions={"subagent_spawn": False}))
res = client.agents.run(inst.instance_id, "do something")
print(res.status, res.output)                  # 无需 sandbox_configs/hmac 手动配置
```

**测试**：`test_sdk_client.py` 增补 8 测试（保底/yaml 合并/技能挂载/hmac 指引/llm_profile 校验/run 指引/sessions.stop 转调 2 测试）。基线 491 → 499。

---

## I-25 run/stop/status/wake 按注册实例分派 ✅ 已完成（2026-09-04，grill 定稿）

**目标**：「注册两个同模板实例、各自 run 并发跑」的公共 SDK 路径；消除按 spec 名复用塌缩。

| 位置 | 改法 | 状态 |
|---|---|---|
| `harness/orchestration/pool.py` `acquire`（:62） | 新增可选 `instance_id` 参数——精确键控 `(name, instance_id)`，运行时 `instance_id` = 注册 inst_id（同模板两实例 uuid 互异）；开头 `requested_instance_id` 捕获防被 sandbox `get_or_create` 的局部同名变量遮蔽；`instance_id=None` 保持 legacy 随机 uuid 语义 | ✅ |
| `harness/sdk/client.py` `agents.run`（:461） | `pool.acquire(spec)` → `pool.acquire(spec, instance_id=inst.instance_id)` | ✅ |
| `_wake_session`（:307） | 同上 instance-keyed（session meta 已绑注册 inst_id → 懒建/唤醒该实例） | ✅ |
| `agents.stop`（:412） | `list_instances(spec_name)` 首个 running → `get_by_instance_id` 精确定位（不再可能停错同名实例） | ✅ |
| `agents.status`（:392） | `list_instances()[0]` → `get_by_instance_id`（未 run 实例回落 registry COMPILED） | ✅ |

**Busy 语义**：同实例 RUNNING 时 run 保持 raise；follow-up 走 `events.send`→wake 收件箱（不引入 run 排队）。

**测试**：`tests/test_instance_dispatch.py`（7 测试：S1 池键控区分/复用、S2 run 按实例分派 + busy 实例级、S3 status/stop 精确命中、S4 wake 精确唤醒）。fake OpenAI 经 `impl_mod.openai.OpenAI` 替换（message 需带 `tool_calls=None`）。

**注意**：SDK 执行面一律走 `instance_id` 参数；`acquire(spec)`（无 instance_id）仅供 legacy/测试/非 SDK 用法（随机 uuid 运行时，无法被按实例定位）。

---

## I-26 沙箱 uuid 身份 + agent→sandbox 关联 ✅ 已完成（2026-09-04，grill 定稿）

**目标**：命名空间层解耦——沙箱身份统一 sandbox_id(uuid)；SandboxInterface 按 uuid 寻址，OS 句柄（PID/容器 id）不出后端。

| 位置 | 改动 |
|---|---|
| `harness/interfaces.py` | SandboxInterface 签名：container_id→sandbox_id；`spawn(config, sandbox_id)` |
| `harness/sandbox/impl.py` + `docker_impl.py` | 内部 `_by_uuid` 私有映射（sandbox_id→PID/container）；方法头翻译；respawn 同 uuid 覆盖式重注册；TTL reaper 内部按 OS id |
| `harness/sandbox/manager.py` | 实体化：`SandboxEntity(sandbox_id, config_name, backend)` + `_entities[uuid]` + `_default_by_config`；`get_or_create(name, create_new=False)` name 级（规范复用/create_new 独立 uuid）；`create(name)` 总是新建；`ensure(name, sandbox_id)` 绑定重物化；`get_entity/destroy(sandbox_id)/list()` |
| `harness/orchestration/pool.py` | acquire 加 `sandbox_id`；manager 解析实体（`ensure`/规范）；runtime 存 `_sandbox_id`（uuid）；配置变更 respawn 在 manager 内、uuid 稳定 |
| `harness/agent_runtime/impl.py` | `_sandbox_instance_id`→`_sandbox_id` |
| `harness/sdk/client.py` + `models.py` | register/run/wake 传 `sandbox_id`；`AgentInstance.sandbox_id`；_SandboxClient.list() 实体化、destroy(sandbox_id) |
| `harness/cli/main.py` | skills list --sandbox-id → 规范实体 entity |

**语义要点**：uuid 稳定跨 respawn（D4）；绑定缺失透明重物化（D5，`ensure`）；destroy = 拆除（默认 get_or_create 后铸新 uuid；pinned ensure 同 uuid 重新供给）；策略层（共享/隔离/重绑）→ I-27。

**测试**：`tests/test_sandbox_entity.py`（16 测试）+ 既有 sandbox/pool/audit/integration 迁移至实体语义。

---


## I-27 策略中间件（gateway/hooks 定向规则 + preset）✅ 已完成（2026-09-05）

**定位**：gateway 层定向策略中间件——既有全局 hooks（authorize.veto/observe + tool.execute，ADR-042 §8）按 ctx.spec.name 定向，纯新增不改现有代码；一切默认关。

| 文件 | 内容 |
|---|---|
| `harness/policy.py` | PolicyEngine 单例：define/enable(name,agent)/add/disable/list/effective；注册 resilient 全局 AUTHORIZE_VETO/OBSERVE + TOOL_EXECUTE hooks（registry 被 clear 自动重挂）；多规则=并集+deny-wins |
| `harness/policy_presets.py` | 4 preset（默认停用）：bash_whitelist（危险通配）/ fs_paths（系统目录）/ read_before_edit（DSH 版本化；hash 纯参数门 + literal digest_provider 适配器，角色映射 cfg 零工具耦合）/ net_policy（IP/CIDR/域名命令级 deny） |
| `harness/interfaces.py` | AgentSpec.policy（str|list|None）可选字段 |
| `harness/sdk/client.py` | `client.policy`（enable/add/disable/list）；agents.register 读 spec.policy 自动 enable（warn-ignore 未知名） |

**启用语义**：enable/add 必带 agent（或 "*"）；spec.policy = register 自动 enable(name, agent=spec.name)；hook 内 ctx.spec 为 None 或未定向 → 放行。DSH read→edit 版本化：hashread 输出 digest 作 observed，hashedit old_hash 比对；literal 走 cfg.digest_provider(sha256)（缺省 seen fallback）；外部改动 → stale deny。

**测试**：`tests/test_policy.py`（16）。全量 582 passed / 0 failed（默认关不变量）。

**注**：沙箱实体绑定策略面（共享组/隔离/重绑，I-26 实体关联之上）另记 **I-28 草稿**。

---

## 关键设计决策备忘

1. **reasoning_content 不硬编码**：通用框架用显式 bool 配置（`AgentSpec.reasoning_passback`/`thinking`）控制，供应商规则（工具调用轮才回传）作为适配层契约实现，参考 DSH `serializeAssistant`。
2. **hook 注册表在进程级**：修掉"client A 的 middleware client B 不可见"，SDK 只是视图（对齐你之前"注册表进程级、上层外接"的定论）。
3. **stop 是请求不是结论**：app stop 走裁决 hook（可否决），framework stop 是硬闸门（不可覆盖）。
4. **IDLE 必须配套 wake + dispose**：无 wake 的 IDLE 是装饰；wake 复用 ADR-039 恢复算法。
5. **上下文修复 = 缓存修复 = 正确性修复**：I-04/05/06/07 全落在 `build_messages`/`agent_thought` 一处，合并为一个里程碑推进。
6. **省钱公式**：本地跑 + 强 verify + 快速升级路径 + Program Agent 零 token；不是"能本地就本地"。

---

## 附录 A：Planner–Executor 分层与翻译 MVP 场景（✅ 归 app 层参考验收场景）

### A.1 分层形态（全局定位）

- **py harness = 小模型执行引擎**（mission 执行端，专门适配小模型）；**DSH = 大模型驱动端**（规划/监督/验收）。
- 关系：DSH 会话通过**薄驱动工具**（HTTP/CLI）消费 py harness 的 mission 契约；**不做插件移植**（移植会否定 py harness 独立存在的价值）。
- 一句话：大模型只做 plan + 监督 + 验收；小模型干 90% 的活；Program Agent 干零 token 的活。

### A.2 mission 契约（I-19 工具集的设计约束）

I-19 的 `/mission` 工具集必须按以下**对外稳定、版本化**的契约设计（否则 DSH 驱动端进不来）：

```
POST /mission                {plan: MissionGraph, params, budget} → mission_id
GET  /mission/{id}/status    PENDING/READY/RUNNING/…/COMPLETED/FAILED
POST /mission/{id}/verify|interject|stop
GET  /mission/{id}/result    artifacts + verify 报告 + token 成本
```

### A.3 escape-hatch 边界（"可编程 SOP + agent 自主"）

| 层 | 归属 | 机制 |
|---|---|---|
| 确定性核心（SOP）：图/JIT/拓扑/verify/超时/预算/权限 | 代码（ADR-006），LLM 不可覆盖 | mission graph + hooks |
| 自主决策点（逃生口）：任务参数/verify 阈值/插话/失败升级 | agent（ADR-017） | 每个闸门的受控覆盖路径 |

原则：**每个确定性闸门配一个显式的、本身受治理的逃生口**——这是"可编程 SOP"与"写死的脚本"的本质区别。

### A.4 翻译 MVP（里程碑 5 的参考验收场景，替代抽象的 star mission）

模板 `translate_domain_docs {domain, glossary_path, source_dir, target_lang, chunk_size}`：

```
Wave 1（Program，零 token）:
    chunk_docs（内部 for 循环：按章节/段落切 N 块）+ build_glossary
    + 生成 chunks.json + 进度 manifest（每块: pending/done/failed/upgraded）
    + verify 完整性（源文件数=分块数、无遗漏段落）→ block_next=true

Wave 2（固定容量 worker 池 + manifest 拉取）:
    translator_1..M（静态定义，池容量 M 由配置定，**不随 N 变**）:
        从 manifest 拉下一个未处理 chunk → 译 → Program verify
        → 标记 done → 拉下一个 → 直到 manifest 空
    每块完成即 Program 自动 verify（AutoTriggerHook）:
        format_check（段落/代码块/标记一致）+ term_check（术语表命中率）
        + length_check（长度比容忍区间）
    on_failure → 云端大模型仅重译该 chunk（ConditionalRoute，chunk 级升级）
    动态组装（编排 agent 自主，纯既有指令）:
        读 manifest 后按需 `wave -modify` 调整池容量 / 增删 worker

Wave 3（Program + 云端抽检）:
    assemble（按原结构合并 → final/）+ spot_check（云端抽审 ~10%）+ 验收
```

**设计原则（本 MVP 采用方案 1&2，不改架构）**：
- **图声明拓扑与闸门，数量归运行期**——N（chunk 数）是 Wave 1 的运行期产物，不是图里写死的；worker 数由"固定池容量 M + manifest 拉取"（方案 1）和"编排 agent 按需动态组装"（方案 2）共同决定。
- 两个方案全部落在**既有原语**上：共享文件系统（ADR-018，manifest 载体）+ 固定 agent 池（`pool.py`）+ `wave -modify`（计划 §1 指令）+ 编排 agent 自主调整（计划 §2"允许 subagents / 动态调整后续 Wave"）——**无需新增 fan-out 节点等架构改动**。
- 代价：图快照中 Wave 2 形状由运行期 append 历史决定，跨 run 复现时形状随 N 变化（影响 I-22 指纹分片）；可接受，如未来需要稳定形状的可审计并行，再评估一等公民 fan-out 节点（ADR 候选，非当前范围）。

**成本模型**：云端直译整本 = 100% → 本方案 10~20%（plan 一次性 + 抽检 + 失败升级）；同领域第二本复用 plan 后趋近 0。

**机制覆盖**：模板复用（I-22）、Program（I-18）、Wave/状态机（I-19）、条件升级、成本账（I-15）。**A2A=0 的特例**——不替代 I-20 的组内通信验收。

**质量注意**：小模型"语义错误但格式通过"是命门——术语表质量、spot_check 抽检率、LoRA 微调是调节阀；verify 门槛、抽检率、池容量 M 与 chunk 上限是 I-16（HITL）的校准对象。

### A.5 对相关 issue 的落地影响

| Issue | 追加要求 |
|---|---|
| I-17 | 数据模型支持 `template/params`（graph 模板 + 输入参数 schema），同模板多实例复用 |
| I-19 | 验收标准加"翻译 mission 端到端跑通（wave1→2→3，verify 阻塞/升级路径可测）"；工具集按 A.2 契约；`wave -modify` 动态增删为必须实现的指令 |
| I-15 | 验收加"云 token 占比度量（对比基线直译）" |
| 新增 I-22 | mission 模板与结果复用（模板参数化 + 结果缓存命中即跳过） |
| I-16（HITL） | 校准对象 = 翻译质量门（术语/长度阈值 + 抽检率）+ 池容量 M / chunk 上限 |

### A.6 远期方向：SDK 装饰器实现 NOOA 模式（记入 ADR 候选，非当前范围）

当任务需要**比 mission 声明式骨架更高自由度**的编程式编排时（对标 NVIDIA NOOA 的"agent 即 Python 对象"，[官方博客](https://developer.nvidia.cn/blog/six-agent-harness-capabilities-for-higher-model-performance/)）：

- **形态**：在 SDK 层提供装饰器集，实现 NOOA 六原则的子集——`@strategy(PredictStrategy())`（控制 LLM 调用方式）、方法体为省略号 → 运行时 LLM 驱动循环、正常方法体 → 确定性 Python、类型标注即可执行合同、字段即持久状态。
- **定位**：作为 **tools 适配 DSH 的 Code Mode（`run_code` / `ctx.codeRuntime`）** 的"代码即行动"能力——程序内工具调用走命名 bindings 函数，与 mission 的确定性闸门互补，不替代 mission（mission 管"机器验收 + 成本账 + 无人值守"，NOOA 模式管"自由度 + 可编程循环"）。
- **边界**：远期；不在当前 22 个 issue 范围内。若启动，先写 ADR（NOOA 模式与 mission 图的分工边界、与 hooks 的关系），再以 `harness/sdk/` 新增装饰器模块 + TDD 推进。
