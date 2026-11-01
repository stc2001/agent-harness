# ADR-004：Agent-Sandbox 解耦模型

**日期**：2026-05-12  
**状态**：已采纳  

## 上下文

原始设计中，Agent 和 Sandbox 是 1:1 绑定的——每个 Agent 实例拥有自己的 Docker 容器。随着架构演进，需要考虑：
- 多个 Agent 是否可以共享同一个沙箱？
- Agent 运行时是否可以独立于沙箱存在？

## 决策

**Agent 和 Sandbox 完全解耦。**

## 理由

1. **Agent 是"大脑"，Sandbox 是"工具"**。AgentRuntime 在宿主进程中运行（LLM 调用 + 决策），仅当需要执行不可信代码时才调用 `sandbox_execute` 工具。
2. **多 Agent 共享沙箱**。多个验证 Agent 可以共享同一个沙箱实例（共享文件系统），权限通过 Agent 级别的 `file_access` 控制，不在沙箱层面。
3. **独立配置**。Sandbox 配置（`sandbox.yaml`）和 Agent 定义（`registry/*.yaml`）完全分离。AgentSpec 中只需引用沙箱名（`sandbox: "main"`），无需知道后端细节。
4. **可测试性**。Agent 可以独立于沙箱进行单元测试。沙箱可以独立进行隔离测试。

## 后果

- `AgentInstancePool`（`harness/orchestration/pool.py`）管理 Agent 实例，`SandboxManager` 管理沙箱实例，两者解耦
- 沙箱从 Agent 的"容器"变为 Agent 的"工具"——语义简化
- `NullSandbox` 用于在已隔离的 bwrap 环境中运行的 Agent

### 2026-05-19 更新

Skills 系统遵循同一解耦原则：Skills 是沙箱文件系统中的 artifact（通过 shared_folders bind-mount 注入），Agent 通过 `read_file`/`bash` 工具自主发现和读取。Skill 的加载与 AgentRuntime 无关——参见 ADR-021。

### 2026-05-22 更新

AgentInstance 字段精简为 5 个身份标识（`instance_id`, `spec_name`, `llm_profile`, `created_at`, `status`）。删除了 `spec_path`, `llm_fallback`, `sandbox_config`, `metadata` 四个死字段。

`AgentStatus` 枚举从 5 个状态（`COMPILED`, `RUNNING`, `STOPPED`, `FAILED`, `ARCHIVED`）简化为 1 个（`COMPILED`）。生命周期完全由 Session（`SessionStatus: CREATED → RUNNING → COMPLETED/FAILED`）接管。AgentInstance 退化为无状态注册表条目——和 Docker image 一样，image 没有"运行中"状态，container（session）才有。

### 2026-08-19 更新（I-14）：三池解耦——模型服务池为第三兄弟

解耦原则从"双池"扩展为"**三池兄弟独立**"：AgentInstancePool（谁在跑）、ModelServicePool（用哪个引擎端点）、SandboxManager（在哪跑命令）三者互不嵌套、各自独立生命周期与可观测性。**harness 本体永不进入任何隔离边界**（明确排除早期 OpenClaw"harness 在 sandbox 里"的反模式）。

- **ModelServicePool（I-14，`harness/orchestration/model_pool.py`）**：模型服务端点注册表 + 探活器。**不 spawn 进程**（C 模式：外部引擎自注册，进程生命周期归外部编排——tile-rwkv DESIGN.md §8.3）；**探活 = `GET /health`（回退 `/v1/models`）**，镜像 ADR-040 §2 就绪探针（探针验证"可服务"而非"进程在"）；多候选端点故障转移 + 全败清晰报错；adapter 预热尽力而为（加载成败归引擎，tile-rwkv DESIGN.md §8.2）。
- **本地路由（Q5-A）**：`LLMConfig.model_service` 池键；`pool.acquire` 时路由+探活+预热一次完成（`_call_llm` 不变）；pool 未配置时保留 I-13 直连行为（向后兼容）。
- **端点联动（Q9）**：引擎换端口重启 → re-register → 下一次 acquire 复用路径自动更新实例端点（对齐沙箱 re-spawn 先例），更新事件记审计 `model_endpoint_updated`（内部自动处理，可观测）。
- 模型池不是 sandboxes 的子属性、agent 池不吞模型池——三者是兄弟，非嵌套（三池独立，延续本 ADR 解耦精神）。
