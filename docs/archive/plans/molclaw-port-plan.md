# MolClaw → Agent Harness 移植分析

**日期**: 2026-05-19
**状态**: 分析阶段
**参考**: [MolClaw arxiv 2604.21937v2](https://arxiv.org/abs/2604.21937) | [GitHub](https://github.com/InternScience/MolClaw)

---

## 1. MolClaw 底层结构拆解

MolClaw 源码分为以下层级，逐层映射到 Agent Harness：

### 1.1 运行时层

| MolClaw 组件 | 作用 | Harness 等价 |
|-------------|------|-------------|
| OpenClaw / Claude Code 运行时 | Agent 执行循环、工具调用调度 | AgentRuntime + Coordinator |
| SCP Infrastructure | GPU 调度、并发任务、访问控制 | Sandbox + Pool（无 GPU 调度） |

**Harness 缺口**: 无 GPU 调度能力。SCP 的 GPU 资源池管理需要远端 Hub，本地只做客户端。

### 1.2 技能系统

| MolClaw 组件 | 路径 | 行数估算 | Harness 映射 |
|-------------|------|:--:|------|
| L1 工具技能 | `skills/L1_tools/` | 60 个 SKILL.md | AgentSkills（文件系统） ✅ |
| L2 工作流技能 | `skills/L2_workflows/` | 14 个 | Pipeline + AgentSkills ✅ |
| L3 方法论技能 | `skills/L3_methodology/` | 26 条原则 | system_prompt（prompts.assemble） ✅ |
| LR 检索技能 | `skills/LR_research/` | ? | AgentSkills ✅ |
| 自动生成技能 | `skills/auto-generated-skills/` | 运行时生成 | bind-mount 持久化 ✅ |
| System Prompt | `skills/system_prompt/` | — | prompts.assemble() ✅ |

**结论**: 技能文件可直接复制到 `skills/` 目录，绑定挂载后 Agent 通过 `read_file` 读取。零代码适配。

### 1.3 执行框架（Gate 调控三阶段）

| MolClaw Phase | 作用 | Harness 对应 |
|--------------|------|-------------|
| Phase 0: 任务分诊 + 技能加载 | 工具调用前强制分类 + 按需加载 L1/L2/L3 技能 | Pipeline + prompts.assemble(skills=...) |
| Phase 1: 持续质量验证 | 每次工具调用后 sanity check + 交叉验证 | Human Gate × N（APP 层编排为 compute→verify→compute） |
| Phase 2: 结果合成 + 技能结晶 | 执行轨迹 → 持久化技能 | `write_file` + `install_skill` + bind-mount ✅ |

### 1.4 基准测试

| MolClaw 组件 | 路径 | 作用 | Harness 侧 |
|-------------|------|------|-----------|
| MolBench-MS | `molbench/` | 分子筛选基准 | 可用 Pipeline 跑，需领域数据 |
| MolBench-MO | `molbench/` | 分子优化基准 | 同上 |
| MolBench-E2E | `molbench/` | 端到端发现（8-50+ 工具调用） | 同上 |
| 评估脚本 | `molbench/eval/` | `eval_runner.py`, `run_eval_bench.py` | 独立脚本，不改 |

### 1.5 推理/运行脚本

| MolClaw 组件 | 路径 | 作用 |
|-------------|------|------|
| 基线推理 | `molclaw_run/infer/baselines/` | 直接 LLM 调用（无 Agent） |
| Claude Agent | `molclaw_run/infer/claude_agent/` | Claude Code + Skills 驱动 |
| 配置文件 | `config/` | YAML 配置（数据集、模型参数） |

---

## 2. 移植路径

### Phase 1: 技能文件移植（零代码）

```
1. git clone MolClaw
2. cp -r MolClaw/skills/L1_tools/*     → skills/{name}/SKILL.md
3. cp -r MolClaw/skills/L2_workflows/* → skills/{name}/SKILL.md
4. cp -r MolClaw/skills/L3_methodology/* → 整合为一个 methodology skill 或按 domain 拆分
5. CLI compile → AgentSpec.skills 选择性挂载
```

**预计工时**: 30 分钟文件整理。

### Phase 2: Agent 定义 + Pipeline 编排

```
1. 定义 AgentSpec: molclaw-screener, molclaw-optimizer, molclaw-validator
2. Pipeline: L2 workflow skills 对应 PipelineStep DAG
3. Human Gate: 每个 compute 后插入 verify node
4. 产物传递: declare_output_path → 下游 vars 注入
```

**新增文件**: `registry/molclaw-screener.yaml`, `registry/molclaw-validator.yaml`, `jobs/molclaw-pipeline.yaml`

### Phase 3: SCP/MCP 工具接入

```
1. pip install mcp
2. harness/tools/mcp_adapter.py: MCPClient → ToolRegistry
3. SCP 工具需先包装为 MCP server（或通过 SCP→MCP 桥接）
```

**预计工时**: ~120 行 adapter + 测试。

### Phase 4: MolBench 评估

```
1. 准备基准数据集（MolClaw 已有，直接用）
2. Pipeline 跑推理 → 导出结果
3. 调用 molbench/eval/ 脚本评估
4. 对比 MolClaw 原始结果验证等价性
```

---

## 3. 风险与未知

| 风险 | 等级 | 说明 |
|------|:--:|------|
| SCP 工具调用链路 | 高 | MolClaw 的 30+ 工具通过 SCP Hub 调度。若无 Hub 部署，须改为 MCP 直连 |
| GPU 资源调度 | 高 | SCP 提供 GPU 池管理。本地运行需手动分配 GPU |
| Conda 环境依赖 | 中 | MolClaw 使用 conda env。Harness 沙箱基于 bwrap，需映射 conda 路径或构建 Docker |
| 技能正文 Token 预算 | 低 | L3 方法论 26 条原则全量加载可能超 token 预算。需分 domain 选择性加载 |
| 分子格式转换 | 低 | SMILES/SDF/PDB 格式互转——纯工具问题，非框架问题 |

---

## 4. 建议执行顺序

```
1. [ ] git clone MolClaw + 通读源码结构
2. [ ] 提取 L1/L2/L3 技能文件 → 适配 YAML frontmatter 格式
3. [ ] 定义 AgentSpec + Pipeline Job YAML
4. [ ] 跑通最小闭环（单个筛选任务，不用 GPU）
5. [ ] 接入 SCP 客户端（或 MCP 替代）
6. [ ] 完整 MolBench 评估对比
```
