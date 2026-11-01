# python-harness 待办 → Issues 清单（py harness 执行原语）

> 来源：对话收敛的 TODO 清单（上下文修复 / hooks / 生命周期 / Stop / 混合推理 / mission / OTel）
> 转换规范：`skills/to-issues/SKILL.md`（tracer-bullet 垂直切片，HITL/AFK）
> 标签词汇：`docs/agents/triage-labels.md`；tracker 约定：`issues/<ID>-<slug>.md` + `merge-requests/<ID>.md`（见 `docs/sop/soft-serve-issue-mr-workflow.md`）
> **范围定论（2026-08-19）**：mission 的**编排层**（图声明/JIT/状态机/Wave 调度/路由/模板缓存）是 **app 层（DSH 驱动端）** 的事情；py harness = **小模型执行引擎**，只保留**执行原语**（ADR-038 框架提供原语、APP 决定调度）。
> **当前 py harness 无 active 待办**：I-20（A2A）/I-21（OTel）⏸ 暂缓（理由见条目）；I-28 ⏸ 等待 app 层（触发条件已记）；I-29/I-38 关闭；**4 项归 app 层**（I-16/17/19/22，见附录）。**已完成**：I-15/25/26/18/27、CLI 观测、I-30、I-31/I-32（09-07）、I-34（执行期强制 infra）、I-35（Policy v2）、I-37（fs_paths Cedar+guard 内核禁读）。
> **已完成并删除**：I-01…I-12（详见 `docs/archive/solved-issues-through-I-43.md` 2026-08-19 章节）。

---

## 第一层：混合推理执行原语（I-15）

### I-15 token 计量（原语） ✅ 已完成（2026-09-04 — grill 收敛：审计归 app，py harness 缩为捕获原语）
**What to build**: py harness 只做**计量原语**：`llm.call` 的 do_call 捕获服务端 usage → `set_context_fields(tokens_in/out)`。**审计落盘/预算裁决/成本模型归 app 层**（grill 决策 B）——app 在 `llm.call` around hook 中调 `next_fn()` 后经 `current_context()` 读取实测 token，自行决定落审计/路由/裁决。
**Acceptance criteria**
- [x] `llm.call` hook 内可读到 token：`_call_llm_once`→`do_call` 捕获 `response.usage` → contextvars（测试：`tests/test_llm_backend.py::TestTokenMetering`，2 测试）
- [x] HookContext 含 tokens_in/tokens_out 字段（I-09 已有；I-15 填充服务端实测值）
- [x] 口径 = 服务端 usage 原样透传；usage=None（后端不报）→ 字段保持 None，**无估算兜底**（grill 决策 C）
- [ ] **归 app**：调用级 token 记录并归因（审计可查）——由 app around hook 落审计；跨 trigger 累积/成本视图归 app / I-21（grill 决策 B/D：逐 attempt 独立，失败 attempt 无 usage → None）
**Blocked by**: I-09（hooks，已完成）、I-13（已完成）
**备注（隐性契约）**：around hook 的 ctx 在 final 前构建，`ctx.tokens_in` 恒为 None——读实测 token 必须于 `next_fn()` 之后调 `current_context()`（见 implementation guide I-15 节）

---

## 第二层：执行引擎能力（I-18 → I-20 → I-21）

### I-18 Program 执行器（SDK 执行面包装，2026-09-04 grill 收敛） ✅ 已完成（2026-09-05）
**What to build**: **确定性零 token 脚本执行节点**——不改执行引擎：Program 是 agent 类的**配置子集**（无 LLM/无推理/无工具白名单），以 **SDK 子客户端包装下层原语**表达（决策：入口 = 独立 `client.program`；输出以 session 事件为主、sandbox 可选作脚本自身写 FS 的面）。零 token 执行节点（verify/goal/数据转换）是 mission 的基础，**保留在 py harness（SDK 面）**。
**What to build（细化）**：`client.program.run(script, session_id=…, sandbox_id/config=…, timeout=…, retries=…) -> RunResult`
- 解析 sandbox 实体（可选：给了 sandbox → manager 实体/规范，脚本在其中跑、可写 FS 持久化 artifact；不给 → 无需沙箱的纯宿主脚本执行？**（实现时按最小集先支持给 sandbox 路径，见 guide）**
- 执行 = 复用 sandbox execute（按 sandbox_id）+ **新小件：重试策略 util（TimeoutGuard/RetryPolicy）**——现无通用重试（仅 I-13 llm 回退）
- 不构造 AgentRuntime/不跑 agent loop → 天然零 LLM
- 输出写 **session 事件**（program_started/program_completed + output），下游/后续 run 可读；脚本自身经 sandbox FS 写共享 artifact（ADR-018）
**Acceptance criteria**
- [x] `client.program.run` 执行确定性脚本成功/失败/超时路径，返回 ProgramResult（输出/退出码/attempts/session_id）〔tests/test_program.py 13 测试〕
- [x] 重试策略：瞬时失败按次数重试；超时/永久失败不重试（`harness/sdk/program.py` `with_retry`）
- [x] 输出写入 session 事件流（program_started/program_completed），下游可读
- [x] 可选 sandbox：给了则经 manager 实体执行（脚本可写 FS）；不给则宿主直跑
- [x] 单元（retry/host/sandbox/SDK 集成）
**Blocked by**: I-26（sandbox_id 寻址，已完成）——可立即开始

### I-20 A2A 通信原语 + 插话窗口 ⏸ 暂缓（2026-09-10：非必要，agent 已有其它通信手段）
**What to build**: 组内通信（共享文件系统 ADR-018 + `events.send` 消息注入）与插话窗口（`agent.pre_step` hook，可注入 modify/abort/skip/inject_context 消息，权限矩阵强制）。**插话裁决策略（何时注入/是否批准）归 app**——py harness 只提供通信与窗口原语。
**Acceptance criteria**
- [ ] 同组 agent 消息互通，跨组被权限矩阵拒绝
- [ ] 插话窗口可注入四种 action，目标 agent 下一步行为可被改变
- [ ] 端到端演示（app 编排下）
**Status**: ⏸ 暂缓——A2A 原语非必要：agent 间已有其它通信手段（共享文件系统 ADR-018、session events、外部服务等），且 `events.send` 与 `agent.pre_step` 插话窗口**已存在**（I-11/I-09 完成）；缺的只是「专用 A2A 消息语义」。等 app 层出现明确跨 agent 消息契约需求再评估。

**Blocked by**: I-09（hooks 已完成）、I-11（wake/worker 已完成）

### I-21 OTel 追踪与成本可视化 ⏸ 暂缓（2026-09-10：CLI 观测已覆盖；引入 server 过重）
**What to build**: 基于 hooks 的纯 observe 追踪：span 生成、contextvars 传播、沙箱子进程经 W3C traceparent 传递；导出 I-15 计量形成成本视图（按 agent/会话；mission/wave 维度归 app 编排层）。最后做，零侵入。
**Acceptance criteria**
- [ ] 调用链 span 完整（LLM/工具/沙箱/会话）
- [ ] 跨进程 trace 关联可查
- [ ] 成本视图可渲染（基于 I-15 计量导出）
**Status**: ⏸ 暂缓——**CLI 观测工具**（logs/session show/agents history，agent·human 双模）已作为落地观测面；I-15 计量原语在 `llm.call` hook 内可用（app 可自建账本）。OTel collector/server 对当前规模过重；未来若有跨服务调用链需求再启（hooks 零侵入接入点仍在）。

**Blocked by**: I-08（hooks，已完成）、I-15

---

## 第四层：SDK 执行面扩展（2026-09-04 grill 拆出）

### I-25 run/stop/status/wake 按注册实例分派（同模板多实例并发） ✅ 已完成（2026-09-04）
**What to build**: 「注册两个同模板实例、各自 run 并发跑」的公共 SDK 路径。**身份合并（决策 A）**：每个已注册实例（registry `inst_{uuid}`）拥有一个**专属懒建常驻运行时**，池键与 `runtime.instance_id` = 注册 inst_id（运行时 uuid 按实例独立——同模板两实例 uuid 互不相同）；run/stop/status/events.send 唤醒全部按 inst_id 精确定位（消除现有按 spec 名复用塌缩——`agents.stop` 可能停错同名实例、`agents.run` 第二个同模板并发被 busy 拒）。
**Acceptance criteria**
- [x] 同模板 register 两次 → 两个独立 inst_id；各自首次 run 懒建独立运行时（uuid 互异）〔`tests/test_instance_dispatch.py`〕
- [x] run(inst_A) 与 run(inst_B) 并发执行互不 busy 误伤，session/事件流各自独立
- [x] stop(inst_id)/status(inst_id)/events.send 唤醒精确命中目标实例
- [x] 运行时分派键改造：`pool.acquire` 加 `instance_id` 参数（`harness/orchestration/pool.py:62`）、`_AgentsClient.run`（`sdk/client.py:461`）、`stop`（:412）、`status`（:392）、`_wake_session`（:307）
**Blocked by**: I-23/I-24（SDK 执行面，已完成）——可立即开始（不等 I-26；沙箱沿用共享容器 + session workspace 隔离）
**Busy 语义（Q3 终确）**：同实例 BUSY 时 run 保持 raise（同步契约不变）；同实例 follow-up 走 `events.send`→wake 收件箱队列（I-11 FIFO）——I-25 不引入 run 排队

### I-26 沙箱 uuid 身份 + agent→sandbox 关联（命名空间层解耦原语） ✅ 已完成（2026-09-04）
**What to build**: 沙箱从「配置名即身份」改为「**独立 uuid 实体**」，完成 agent 与沙箱在**命名空间层**的解耦（代码/对象层解耦 ADR-040 已有；本 issue 解命名空间层）：
- SandboxManager 管理**沙箱实例 = uuid 实体**：`get_or_create` 按配置可实例化多个沙箱实例（uuid 互异），不再单例焊死配置名
- **agent→sandbox 关联机制（本 issue 本体）**：agent 实例侧持 sandbox uuid 引用——默认规则：`spec.sandbox` 配置名 → 懒建/解析到一沙箱实例（对外行为不变）；支持显式查询/指向。agent 调用的确是其指向的沙箱
- 沙箱实体生命周期按 uuid 管理（TTL/锚点/reap 对齐 ADR-040），独立于任一 agent
**Acceptance criteria**
- [x] 同一配置可创建多个沙箱实体（uuid 互异）；实体以 sandbox_id 查询/管理（create()/create_new）〔tests/test_sandbox_entity.py〕
- [x] agent 关联到 sandbox uuid（默认规范实体 + register sandbox_id 显式绑定），与配置名解耦
- [x] 沙箱实体生命周期按 uuid（respawn 同 uuid；agent stop ≠ 销毁；destroy(sandbox_id)；ADR-040 §5 注记）
- [x] SandboxManager 实体化 + SandboxInterface uuid 化 + pool/AgentRuntime 关联字段（_sandbox_id）改造
**明确不做（后置 → I-27）**：共享/隔离策略、组策略、动态重绑
**Blocked by**: ADR-040（生命周期按 uuid）对齐
**Related**: ADR-018（组共享——策略层消费本原语，见 I-27）

### I-27 策略中间件（gateway/hooks 定向规则 + preset 规则集） ✅ 已完成（2026-09-05，grill 2026-09-04/05 定稿）
**What to build**: **gateway 层定向策略中间件**——基于既有全局 `authorize.veto/observe` + `tool.execute` hooks（ADR-042 §8），按 `ctx.spec.name` 定向约束**特定 agent**，纯新增不改现有代码。机制（py harness）+ 内置 4 preset（内容亦入 py harness）；**一切默认关**，app 两通道显式开启（`client.policy.enable(name, agent=…|“*”)` 或 `AgentSpec.policy` 声明 register 自动 enable）；harness 缺的规则经 `client.policy.add` 插入。
**内置 preset（默认停用，enable 时 app 给 cfg）**：
- `bash_whitelist`：危险命令通配 deny（rm -rf /、mkfs、dd of=/dev/、curl|sh 等；cfg.allow）
- `fs_paths`：系统目录 deny（/etc /proc /sys /dev /boot /root /run /usr…；cfg.allow）
- `read_before_edit`：DSH 式版本化（observed-version；hash 系纯参数门 hashread→hashedit old_hash；literal 系 cfg.digest_provider sha256 适配器，无则 seen fallback；角色/参数映射在 cfg 与工具零耦合）
- `net_policy`：目标 deny（cfg.deny IP/CIDR/域名，命令级；host 模式沙箱内 127.0.0.1=宿主机）
**Acceptance criteria**
- [x] 默认全关：未 enable → effective(agent)=∅，既有全量 582 测试全绿（零行为变化）
- [x] enable 定向：enable(n, a) 只约束 a；agent="*" 全量；disable 撤销
- [x] spec.policy（str|list）声明 → register 自动 enable（未知名 warn-ignore）
- [x] 4 preset 行为（tests/test_policy.py 16 测试：engine/定向 hook/bash/fs/read_before_edit 版本化/net_policy/client.policy）
- [x] client.policy add 自定义规则（agent 定向必填）；未知 enable raise
**Blocked by**: I-26
**注**：原「沙箱绑定策略层」（共享组/隔离/动态重绑，基于 I-26 实体关联）与网关命令/FS 策略是**两个面**——前者（沙箱实体绑定面）暂记 I-28 草稿，本 issue 落地后者（授权/工具策略面）

### I-28 沙箱实体绑定策略面（共享组 / 隔离 / 动态重绑） [draft·等待 app 层]（基于 I-26 实体关联）
**What to build**: 建立在 I-26 uuid 关联之上的**绑定策略面**（与 I-27 网关/工具策略面互补，属沙箱资源侧）：共享组（多 agent 绑同一 sandbox uuid，ADR-018 组语义）、实例隔离（各自绑不同 uuid）、动态重绑（运行期迁移）。**归属**：py harness 原语 vs app 编排——待前序闭合后 grill。
**app 层依赖 + 触发条件（2026-09-10）**：I-28 管的是**资源拓扑**（谁与谁共享同一沙箱域、何时迁移），与 PolicyEngine（行为约束）正交。「统一管理一组 agent」的分组维度（tenant/project/mission 节点/角色）由 **app 层治理定义**，harness 不先验猜测。触发引入条件：① app 出现「同一沙箱域被多 agent 共享且需集中变更/审计」的实际需求；② 或出现运行期重绑（迁移）真实场景。届时再定归属。另：policy 侧的**组作用域**（enable(rule, group=…) 或 spec 标签）同理——等 app 明确组维度后作为引擎小扩展。
**Blocked by**: I-26（已完成）

### I-29 策略后端 hybrid（Cedar/Dogwood 互补引入） ✅ 关闭（2026-09-10：Cedar 已作为静态核心落地；Dogwood 取消）
**Context**: 调研 AWS Cedar（声明式授权语言，单请求无状态，**刻意无正则/字符串算子**——官方设计文）与 Dogwood（2026-08 开源，在 Cedar 之上治理 agent 工具调用**序列**）后确认：动态检查（bash 危险通配正则、fs 路径解析、read_before_edit 的 observed-version digest、session 时序状态）**必须留在 Python host**，作为 context 喂给策略层——**hybrid 互补，非整体替换后端**。
**What to build（候选，未定优先级）**:
- PolicyEvaluator 决策 seam：`decide(agent, gate) -> str|None` 接口 + PolicyEngine 可注入 evaluator（默认内置 Python preset 链）——为声明层后端预留
- Cedar/Dogwood 作为**声明式结构决策组件**（agent/action/resource 矩阵 + forbid 优先 + 序列约束）接入 seam；host 动态链保持默认
- 词汇表草案：principal=spec.name、action=tool/gate、resource=path/host/url、context=args+session+宿主预计算事实
**Acceptance**: seam 存在且默认行为不变（python 引擎）；声明后端 head-to-head 输出一致后可切
**Status**: 关闭——Cedar 已落地为**静态核心**（I-35：cedarpy 集成、per-rule 定义单元、check_static/deny-wins/NoDecision 语义、guard 编译、runtime 接线）；**Dogwood 取消**（无 python 包；序列/有状态由 veto overlay 承担，见 ADR-048 §7-9）。残余「声明后端 head-to-head / PolicyEvaluator seam」不再需要。

**Blocked by**: I-27（已完成）

### I-30 上下文压缩触发策略 ✅ 关闭（2026-09-06：折叠链路已具备，非 harness 缺口）
**核实**：`build_messages` 已内置 `memory_summary` 折叠契约（`prompt_assembly.py:128-178`——`after_event_id` cutoff、不计预算、插 task 锚点后）；生产侧成套 helpers 已存在（`client.memory.tail/compress/inject_summary/estimate_tokens`，`sdk/client.py:1292-1424`）。**80% 触发 = app/scenario 策略**：`agent.pre_step`（先于 build）里 estimate_tokens ≥ window×ratio → tail→compress→inject_summary。留待长上下文场景验证（app 侧 pre_step 策略示例）。

### I-31 Session 去状态机化（无状态 append-only 日志 + agent 持有派生） [backlog]（2026-09-07 grill 收敛）
**Context**: 运行链路证据——AgentRuntime 只 `store.append_event` + 自建逻辑视图；`SessionManager` 状态机（status 推断/transition 校验）仅在管理面展示与 gate 落盘被用，且 `_TERMINAL_SUCCESS={job_completed}` 是 ADR-038 删 DAG 后的死语义（生产无 job_* 发射者）→ 真实 agent 会话永远 RUNNING。
**决策（grill 2026-09-07）**:
- Session = **无状态 append-only 日志**（store 为唯一事实源）+ 绑定记录（metadata.instance_id 保留不删）
- **持有关系 = 派生**：从 pool/registry 查绑定实例——实例存在且 running/idle → 持有（CLI 显示 holder instance + status）；stopped/disposed/不存在 → **unbound**（物理解绑/元数据迁移均不做，Q2-b）
- **rewind/fork/checkpoint 保留**（日志级运算，非状态）；执行守卫以**派生持有状态**为事实源：绑定实例 RUNNING 时禁止（agent 在生成/tool_calling 轮中），IDLE/unbound 放行
- `stop()`（STOPPED+dispose）/ idle-timeout / pool destroy 后 → 派生为 unbound（日志保留）
- 移除 `SessionManager` 状态机（`_infer_status`/`_validate_transition`/内存 map）；gate_manager 直连 store；SessionsClient show/events/rewind/fork 适配 store 级操作
- CLI agent JSON 增 `holder_instance_id`/`holder_status`；人类 md 同
- SessionStatus 枚举/推断的处置与 `tests/test_session.py` 重写为日志语义在实现时定（可能删 RUNNING/COMPLETED 语义或仅作展示 tag）
**Blocked by**: I-11（wake/续跑需保持，派生持有不破坏）
**Related**: ADR-039（任务容器）、ADR-043（agent 生命周期=权威状态）、ADR-037（append-only）——实现时补注记

### I-32 Agent 绑定历史图（session×sandbox 二部图 + 按名查询） [backlog]（2026-09-07 grill 收敛）
**Context/决策**: 见 ADR-049 §2——边=(agent, instance_id, session_id, sandbox_id, first_ts, last_ts)；同三元组重绑合并 last_ts（保留 first_ts）；存储于 session meta.binding_history；SDK agents.history_sessions/history_sandboxes/history_bindings（agent_name 必填，bindings 限该 agent 该 session）；CLI agents --name A --history_sessions/--history_sandboxes/--history_bindings --session SID（双模输出）；DRT(sqlite reference 多种子) 验证复合查询。
**Status**: 实现进行中（store.update_session_metadata、run/wake 记录边、三查询 API、CLI agents、DRT 已绿；ADR-049 已建）。

### I-33 工具执行期策略层（pre-call → tool-called → tool-exec 链） [backlog]（2026-09-09 grill 收敛）
**Context/缺口**：现有 policy（I-27/ADR-048）veto 挂在 Gateway.authorize（pre-call 参数门）——只约束「模型提交的工具参数」（字段匹配：fs_paths 不含 bash 即因通配可绕；net_policy 命令文本解析；read_before_edit 版本锚）。但**实际 fs/net 访问只有工具执行瞬间才发生**：pre-call 授权后到 sandbox syscall 之间无策略层。链条应为 pre-call(参数/版本门) → tool_called(事件) → [执行期策略点] → tool-exec。
**决策（grill 2026-09-09）**：
- 执行期强制与观察属 **policy 基础设施**，接口挂在 sandbox.impl（backend 无关）：restrict_paths(allows)/restrict_net(allows)/access_events()；policy/gateway 把生效规则**编译**为这些调用，内核执行（不可绕），审计事件回填 policy（observe 面）
- 机制矩阵（分工）：fs 强制=Landlock 路径白名单；net 强制=netns+nftables allow 集（network=host 时收窄）；兜底=seccomp-bpf（危险/逃逸 syscall 黑名单）；观察=eBPF/audit 采真实 open/connect（policy 事实面、规则编写与漂移验证）——bwrap 只负责 namespace 启动，不承担上述
- 观测不是拦截前提：静态内核强制（Landlock/netns）无需先观测；观测服务于规则编写+验证；动态逐调用仲裁（seccomp USER_NOTIF）留作可选
- 重隔离后端（gVisor syscall 截断 / Firecracker 设备层）= 同一 policy 接口的不同 backend（本 issue 不做，仅记录）
- **范围收敛（2026-09-09）**：无观察层——Landlock/seccomp **拦截事件即审计**（拒绝 → 命令 EACCES/EPERM 自然落在 tool_result/session）；观察层（python-ptrace/eBPF）移除出 active 范围（host-attach 实测 EPERM、TRACEME 不稳、eBPF 特权）留未来可选
- **失败语义**：Landlock 规则装配失败/内核缺失 → **fail-closed**（拒该次工具调用）
- **落地形态（2026-09-09 确认）**：Landlock = 沙箱内一次性 C launcher（execguard，host 预编译 RO bind，非 daemon）；seccomp = host mephi42-seccomp export_bpf → bwrap --seccomp FD；装配失败 fail-closed；**尚未集成**——先以 tests/test_i33_execguard_feas.py 验证链路
- **实现候选包**：Landlock=landlock(Edward-Knight)/py-landlock（或在沙箱内跑预编译 C helper——已实测可用、零 py 依赖）；seccomp=python3-seccomp（host 侧 export_bpf → bwrap --seccomp FD）
- 网络细粒度：不实现（network none/host + net_policy 文本前置）；特权后端候选记录待隔离后端
**Related**: ADR-048（policy-middleware）、ADR-051（执行期强制架构）、I-27/I-28、sandbox.impl

### I-34 沙箱执行期强制基础设施（Landlock+seccomp） ✅ 完成（2026-09-09，承接 I-33/ADR-051）
**What**: `SandboxRestriction`（沙箱内绝对路径+读写/`syscalls` deny 集）契约入 interfaces；`sandbox.impl execute(pid, cmd, restrict=…)` 落地——bwrap 装配：Landlock 一次性 C launcher（host 预编译 RO bind /opt/harness/execguard，spawn→restrict→execvp）+ host mephi42-seccomp `SyscallFilter.export_bpf` → `--seccomp FD`（先于 launcher 覆盖整树）；Landlock 装配失败/内核缺失 → **fail-closed**（该次调用拒绝，不执行）。execguard 源入 `harness/sandbox/execguard.c`（或包内）。拒绝即审计（EACCES/EPERM 自然落 tool_result）。
**Status**: ✅ 已实现（commits c791c77/5c94610/3023c54）——SandboxRestriction 契约；execguard.c/.py（懒编译 C launcher + mephi42-seccomp BPF）；execute(restrict) + fail-closed；docker 拒绝；测试 14 + 全量 606 passed。

**AC**: execute(restrict) 端到端（Landlock 窄于 mount；seccomp 拒 socket）；fail-closed 路径测试；无 restrict 时行为不变（既有沙箱测试全绿）。
**Blocked by**: None（可行性测试 tests/test_i33_execguard_feas.py 已绿，2 passed）。

### I-35 Policy v2：Cedar 静态核心 + veto overlay + guard→execute 接线 ✅ 实现完成（2026-09-09；HITL 决策已拍板，I-36 合并入此）
**What**: 规则定义单元 `{cedar?, guard{paths{rw,ro}, syscalls_deny/allow}, overlays[veto名]}`；engine 沿用现有 enable/agent-scope 模型（默认 OFF、spec.policy 自动 enable、可 "*"）。执行链：**cedar 静态决策**（permit/forbid；NoDecision=放行+warn，域由作者铺开）→ **overlay veto**（python，动态/有状态）→ **guard 数据直接编译 SandboxRestriction**（无翻译层：guard 段即内核边界，不从 cedar permit 反推）→ runtime 调 `sandbox.execute(restrict=…)`；拒绝/fail-closed 落 tool_called allowed:false + 审计。python-only 规则 = cedar 空 + overlay（统一模型自然子集，无需独立兼容层）。最小 schema：principal=Agent；action=Action(tool 级)；resource=File{path}/Cmd{text}；context=tool_args。文档修订：ADR-048 §5、ADR-051、I-29 backlog。
**AC**: ①cedarpy 静态评估集成（含 like/NoDecision/批量）②定义单元与 enable/add 通道 ③runtime 端到端一次工具调用（cedar 决策 → overlay → guard 限制在沙箱实际生效；拒绝事件可见）④guard 编译正确性单测 + e2e（借 I-34）⑤python-only 规则行为不变。
**Status**: ✅ 已实现（S1-S5 commits a765727/a9ad379/849c4c7/256c8c5/c6929aa：check_static/通道/compile_guard/veto 静态先行+runtime pending 注入/内核 e2e）。Cedar 要点：条件用 when{} 子句、实体对象 uid {type,id}、未匹配=隐式 Deny（以 diagnostics.reasons 判覆盖实现 NoDecision 语义）。遗留细化：deny reason 注入 agent 上下文的 tool 反馈化（继续循环自纠）。

**Blocked by**: None（可与 I-34 并行；restrict 已就绪）。

### I-37 Preset 静态 Cedar 化 🔶 部分完成（2026-09-09：fs_paths ✅；bash_whitelist 决定保留纯 veto overlay）
**What**: 纯静态 preset 重写为 Cedar：fs 路径域（workspace allow+RO sys）、bash 危险模式、escape syscall deny；+ 翻译正确性测试（Cedar 文档 → 期望 SandboxRestriction/决策）。
**Status**: fs_paths ✅ 已适配（cedar File+Cmd forbid + guard.sys_read_deny 内核禁读，修历史 bash 绕过；commit 351a0c0，含内核 e2e）；escape syscall 由 guard 数据承载（按需）。**bash_whitelist 决策：保留纯 veto overlay**（正则 `rm\s+-[a-z]*[fr]\s+/`、`curl|sh` 等 Cedar like 无法表达，ADR-048 host-side 原则）；定义单元形态 = cedar=None + veto + cfg（已测）。

**AC**: 三条 preset Cedar 文档 + 决策与 guard 编译测试绿。
**Blocked by**: I-35。

### I-38 Preset 动态 Cedar+veto 化（read_before_edit、net_policy） ✅ 关闭（2026-09-09：现有实现即 veto overlay 子集，无功能增量）
**What**: read_before_edit（Cedar 放行 edit 写域 + veto overlay 时序/版本）与 net_policy（Cedar 静态域 + veto 端点文本 overlay）重写。
**Status**: 关闭——read_before_edit/net_policy 本属定义单元 veto overlay 子集（有状态/端点文本；Cedar 无正则且无状态），严格 Cedar 化无功能增量。保持 cedar=None + veto + cfg（与 bash_whitelist 同）。

**AC**: 两条 preset 重写 + 序列/端点行为测试（既有 test_policy 用例迁移）。
**Blocked by**: I-35。

### I-39 任务 prompt 在 runtime 被截断为 200 字符 ✅ 修复完成（2026-09-19）
**What（事实）**: `agent_runtime/impl.py:956` — `payload["input"] = str(task_input)[:200]`；而 `_agent_loop` 唯一的信息来源是事件流（`_resume_events` → `build_messages`），因此**模型收到的 Task 永远只是前 200 字符**。同处 `impl.py:967` checkpoint 的 `prompt` 也被截断到 500（P0-4 恢复路径隐患）。
**实证**: 真实会话 `f3ecc88f` task2 的 `agent_started.input` 长度 = 200，尾部停在 "…via echo with a heredoc. Then verify with bas" —— 后续指令（grep 校验、`FILE_WRITTEN` 结尾要求）从未到达模型。此前若干「模型不听指令」的判断因此需要复核。
**建议修法（待确认）**: (a) 取消 runtime 截断（事件存全文）；(b) 全文 + 另存 `input_preview[:200]` 供 CLI/摘要（推荐）；(c) 仅 CLI 展示层截断。checkpoint `prompt` 建议同批取消截断。
**顺带发现**: `_SessionsClient.stop`（`sdk/client.py:678`）按 `spec_name` 取池内第一个 RUNNING 实例（`list_instances(inst.spec_name)`），多实例同 spec 时可能停错实例——建议用 session metadata 的 `instance_id` 精确匹配。
**Status**: ✅ 已修复（TDD）——① agent_started.input 存全文（Task: 即全文）；② checkpoint prompt 存全文；③ 新增 RunResult.instance_status（返回瞬间实例状态快照，completed/stopped -> idle）消除与 run 结果字段的歧义；④ 顺带修复：异常分支未把实例放回 IDLE（失败任务永久占用 running 且跳过 lifecycle stop）——统一走 _finish_run；⑤ CLI 显示层改为中间截断（head+tail，宽度可配），审计日志维持现状（session 已存全文）。全量回归 677 passed。

**Blocked by**: None（TDD 可立即修）

### I-40 工具路径权限前置检查误拒「挂载根」 ✅ 修复完成（2026-09-19）
**症状**: 授予 file_access {<host_ws>/: read_write} 的 agent 执行 cd /ws && ... / ls -la /ws 被拒：
"Access denied — path '/ws' not in agent permissions"——整条命令未执行，模型被迫改道 /tmp（会话 400f4e59 实证）。
**根因（两层，均属工具前置检查，非内核/非 Landlock）**:
1. file_io._check_access 用朴素 startswith：授权键为 host_ws/（带尾斜杠），挂载根解析结果为 host_ws（无）→ False → 拒；子路径 /ws/x 反而通过。
2. sandbox_exec._extract_paths 用 shlex.split 不拆 shell 元字符 → ls -la /ws; 抽出粘连 token，同样被拒。
**修复（TDD）**: ① _within(path, prefix) 边界感知判定（rstrip 斜杠后比较：根放行、/ws-other 仍拒、键带/不带尾斜杠等价）；② 抽取前把 ; & | ( ) < > 与换行规范为空格（仅用于抽取，不改执行）并 strip 引号。
**验证**: tests/test_i40_path_permissions.py 7 例（含工具层回归：引用挂载根的命令必须被执行）；最小回路复跑全 OK；相关套件 60 passed。

### I-41 「逻辑视图」读面契约未落实 ✅ 修复完成（2026-09-30，方案 A′）
**现象（已确定性验证）**: 会话执行 rewind 后——
  physical: [agent_started, agent_thought, tool_result, agent_thought, agent_completed, session_rewound]
  logical (SessionManager.wake): [agent_started, session_rewound]   ← 已截断
  runtime (_agent_loop 实际输入): 与 physical 相同                    ← 未截断
**根因**: ADR-050/I-31 声明「每次读取都重建逻辑视图」，但 **SessionManager._rebuild_logical_events 除内部外无调用者**；
runtime 在 impl.py:352 直接用 store.read_events（物理日志）。全仓直接读物理日志处 ≥12（runtime 1 + SDK client 11）。
**衍生问题**: `_resume_events`（剥离缺 tool_result 的 dangling 工具轮）本质是**读时投影**，却实现在 runtime 内；
与 SessionManager 的逻辑视图重建属同类职责 → 读面契约被两个模块各切一半。
**影响**: app 用 rewind 实现「回到某点重来」对模型行为无效（fork 不受影响，因 fork 物理复制逻辑视图）。
**候选修法**:
  A. 统一读面：SessionManager 提供 view()（rewind 重放 + resume-safe 归一），runtime/SDK 一律走它。
  A'.（推荐）分层：SessionManager.logical_view()（日志层：rewind 重放） + prompt_assembly.assemblable()（提示词形状层：剥离 dangling 工具轮）；runtime 只组合二者。
  B. 最小改动：runtime 先取 SessionManager 逻辑视图，再保留本地 _resume_events。
  C. 明确 rewind 仅用于观测（文档化），不接入模型上下文。
**Status**: ✅ 已修复（diagnose → TDD，提交 3b43dbb）——`session/impl.py` 新增模块级 `logical_view(physical)` 与 `SessionManager.logical_view(sid)`；`_resume_events` 迁至 `prompt_assembly.assemblable()`（提示词形状层），runtime 改为组合 `assemblable(logical_view(store.read_events(sid)))`。**TDD 期间追加发现**：assemblable 原按「最后一个 agent_started 之后的 tail」划界，导致跨任务遗留的 dangling 工具轮不被剥离 → 改为全量扫描。验收：回路 `model saw OLD? False`；ADR-050/039/034 已同步注记；全量回归 689 passed。

**Blocked by**: 需 app 层确认 rewind 语义（是否要求对模型生效）——ADR-050 已声明契约，故倾向 A'。

### I-42 SDK 读面仍消费物理日志 ✅ 修复完成（2026-09-30，方案②读面二分）
**现象**: I-41 修复后 runtime 已消费逻辑视图，但 `sdk/client.py` 仍有 11 处 `store.read_events`：
`_terminal_output`（RunResult.output）、`sessions.show`、holder 派生、`agents.history_*`、prompt preview 等。
**影响**: rewind 后这些读面仍显示被回退的内容（已验证：rewind 后 RunResult 输出仍为旧轮内容）。
**候选修法**: ① 全部改走 `SessionManager.logical_view()`（一致但 rewind 会“隐藏”历史，审计需另走物理读）；② 明确区分「审计读面（物理）」与「语义读面（逻辑）」，在 API 上显式命名；③ 维持现状并文档化。
**Status**: ✅ 已修复——读面显式二分：**语义读面** `HarnessClient._logical_events()`（10 处 SDK 读点全部迁移）+**审计读面** 新增 `sessions.audit_events()` / `sessions.audit_path()`；CLI `harness logs` 默认语义、`--audit` 看物理日志。日志层新增 `logical_view_of(store, sid)` 供 runtime 组合（物理读点仅剩 `session/impl.py` 与 `audit_events`）。守卫：`tests/test_i42_read_faces.py` 含**源码扫描型 fitness 测试**（物理读点越界即失败）+ 语义/审计对照测试。ADR-050 增读面二分注记、ADR-049 增观测注记；全量回归 695 passed。

**Blocked by**: app 层需确认 rewind 语义（观测用途 vs 语义用途）——建议 ②。

### I-43 组装产物预览（读面三分之三） ✅ 实现完成（2026-09-30）
**目标**: 让「发送给 LLM 的上下文」成为可查询的**读面**，与 runtime **单源同函数**，且只读。
**实现**:
- 单一真源 `harness/prompt_preview.py: compose_llm_context(store, spec, session_id, system_prompt=?, task=?, event_id=?, overrides=?)`
  （物理日志 → logical_view → [历史前缀 | 合成 task] → assemblable → build_messages → hooks）；
- `AgentRuntime.llm_messages()` 包装之，loop 改为**只消费**该函数（删除本地 `_build_messages`）；
- SDK 新增 `sessions.preview_llm_messages(session_id, instance_id, event_id=?, task=?, system_prompt=?, overrides=?)`（**必填 instance_id**，未知实例显式报错；只读：不落盘/不 acquire/不调 LLM）；
- CLI 新增 `harness preview <instance_id> --session <sid> [--event-id E | --task T] [--json]`；
- `build_messages(..., meta_out=...)` 回传 `truncated / dropped_event_count / folded_summary`。
**测试**: tests/test_i43_preview.py 6 例（含**单一真源一致性**：以该轮 agent_started 为界的预览 == runtime 实际发送的 messages 逐字节相同；只读性断言）。
**回归**: 全量 700 passed（1 例真 LLM hashline flake，单跑通过）。
**Blocked by**: None

---

## 附录：归 app 层（DSH 驱动端编排）的 mission 项

> 以下 4 项**不在 py harness 实现**——mission 的图声明/JIT/状态机/Wave 调度/路由策略/模板缓存是 **app 层（DSH 驱动端）** 的编排逻辑。py harness 只提供执行原语（I-15/18/20/21）与 hook 触发点（`llm.call`/`agent.pre_step`/`agent.lifecycle`/`agent.stop.*`）。原 issue 设计细节保留在 git 历史（`d10d490` 之前版本）与 `mission-control-implment-plan.md`。

| 原 issue | 内容 | 归 app 后依赖的 py harness 原语 |
|----------|------|--------------------------------|
| **I-16** 路由策略（云监工/本地工人）[HITL] | 什么任务给本地小模型、verify 严度、升级阈值——**人工校准**（成本 vs 质量权衡） | `llm.call` around 点（已就绪）+ I-15 计量 + I-13/14 双后端 |
| **I-17** mission 数据模型 + JIT 编译器 | MissionGraph（统一 agent 节点/扁平图/wave）+ JIT 校验（环/双根/缺父/深度/hook 校验点可插拔） | 执行 API（I-23 run/wake/stop）+ hooks |
| **I-19** mission 状态机 + Wave 调度 + 工具集 | PENDING→READY→RUNNING→终态、Wave 定义/verify/阻塞、`/mission` 契约（POST /mission、status、verify|interject|stop、result） | I-11 wake、I-18 Program、I-20 A2A、I-12 stop、I-15 计量 |
| **I-22** mission 模板与结果复用 | 模板参数化（同图不同输入）+ 结果缓存（指纹+输入 hash）；**模板参数化由 Jinja 承担**（ADR-045），结果缓存仍自研 | I-23 执行入口 + I-21 观测 |

---

## 依赖关系速览（py harness 当前待办）

```
第一层：✅ I-13/I-14 → ✅ I-15 token 计量原语（2026-09-04 完成，审计归 app）
第二层：I-18（Program）独立；I-20（A2A/插话，依赖已满足）；I-21（OTel，依赖 I-15 ✅）
第三层：✅ I-23 client.run + I-24 装配收敛（已完成）
第四层（2026-09-04 新增）：I-25 run 按注册实例分派（依 I-23/24 ✅，可立即开始）；I-26 沙箱 uuid 身份+关联（独立，需 ADR-040 对齐）；I-27 沙箱绑定策略层（依 I-26）
```

## 备注

- **HITL**：I-16（路由策略）归 app 层后仍需人工校准，但**不再阻塞 py harness**。
- **已完成 I-01…I-12 + I-23**（2026-08-19），记录见 `docs/archive/solved-issues-through-I-43.md`；实现细节见 `python-harness-implementation-guide.md`。
- **范围定论**：mission 编排 = app 层（`mission-control-implment-plan.md` 供 app 实现参考）；py harness = 执行原语（本清单）。
- **I-12 依 ADR-044**（双轨 + §5 reason 谱系）；**I-15 依 ADR-042 §8**（计量在 `llm.call` hook 内，hook 全局、业务实例隔离）。
- **I-23/I-24（SDK 执行面）**：公共 SDK 只有管理面没有执行面（`client.run` 缺失），装配引导散落 CLI/私有 `_pool`。

---

## 2026-10-19 同步后遗留问题登记（项目级）

> 来源：A–H + I-59 同步与 Phase 1–4 收口后的最终审计；详见各 issue/ADR。

### L1. `2>/dev`：`sys_read_deny` 与 `/dev` 冲突（I-69，open）

- 现状：`fs_paths`/`sys_read_deny` 默认禁用；启用会把 `/dev` 从 execguard 自动 RO 集**精确排除**，
  导致 `2>/dev/null` 被 Landlock 拒绝（`FS_RO` 无 write_file）。
- 方案（I-67 评估推荐 A）：排除 `/dev` 后显式以 **rw** 回补 `/dev/null`（必要时
  `/dev/zero`、`/dev/urandom`、`/dev/tty` 单独论证）；`/dev/mem` 等敏感节点仍 deny。
  依赖 Landlock 文件级 `PATH_BENEATH`（需内核 e2e）；不支持则回退 C（wontfix）。
- 参考：ADR-051（2026-10-19 评估段）、`issues/I-67-sys-read-deny-dev-conflict.md`、
  `issues/I-69-dev-null-reallow.md`。

### L2. benchmark 测试集缺失（未立项）

- 仓库**没有** `benchmarks/`、`perf/`、`eval/` 或 benchmark 测试；`results/` 为忽略的运行产物。
- 现有 `tests/` 是单元/集成/e2e（996 用例），不能替代 benchmark。
- 若要建：需定义任务集、指标（成功率/步数/tokens/耗时/压缩触发）、runner
  （fake LLM + 真实 DeepSeek）、基线结果；建议单独立项。

### L3. 安全：仓库中已提交的 key 文件

- `docs/harness_doc/deepseek_keys_for_test` 自首个提交（`2e418b0`）起被 git 跟踪，
  形如真实 `sk-` key（36 字节；与当前 `examples/.keys/auth` **不是**同一把）。
- 建议：立即**轮换该 key**；从 HEAD 删除并清理 git 历史（filter-repo/BFG）；
  加 ignore 规则（如 `**/deepseek_keys_for_test`）。

### L4. 已知非 hermetic 测试（偶发，单跑通过）

- `tests/test_e2e.py::TestHashlineLLMIntegration::test_llm_edits_python_file_with_indentation`（真实云端 LLM）
- `tests/test_sandbox.py::TestFirewallPropagation::test_host_network_allows_connections`（httpbin 网络）

### L5. 已延期事项

- **Phase 5**（全局真源/仓库角色裁决）暂缓；本地工作区与 OH 的后续同步策略未定。
- `education-agent-skills/`（独立仓库）、`harness/config/models.yaml`（部署配置）按用户决定
  暂不纳入版本控制。

### L6. 已确认结论：工具输出长度限制（e2e，I-72）

- **App 层**（`pi/tools/common.py`）：`MAX_OUTPUT_CHARS=50_000`（bash/read/grep/glob 经 `cap()`）；
  `MAX_READ_LINES=2000`、`MAX_GLOB_RESULTS=1000`、`MAX_GREP_LINES=200`。
- **Harness 层**（`harness/prompt_assembly.py`）：`TOOL_RESULT_LIMIT=8192` 字符/每 `tool_result`
  消息；装配时 `_event_messages` 再截断（第三方工具兜底）；harness 内置工具在工具层先截断。
- **e2e 实测**（`tests/test_pi_runtime.py::test_bash_output_*` / `test_read_*`）：
  bash 输出 200,000 字符 → 事件存储 **50,029** 字符（app 上限）；装配进 prompt 的 tool
  消息 **8,229** 字符（harness 上限）。⇒ **模型实际收到的上限是 harness 的 8192（+marker）**。
