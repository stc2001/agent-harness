# ADR-051：工具执行期策略层（pre-call → tool-called → tool-exec）

## Status

Accepted (2026-09-09)

## Context

模型只在「调用工具 → 自动执行指令」时才产生真实风险；sandbox 以白名单执行域（挂载/网络域，域外默认 deny）圈定边界。policy（ADR-048）的 veto 挂在 Gateway.authorize——**pre-call 参数门**，约束的是「模型提交的工具参数」（fs_paths 路径字段、net_policy 命令文本、read_before_edit 版本锚），本质是字段匹配，**无法约束实际 fs/net 访问**（bash 通配/变量/转义可绕；真实访问仅在执行瞬间发生）。故链条 `pre-call → tool-called → tool-exec` 在 tool_called 与 tool-exec 之间缺少策略点。

## Decision

### 1. 策略作用链（三阶段）

`pre-call`（现 ADR-048 veto：参数/版本/顺序门）→ `tool_called`（事件已发，allowed 标记）→ **`exec-stage`（新）** → `tool-exec`（sandbox syscall）。exec-stage = 把已生效规则编译成对沙箱执行期原语的调用，在工具进程真正触碰 fs/net 前由内核强制。

### 2. 执行期强制与观察 = policy 基础设施（backend 无关）

- 接口挂 **sandbox.impl**（bwrap/docker 共用抽象）：`restrict_paths(allows)`、`restrict_net(allows)`、`access_events()`；bwrap 仅承担 namespace 启动（mount/网络），不提供这些（无 post-spawn API）。
- **机制矩阵**：fs 强制 = Landlock 路径白名单（bash 通配亦不可绕）+ **sys_read_deny**（allow-list 无法「减除」，故 execguard 装配时以 `roex` 从自动 RO 排除指定目录 → 内核 EACCES，实现禁读系统目录）；net 强制 = netns + nftables allow 集（network=host 时收窄出访）；兜底 = seccomp-bpf（危险/逃逸 syscall 黑名单）；观察 = eBPF/audit 采集真实 open/connect 事件流。
- **观测不是拦截前提**：静态内核强制无需先观测；观测提供规则编写所需事实与强制后漂移验证（反馈闭环）。
- 动态逐调用仲裁（seccomp USER_NOTIF、gVisor syscall 截断、微 VM 设备层）为未来可选后端，同一 policy 接口、不同 backend。

### 3. 职责不动

- sandbox 仍是「真边界」（白名单执行域）；policy 是决策者（编译规则）；gateway/hooks 是通道；pre-call veto 保留（约束模型行为契约）。

## Consequences

- 正面：执行期不可绕（内核）；实际访问有事实面（审计）；规则可同时约束参数与实际行为；backend 可演进。
- 代价：需要内核能力探测（Landlock ≥5.13、netns、BPF 权限）；helper 进沙箱方式；新增 exec-stage 编译路径与事件接线；实现分阶段（见 I-33）。

**2026-09-09 I-37 注记**：fs_paths 适配为策略定义单元——cedar（File 路径 + Cmd 文本 forbid）+ guard.sys_read_deny（Landlock 排除自动 RO → 内核级禁读，bash 同样被挡；execguard roex 装配）。bash_whitelist 保留纯 veto overlay（正则不可 Cedar 化）。

**2026-09-09 实现注记（I-35 Policy v2 已落地）**：规则定义单元 {cedar?, guard, description?, reason?}；check_static（对象 uid 实体 + when{} 子句；未匹配=隐式 Deny → 以 diagnostics.reasons 判覆盖实现 NoDecision=放行+warn）；compile_guard 合并 guard→SandboxRestriction（同路径 rw 胜）；veto hook 静态先行（deny-wins over overlay）；runtime 工具执行前 compile_guard→pending→注入 execute(restrict)，guard 编译失败 fail-closed。测试 16 含内核 e2e。

**2026-09-09 分层简化注记（Policy v2）**：规则定义单元 `{cedar?, guard{paths, syscalls}, overlays[]}`；**Cedar 只做静态决策**（allow/deny/NoDecision=放行+warn），**veto overlay** 做动态/有状态，**guard 数据段直接编译 SandboxRestriction**（无 cedar→restriction 翻译层、无 python 兼容层——python-only 规则=cedar 空+overlay 的自然子集）；执行链 cedar→overlay→guard 编译→execute(restrict)。原 I-36（接线）并入 I-35；preset Cedar 化分 I-37（静态）/I-38（动态）。

**2026-09-09 落地形态（C launcher + host seccomp，经确认）**：Landlock 应用 = 沙箱内**一次性 C launcher**（非 daemon）：bwrap 首进程 = execguard（open allow 路径 → landlock_restrict_self → execvp 用户命令），host 侧预编译、RO bind 进沙箱（如 /opt/harness/execguard），零 py 依赖、每调用微秒级；seccomp = host 侧 mephi42-seccomp（PyPI 2.5.6 已装）SyscallFilter.export_bpf 生成 BPF → bwrap --seccomp FD（launcher 之前装入，覆盖整树）。装配失败/内核缺失 → **fail-closed**（该次调用拒绝）。集成前先以可行性测试验证链路（tests/test_i33_execguard_feas.py）。

**2026-09-09 范围收敛注记**：执行期层**只做强制，不做观察**——Landlock/seccomp 的拦截（EACCES/EPERM 落入 tool_result）即审计记录；观察层（python-ptrace/eBPF）移除出范围（host-attach EPERM、TRACEME 不稳、eBPF 特权），保留为未来可选。**Landlock 规则装配失败 = fail-closed**（拒该次调用）。候选实现：Landlock=landlock/py-landlock 或预编译 C helper（已实测）；seccomp=python3-seccomp export_bpf → bwrap --seccomp FD。

**2026-09-09 环境实测注记（本机 kernel 6.12 / LSM=capability,landlock,lockdown,yama,bpf / bwrap 0.11）**：
- `restrict_paths`：**可行且已验证**——mount 层已白名单（/etc 等 RO）；Landlock gate helper（gcc 预编译，spawn 后 restrict 再 exec，无额外特权）实测可把「/workspace 挂载 rw」收窄到「仅 /workspace/sub 可写」（父目录 EACCES），比 mount 更细。注意点：handled 掩码需限 15 位、需 RO 挂载 /usr//lib64//bin 供动态加载、helper 需与沙箱镜像同放。
- `restrict_net`：**非特权不可行**——bwrap `--unshare-net` 内 uid 0（含 `--uid 0` 映射）`nft add table` 仍 EPERM（无 CAP_NET_ADMIN）；维持现状 network none/host + net_policy 文本前置；收窄出访需特权后端或换隔离后端（见 I-33 分阶段注）。
- `access_events`：**选定 python-ptrace**（pip 已装，PtraceDebugger/PtraceSyscall import OK）——userspace ptrace 挂沙箱 pid，零特权采 openat/connect 事件；eBPF/audit 仍需特权（后续可选）。

**2026-09-09 架构决策汇总注记**：
- 角色分层最终版：**Cedar = 全部静态规则**（决策 + exec-guard 允许域双输出）；**veto = Cedar 的动态/有状态扩展**（python overlay，取代 Dogwood 组件——无 python 包）；无观察层，拦截即审计。
- SandboxRestriction 用**沙箱内绝对路径**（sandbox 模块添加的执行限制）；是否启用/配置 = policy 层。
- 落地分三阶段 = I-34（Landlock C launcher+seccomp BPF infra）→ I-35（Policy Cedar 化：静态核心+veto overlay+NoDecision/schema 决策）→ I-36（接线 compile→execute）+ I-37/38（preset 静态/动态 Cedar 化）。
- 现有 veto = 纯 Python 函数（非 DSL）——Cedar 化需显式翻译层与测试。

**2026-09-09 实现注记（I-34 已落地）**：`SandboxRestriction`（沙箱内路径 rw/ro + syscalls_deny）契约入 interfaces；`harness/sandbox/execguard.c`（C launcher，自动 RO sys 前缀）+ `execguard.py`（懒编译缓存、seccomp BPF via mephi42-seccomp）；`BubblewrapSandbox.execute(sandbox_id, command, restrict=None)` 装配 launcher RO-bind + `--seccomp FD`；**fail-closed**（landlock 预检/launcher 失败/无 gcc → SandboxRestrictionError；命令不执行）；docker backend restrict → 拒绝。测试 tests/test_i34_execguard.py（14）；全量 606 passed。后续接线（规则→restriction 编译与 pre-call 决策）见 I-35/I-36。

## References

- ADR-048（policy-middleware）、I-27/I-28/I-33、sandbox.impl

**2026-10-03 注记（pre-call 门的作用域收紧 / 一次过度工程化的回退）**：

本 ADR §9 已声明 pre-call veto「本质是字段匹配，无法约束实际 fs/net 访问（bash 通配/变量/转义可绕）」。实现层一度试图**补完这个门**：为 `bash` 增加命令分类（只读命令白名单 `_READ_ONLY_CMDS`、写命令表、解释器判定、`2>/dev/null` 重定向识别、`..` 归一化、未映射路径 fail-closed、沙箱内部路径白名单）——累计 ~230 行启发式。

**回退理由（三条，均已实测）**：
1. **命令空间无界**：新工具、新旗标、变量、引号拼接、`$(…)`、`cd`、内联 `python3 -c` 层出不穷；枚举必不完整，故"更聪明的提取器"不能成为安全保证。
2. **误伤真实使用**：把所有路径一律按 write 检查，导致只读输入目录下 `cat`/`ls` 被拒；而 `cat > out <<'EOF'`、`python3 - <<'PY'` 是 agent（含 DSH 自身）产出文件的**常规手段**。实测一次运行中该误伤直接打断任务。
3. **安全增益由内核提供，且已实测**：`shared_folders` 默认 `mode:"rw"`，实测 `echo PWN > /ws/out/../src/chunk_000.txt` **成功改写了宿主机源文件**；加 `mode:"ro"` 后内核返回 `Read-only file system`，`..` 同样被挡。Landlock（`execguard`）可用但**当前未为本 agent 启用**（`compile_guard` 无规则 ⇒ 返回 None）。

**收敛后的契约**：
- **执行点 = 挂载 + guard**（有界：路径集合）。这是唯一保证。
- **pre-call 门 = 咨询性**（无界：命令形式）。只做一件有价值的事：**读优先**——路径可读即放行；仅在读被拒时才按 write 复检。宁可漏，不可误伤。
- 未映射路径（`/dev/null`、`/proc/…`、逃出挂载的 `..`）**不由本门判定**：宿主机上不存在对应文件，交给内核。
- 净改动：`sandbox_exec.py` 相对启发式版本 **-109 行**。
- 回归测试 `tests/test_access_boundary.py`（15）：路径归一化、兄弟前缀由权限层拒绝、**内核级 e2e**（`mode:"ro"` 挡住直写与 `..` 穿越写、宿主机文件未被修改）、以及**禁止命令分类表复辟**的守卫。


**2026-10-03 注记（exec-guard 首次为应用层长程 agent 启用）**：

上一条注记记录了 pre-call 门收敛为「咨询性」并把执行点交给内核；本注记记录
harness 级 exec-guard（Landlock）首次在**应用层长程 agent** 上真正启用。具体规则
来自当时 app 的挂载布局（历史配置示例；该 app 已退役，ADR-002）；能力验证以
I-52 的 `tests/test_exec_guard.py`（14 项）为准。

- **规则**：`ja2zh_fs_guard`（agent-scoped）`guard={"paths":{"rw":["/ws/out","/dev"],"ro":["/ws/src"]}}`，经 `PolicyEngine.add()` 定义并启用；`compile_guard` 编译为 `SandboxRestriction`，由 runtime 注入 `execute(restrict=…)`。
- **`/dev` 必须进 rw 集合**：execguard 的自动 RO 基集含 `/dev` 但只读，导致 `2>/dev/null` 报 `Permission denied`。`/dev` 是沙箱内 tmpfs，写 `/dev/null` 不暴露宿主文件系统。这是必须避免的误伤——不修则会打断 shell 重定向这一常规写法。
- **能力验证**（I-52 `tests/test_exec_guard.py`，14 项）：
  - 授权内放行：`cat /ws/src/f.txt`、`echo > /ws/out/x`、`python3 - <<'PY'`、`cat > /ws/out/h <<'EOF'`、`ls 2>/dev/null`
  - 未授权 **rw 挂载** 写入被拒——这是挂载表达不了的能力：`/ws/other` 挂成 rw，内核仍拒绝，因为允许集只有 `/ws/out`
  - **与命令形式无关**地被拒：`python3 -c "open('/ws/other/e','w')"`、`VAR=/ws/other; echo x > $VAR/e`、`cd /ws/other && echo x > e`、`dd of=/ws/other/e`——这些正是 pre-call 字段匹配无法覆盖的形式
  - 源目录直写与 `..` 穿越写均被拒，宿主源文件校验未被修改
- **与挂载的关系**：挂载（`mode:"ro"`）保护**已挂载的树**；guard 额外给「rw 挂载内仅子目录可授权」与「程序直接 open 路径」的覆盖。两者互补，缺一不可。
- **遗留 / 2026-10-19 评估（I-67）**：`fs_paths` 规则的 `sys_read_deny`（禁读 `/etc /proc /sys /dev …`）在本 agent 未启用——启用它会从 execguard 的自动 RO 集**精确排除** `/dev`，从而打断 `2>/dev/null`。I-67 设计评估（docs-only）：
  - **机制**：`execguard.c` 的 `BASE_RO` 含 `/dev`；`roex <path>` 只跳过完全相同的 base 路径（`excluded()` 精确匹配），随后显式 `ro PATH` / `rw PATH` 仍会 `landlock_add_rule`。故「排除 `/dev` 后显式声明 `/dev/null`」在机制上可行，前提是当前 Landlock ABI 支持**文件**级 `PATH_BENEATH` 规则（需在目标内核/CI 实测；失败则回退 C）。
  - **A（推荐）**：`sys_read_deny` 排除 `/dev` 的同时，显式回补 `/dev/null` 到 **rw** allow 集（不能只 ro：`2>/dev/null` 需要 `write_file`），必要时再加 `/dev/zero`、`/dev/urandom`、`/dev/tty`；其余设备节点仍被 deny，安全面最小。最小实现：`fs_paths` preset 的 `guard.paths.rw` 增 `/dev/null`（或 `SandboxRestriction` 支持设备白名单），并在 `tests/test_exec_guard.py` 增加「fs_paths 启用时 `2>/dev/null` 放行、`/dev/mem` 拒绝」用例；实现另开 **I-69**。
  - **B**：`KERNEL_DENY` 去掉 `/dev`；自动 RO `/dev` 恢复，但 `2>/dev/null` 仍因只读失败，需 app 额外声明 `/dev` rw（等于部分放弃 deny）；仅适合明确接受该权衡的部署。
  - **C**：wontfix，保持禁用 `fs_paths`/`sys_read_deny`；pi 当前不使用它，风险为零。若 A 的文件规则在目标内核不可用，则回退 C 并保留本条文档化风险。
  - **结论**：推荐 **A**；本 ADR 更新不改变任何默认行为，`fs_paths` 仍默认禁用，实现与内核验证另开 I-69。

**2026-10-19 注记（pi 模式 agent 的内核边界配置）**：

ADR-002 的 pi 模式通用 agent 沿用「执行点 = 挂载 + guard」的契约，具体配置：

- **挂载**：cwd → `/workspace`（rw，`--bind`）；`/usr /lib64 /lib /bin /etc` 只读（`--ro-bind`，bwrap 自动）；`/tmp`（tmpfs）、`/proc`、`/dev`（tmpfs）。LLM 调用在宿主进程，sandbox 网络保持 `host`。
- **guard**：agent-scoped 规则 `pi_fs_guard`，`guard={"paths":{"rw":["/workspace","/dev"]}}`，经 `client.policy.add(..., guard=...)` 定义启用；系统只读前缀由 execguard 自动补，无需显式 ro 列表。`/dev` **必须在 rw 集合**（否则 `2>/dev/null` 被误伤，沿用 2026-10-03 注记的实测结论）。
- **read_before_edit**：不在 app 工具内实现，启用 harness 自带 preset：`client.policy.enable("read_before_edit", agent, cfg={"read_literal":{"read":"path"}, "write_literal":{"edit":("path", None)}, "digest_provider": …})`。`write` 仅负责创建、修改一律走 `edit`，因此守卫覆盖所有「既有文件修改」；未读/过期（stale）均被 veto 拒绝。
- **不启用** `fs_paths` / `sys_read_deny`：沿用上一条遗留结论（会连带禁读 `/dev`）；pre-call 路径匹配维持咨询性，不作为安全保证。
- **验证**：`tests/test_pi_security.py`（kernel e2e：`/workspace` 外写被拒、`2>/dev/null` 放行、未读/过期 `edit` 被拒）；挂载与 guard 的互补性论证沿用 2026-10-03 注记。

- **历史说明**：2026-10-03 注记中的翻译 agent 已于 2026-10-19 删除（ADR-002/I-62）；其挂载/guard 配置作为历史依据保留，pi 采用上面这条等价配置。
