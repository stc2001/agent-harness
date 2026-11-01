此外，计划编排层应当是app层的事情（而不是harness层）,存在以下痛点：
1.应当是agent驱动的，但考虑到一个任务不可能一句话计划好，一个长程任务往往需要多次wave的agent协同；
2.现有PLAN模式似乎只是单纯的根据对话确认PLAN的文档，缺少确定性的流程编排，依旧会陷入agent自说自话完成的情况（缺少/goal或者/verify）
3.纯确定性的流程编排不适合现在agent自主执行的趋势
于是提出以下方案：
ADMIN：
编排agent：创建mission,编排mission的agent的主agent，直接和user沟通
A2A：Agent2Agent协议
JIT编译：也就是输入一条指令即检查其指令合法性，根据已经插入的agent关系图谱，检查是否符合指定的agent依赖约束（如有）
agent依赖约束：星形，网状，树状
wave：波次，用于定义一个mission多个子任务

0.0 Jinja 前端渲染（2026-08-19 决策，ADR-045）
    ·图声明采用 Jinja2 前端渲染（app 层）：模板 → 渲染 YAML → Schema 校验 → JIT 编译 → 执行。
    ·声明层弱化：
        ├─ 模板参数化（同图多实例）→ Jinja {{ params }}（原 I-22 模板机制删除）
        ├─ 批量 worker 槽位 → {% for i in range(M) %} 渲染期生成（M 配置定，任务量 N 归运行期 manifest 消费）
        └─ 静态条件节点（是否加 verify/抽检）→ {% if %} 渲染期决定；运行期数据依赖分支仍留 ConditionalRouteHook
    ·双模式并存：
        ├─ 模式 A（模板化批次任务）：Jinja 渲染静态图 → 一次编译 → 执行；append 弱化为"改模板 → 重新渲染"
        └─ 模式 B（agent 自主编排）：JIT 动态构建保留（append/detach/modify 增量编译）
        → Jinja 是"提交前"的图生产器，JIT 是"提交后"的图裁判与演进器，时间轴不重叠
    ·沙箱红线：SandboxedEnvironment + 白名单，渲染产物为纯 YAML
    ·不弱化：JIT 校验（环/双根/缺父/深度/manage_scope）、状态机、AutoTrigger/ConditionalRoute、
      wave -modify/interject、权限矩阵——全在提交后，Jinja 不替代

0.Mission Graph 中所有节点均为统一的 Agent Class，不存在异构节点。Program Agent 是 Agent 的退化子集，差异仅通过 agent_type 字段内化到 execute() 实现中，外部调度器无 if/else 分支。

Agent 核心字段：
├─ name, mission, parent, tags: 通用身份
├─ agent_type: "llm" | "program"          # 唯一分派开关
├─ capabilities:                          # 能力位，Program 强制归零
│   ├─ reasoning: bool                    # Program: False
│   ├─ interjection: bool                 # Program: 强制 False（忽略配置）
│   ├─ a2a_communication: bool            # Program: 强制 False
│   └─ stateful_dialogue: bool            # Program: False
├─ model: str | None                      # LLM 用；Program 忽略
├─ allow_interjection: bool               # LLM 用；Program 忽略
├─ runtime: str | None                    # Program 用；LLM 忽略
├─ script: str | None                       # Program 用；LLM 忽略
├─ timeout, retries, on_failure           # Program 执行策略；LLM 忽略
└─ output, output_schema                  # 通用

Program Agent 特性：
• 不接入 LLM，执行确定性脚本/函数
• 单次执行，无推理循环，无状态对话
• 不参与 A2A 通信（即使配置 a2a_enabled=True，也强制忽略）
• 不支持插话（即使配置 allow_interjection=True，也强制忽略）
• 典型用途：/verify、/goal、数据转换、条件判断、自动化通知

1.设计一个mission control，作为一个工具，其核心是接受来自agent创建的编排指令，有以下几个基础管理指令：
    # 生命周期
    /mission create -name {new_mission_name} [-root {agent_name}]
    /mission delete -name {mission_name} [-agent {agent_name}] #删除mission的某个agent或者某个mission
    /mission start/stop/restart -name {mission_name}
    

    # 图结构操作（JIT 实时编译）
    /mission append -name {mission} -agent {agent;params} [-parent {parent}] [-tag manager|member]
    /mission detach -name {mission} -agent {agent_name}          # 从父节点剥离，不删除
    /mission modify -name {mission} -agent {agent_name} -param {json} #说明：修改 PENDING / READY / FAILED 状态 Agent 的参数。对 RUNNING Agent 无效。
                                                                      #JIT：触发增量编译，检查修改后依赖是否仍然成立。
    /mission config -name {mission} -agent {agent_name} -key {key} -value {value} #说明：配置 Agent 级参数（如 timeout, retries, allow_interjection, on_failure）。

    # 观测
    /mission view -name {mission_name} [-agent {agent_name}]  #查看某个mission的agent或者mission本身
    /mission status -name {mission_name}                      #显示各 Agent 子状态：PENDING→READY→RUNNING→PAUSED/COMPLETED/FAILED
    
    # 显式编译触发（可选，append 默认触发增量编译）
    /mission compile -name {mission_name} [-dryrun]

    # Wave 管理（解决长程任务多轮协同）
    /mission wave -name {mission} -define {wave_id} -agents {list} -goal {objective} [-block_next true|false]
    /mission wave -name {mission} -verify {wave_id} -condition {check_logic}
    /mission wave -name {mission} -list
    /mission wave -name {mission} -modify {wave_id} -agents {+-agents} #说明：对未开始或运行中的 Wave 动态增删 Agent。
    /mission wave -name {mission} -block_next {wave_id} -value {true|false} #说明：配置 Wave 是否阻塞下一 Wave（默认 true）。
    
    #插话指令：
    /mission interject -name {mission} -agent {agent_name} -content {message} [-action modify|abort|skip|inject_context] #说明：仅在目标 LLM Agent 处于 PAUSED（stop_result=tool_calling）且 allow_interjection=True 时有效。
    
2.基本设计原则：
/mission模块采用JIT编译方法，插入一条指令即进行检查合法性
agent关系依赖：本质是有向图，但采用扁平化设计（最大深度=2：Root → Child），不引入嵌套子图或 Composite 节点。同一层级且同一父节点的 Agents 自动成 Group。

    基本约束：
    ·每一个mission都有一个root节点,root节点对应的agent默认是编排agent，或者编排agnet指定的agent。
    ·同一层级的agents且满足属于同一个父节点的agents,可以组内通过A2A通信（member标签），父节点的agent默认可以配置管理其直属的子节点agent，也就是restart、stop、modify（manager标签的A2A）
        ·父节点的agent也可以显式配置为不允许管理直属子节点，但子节点的子节点（如有，即类似于树状编排，且层级很深），即跨层级则不支持：也就是“下属的下属不是我的下属”
        ·也可以给指定层级的父节点配置允许subagents,让其自行生成子代理完成任务
    ·因此，需要显式配置agent之间的依赖关系，允许多对一、一对多
    
    不同类型的agent权限矩阵：
        ┌─────────────────────┬────────────┬─────────────┬────────┐
        │ 操作                │ Root       │ Parent      │ Member │
        ├─────────────────────┼────────────┼─────────────┼────────┤
        │ start/stop/restart  │ 全树       │ 仅子树      │ ❌     │
        │ append/delete       │ 全树       │ 仅直属子节点│ ❌     │
        │ modify (PENDING)    │ 任何 Agent │ 仅直属子节点│ ❌     │
        │ 插话 (PAUSED)       │ 可配置     │ 可配置      │ ❌     │
        │ Group 内 A2A 通信   │ ✅         │ ✅          │ ✅     │
        │ 跨 Group 通信       │ 需授权     │ 需授权      │ ❌     │
        │ 生成 subagents      │ 可配置     │ 可配置(一层)│ ❌     │
        └─────────────────────┴────────────┴─────────────┴────────┘

        manage_scope 机制：
        • 每个 Agent 维护 manage_scope 字段，默认值为 1（仅管理直属子节点）
        • Root 可配置为 -1（无限）或具体数值
        • 跨层级管理被显式禁止："下属的下属不是我的下属"
        • JIT 编译时检查：任何管理操作的目标必须在发起者的 scope 范围内
        • 父节点可显式配置为不允许管理直属子节点（manage_scope=0）
    
    需要的agent/wave状态机约束：
        ·Agent 级状态机（替代 Mission 级全局冻结）：
            状态定义：
            ├─ PENDING：前置依赖未满足，完全可修改/删除
            ├─ READY：依赖已满足，已入调度队列，可修改（需通知 scheduler）
            ├─ RUNNING：执行中，默认不可修改
            ├─ PAUSED（LLM 专属）：stop_result=tool_calling 时的插话窗口
            ├─ COMPLETED / DONE：执行完毕，锁定
            └─ FAILED：执行失败，可修改后重试

            运行时锁定规则：
            ┌─────────────┬─────────┬─────────┬────────┬────────────────────────┐
            │ 状态        │ Modify  │ Delete  │ 插话   │ 说明                   │
            ├─────────────┼─────────┼─────────┼────────┼────────────────────────┤
            │ PENDING     │ ✅      │ ✅      │ N/A    │ 未开始，完全可变        │
            │ READY       │ ⚠️      │ ✅      │ N/A    │ 修改需通知 scheduler    │
            │ RUNNING(llm)│ ❌      │ ❌      │ 受配置 │ 默认关闭               │
            │ PAUSED(llm) │ ❌      │ ❌      │ ✅     │ 仅 tool_calling 且允许 │
            │ RUNNING(prog)│ ❌     │ ❌      │ ❌     │ 无插话窗口             │
            │ COMPLETED   │ ❌      │ ❌      │ N/A    │ 已执行，锁定           │
            │ FAILED      │ ✅      │ ✅      │ N/A    │ 可调整后重试           │
            └─────────────┴─────────┴─────────┴────────┴────────────────────────┘

        Wave 级别可变性：
        • 未开始的 Wave：整体结构可变（增删 Agent、调整依赖）
        • 正在运行的 Wave：已完成 Agent 锁定，未完成 Agent 可变
        • 编排 Agent 可根据前面 Wave 结果动态调整后续 Wave
    
    扩展（hook）：
    > 注（2026-08-19）：本节的 Hook（CapabilityMask/AutoTrigger/ConditionalRoute/JIT 校验等）是 **mission 领域内的确定性扩展点**（编译期校验 + 执行期自动触发），与 ADR-042 的**进程级事件总线 hooks**（harness/hooks：llm.call / agent.pre_step / agent.lifecycle 等）是两个独立概念。衔接关系：mission 作为事件总线 hooks 的**消费方**——AutoTrigger 感知上游 COMPLETED 可订阅 `agent.lifecycle`；ConditionalRoute/计量走 `llm.call` around；插话走 `agent.pre_step`。混合模式边界见 ADR-042 §8（hooks 全局单例、业务服务实例隔离，无共享 core）。
    ·一些任务可能会需要特定命名+特定关系结构（如树状），比如：OPC一人公司需要组织架构编排，往往是树状，则可能需要：
        ·agent关系上：按照树状结构划分；
        ·agent命名上：为了方便编排agent识别并编排，采用组内驼峰，组间pydantic命名法，“_”这个符号用于划分其节点的层级，如missionA_a , missionA_b两个则是同一个任务missionA的同一级别， missionA_a_b , missionA_b_c则是属于a和b下面的级别的subagents。
    ·因此对于如上特殊情况，提出两个Hooks,一个是agent命名约束hook,一个是mission的agent依赖关系约束Hook：前者可以约束命名方法，后者可以约束特定mission的agent应该遵循的图结构（星形/网状/树状）。
    ·此外，还有权限约束Hook,默认是检查manager 标签合法性
    具体而言：
        1. CapabilityMaskHook（能力位强制覆盖）
        触发：JIT 编译时（on_agent_append）
        作用：Program Agent 的 capabilities 强制归零。用户即使错误配置 allow_interjection=True 或 a2a_enabled=True，也直接忽略。
        行为：不报错、不崩溃，静默覆盖。
        2. AutoTriggerHook（自动化触发）
        触发：上游 Agent 进入 COMPLETED 时
        作用：自动将依赖全部满足的 Program Agent 从 PENDING → READY，无需手动 /mission wave -verify。
        用途：实现 /verify、/goal 等检查点的无人值守执行。
        3. ConditionalRouteHook（条件路由）
        触发：Program Agent 执行完成时
        作用：根据 Program 输出（布尔/枚举/数值）决定激活下游哪个 Agent，实现扁平图中的 if/else 分支。
        配置示例：
            conditional_routes:
            - if: "output.passed == true"  then: "deploy_agent"
            - else: "fix_agent"
        4. ScriptSyntaxHook
        触发：JIT 编译时
        作用：检查 script 语法合法性（python/node/bash）及 runtime 环境可用性。
        5. DependencyCheckHook
        触发：JIT 编译时
        作用：检查 script 中引用的 context.inputs.{upstream} 是否指向已存在的上游 Agent。
        6. TimeoutGuardHook / RetryPolicyHook
        触发：Program 进入 RUNNING 时
        作用：强制超时保护（SIGTERM→SIGKILL）及失败重试（固定间隔/指数退避）。
        

3.agent间通信
    ·采用A2A通信，拥有群聊（matrix协议）功能，同一层级的agents且满足属于同一个父节点的agents,为一个group（当然该父节点agent本身也是组员），可以组内通过A2A通信
    ·此时组内父节点agent标签是manager,其余的标签是member。

3.1.A2A 通信边界：

    • A2A 仅发生在同一 Group 内的 llm 节点之间
    • Group = 同一 Parent 下的所有 llm 子节点 + Parent 本身
    • Program Agent 不参与 A2A Group Chat，即使配置 a2a_enabled=True 也强制忽略
    • Program Agent 的输出通过 Mission Control 的共享 Context Store 写入，下游 Agent 通过 context.inputs.{name} 读取
    • 跨 Group 的 llm 通信需显式授权，通过 Agent Card 发现机制实现

    

4.技术细节：
4.1 指令JIT
当收到 /mission append 时，JIT 编译器执行：
├─ 语法检查：指令格式、参数完整性
├─ 图结构检查：
│   ├─ 循环依赖检测（DFS/BFS）
│   ├─ Root 节点唯一性检查
│   ├─ 父节点存在性检查
│   └─ 层级深度限制（可配置）
├─ 约束检查（Hook）：
│   ├─ 命名约束 Hook：如有，例如正则匹配 missionA_a_b 格式
│   ├─ 拓扑约束 Hook：如有，例如星形/树状/网状结构验证
│   └─ 权限约束 Hook：manager 标签合法性
└─ A2A 兼容性检查：
    ├─ Agent Card 有效性
    ├─ 通信路径可达性
    └─ 认证配置完整性
    └─ Program Agent 检查：
    ├─ 能力位强制归零（CapabilityMaskHook）
    ├─ runtime 环境可用性（ScriptSyntaxHook）
    ├─ script 语法预检
    ├─ 输入依赖存在性（DependencyCheckHook）
    └─ output_schema 格式合法性

4.2 插话机制
插话机制（Interjection）：

目的：解决 LLM Agent "自说自话"问题，允许编排 Agent 或父节点 Agent 在关键节点介入。

触发条件（必须同时满足）：
1. Agent 类型为 llm
2. Agent 状态为 RUNNING
3. Agent 完成一次完整输出（一轮推理结束）
4. stop_result == "tool_calling"
5. 该 Agent 显式配置 allow_interjection=True（默认 False）

插话权限：
├─ 编排 Agent（Root）：可插话任何 allow_interjection=True 的 LLM Agent
├─ 父节点 Agent（Manager）：可插话直属子节点中 allow_interjection=True 的 LLM Agent
└─ Member Agent：不可插话其他 Agent

插话指令：
/mission interject -name {mission} -agent {target_agent} -content {message} [-action modify|abort|skip|inject_context]

-action 说明：
├─ modify：修改该 Agent 后续执行参数
├─ abort：终止当前执行，标记 FAILED
├─ skip：跳过当前步骤，进入下一逻辑节点
└─ inject_context：注入额外上下文，影响下一轮推理

插话后行为：
• Agent 状态保持 RUNNING，基于新输入重新生成，继续执行
• 若插话为 abort，进入 FAILED，触发下游 PENDING Agent 重新评估
