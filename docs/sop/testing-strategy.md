# Agent Harness — 测试策略 SOP

> 本文档定义 Agent Harness 的三层测试策略：何时写单元测试、何时写集成测试、何时写 E2E 测试。
> 基于 2026-05-17 shared_folders execute bug 的经验教训——mock sandbox 层面的单测无法暴露真实调用链断裂。

---

## 测试金字塔

```
        ┌──────┐
        │ E2E  │  完整栈 + 真实 LLM（最少，最慢）
       ┌┴──────┴┐
       │ 集成层  │  模块间胶水 + 真实基础设施（中等，关键）
      ┌┴────────┴┐
      │  单元层   │  单模块 + mock 依赖（最多，最快）
      └──────────┘
```

| 层 | 被测对象 | 替代什么 | 替代什么不用 |
|:--|---------|---------|------------|
| 单元 | 单个方法/类 | 依赖注入 mock | — |
| 集成 | 模块 A → 模块 B 的调用链 | — | LLM API、网络 |
| E2E | 完整 Job 从 YAML 到 event log | — | — |

---

## 何时写哪层

### 单元测试（必写）

**触发条件**：任何新增或修改的方法。

**覆盖目标**：
- 正常路径（happy path）
- 权限拒绝路径
- 边界条件（超时、预算耗尽、空输入）

**约束**：
- 只能通过公共接口调用
- mock 所有外部依赖（sandbox、LLM、文件系统）
- 不能依赖 bwrap 或真实 API key

**示例**：`test_agent_runtime.py` 中的 `test_max_llm_calls_circuit_breaker` — mock `_call_llm`，验证熔断逻辑。

### 集成测试（以下任一条件满足时必写）

**触发条件**：
- 新模块有外部依赖（sandbox、文件系统、子进程、网络）
- 两个已有模块首次建立调用关系
- 修改了调用链中的中间层（如 `execute()` 的 `SandboxConfig` 构造）

**覆盖目标**：
- 调用链两端的数据一致性（写→读、发→收）
- 跨模块权限边界（bwrap 文件系统允许但 AgentRuntime 拒绝）
- 并发共享（二 Agent 单沙箱）

**约束**：
- 使用真实基础设施（bwrap、临时目录）
- mock 仅 LLM API 和网络
- 每个测试必须自己创建和清理资源（temp dir、bwrap 进程）

**示例**：`test_integration.py` 中的 `test_worker_writes_reader_reads` — 真实 bwrap + 两个 AgentRuntime，验证共享文件系统通路。

### E2E 测试（架构变更时写）

**触发条件**：
- 新增 Phase 类型或 Job 拓扑
- 修改 Orchestrator 或 Coordinator 的调度逻辑
- Human Gate 机制变更

**覆盖目标**：
- 完整 Job 从 YAML 到 event log 的闭环
- 真实 LLM 调用下的 DAG 执行

**约束**：
- 需要真实 API key
- 速度慢（秒级），数量控制在 10 个以内

**示例**：`test_e2e.py` 中的双 Agent 校验流水线。

---

## 集成测试编写清单

以下 checklist 适用于任何新增模块或跨模块调用链：

```
□ 1. 使用真实基础设施——不上 mock sandbox、不上内存文件系统
□ 2. 资源自管理——temp dir / bwrap 进程在测试内创建、在测试内清理
□ 3. 验证调用链双向——写操作后验证读回；读操作前确保写入
□ 4. 验证宿主侧——检查 temp dir 中的文件确实存在且内容一致
□ 5. 验证权限边界——沙箱允许 ≠ AgentRuntime 允许，两者都要测
□ 6. 验证并发共享——同一沙箱实例被多个 AgentRuntime 使用时文件可见
□ 7. 进程清理——kill sandbox 放在 finally 或显式调用，避免僵尸进程
□ 8. ready 等待——spawn 后 poll is_alive()，不用固定 sleep
```

---

## 反模式

| 反模式 | 为什么是反模式 | 替代做法 |
|--------|:-------------|---------|
| mock sandbox 测文件 I/O | 2017-05-17 的 execute bug 在 mock 下不可见 | `test_integration.py` 用真实 bwrap |
| 固定 `time.sleep(1)` 等进程就绪 | 慢机器上不够，快机器上浪费 | 轮询 `is_alive()` |
| 全局共享 temp dir | 测试间文件残留导致假阳性 | 每个测试用 `tempfile.mkdtemp` + `shutil.rmtree` |
| 只测 AgentRuntime 不测 sandbox | `write_file` 返回 "Wrote N bytes" 但文件不在宿主上 | 加 `os.path.isfile(host_path)` 断言 |
| 只测 tools 不测 `execute()` | 两个走不同的 bwrap 配置路径 | 分别覆盖 spawn 时 mount 和 execute 时 mount |
| 集成测试放在单元测试文件里 | 混淆测试速度和依赖 | 独立 `tests/test_integration.py` |

---

## 新模块的测试文件结构

```
tests/
├── test_<module>.py          # 单元测试（mock 依赖）
├── (如适用) test_integration.py  # 跨模块集成（真实基础设施）
└── (如适用) test_e2e.py          # 全栈（真实 LLM，已有，追加）
```

---

## 冒烟顺序

```bash
# 1. 单元测试（秒级，无外部依赖）
pytest tests/ -q --ignore=tests/test_e2e.py --ignore=tests/test_integration.py

# 2. 集成测试（秒级，需要 bwrap）
pytest tests/test_integration.py -q

# 3. E2E 测试（分钟级，需要 API key）
pytest tests/test_e2e.py -q
```

---

## 回顾日期

- 2026-05-17：初版。基于 shared_folders execute bug 和集成测试补齐经验。
