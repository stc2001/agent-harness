# ADR-023: YAML 提取到 CLI — SDK 接收 Python 对象

**日期**: 2026-05-19  
**状态**: 已接受  

## 上下文

SDK 层的 `_AgentsClient.compile(spec_path)`、`_JobsClient.submit(job_yaml)`、`SandboxManager(config_path)` 均在内部调用 `yaml.safe_load()`。这违反了分层原则——SDK 作为编程接口不应依赖序列化格式。用户通过 CLI 用 YAML；用户通过 SDK 用 Python 对象。两者不应混淆。

参考 Docker 模型：`docker run` CLI 解析命令行参数 → Docker Engine API（JSON，无 YAML）。`docker-compose` CLI 解析 `compose.yml` → Engine API。

## 决策

**SDK 不再读取任何 YAML 文件。** 所有 `yaml.safe_load()` 调用提取到 CLI 层或类方法。SDK 接收 Python 对象。

具体变更：
1. `SandboxManager.__init__(configs: dict)` — 接收预构建的 `SandboxConfig` 字典
2. `_AgentsClient.compile(path)` 删除 → `_AgentsClient.register(spec: AgentSpec)` 新增
3. CLI `cmd_compile` 自行 `yaml.safe_load()` + 构造 `AgentSpec` → 调用 `register()`
4. `HarnessClient.__init__` 接受可选 `sandbox_configs` 参数

## 理由

1. **SDK 不应依赖序列化格式**。YAML 是用户界面约定，不是 API 约定。
2. **程序化编排可行**。用户可直接构造 `AgentSpec`/`JobSpec`/`SandboxConfig` Python 对象，不需要 YAML 文件作为中介。
3. **测试简化**。测试直接构造 Python 对象而非创建临时 YAML 文件。
4. **对齐行业标准**。Docker、Kubernetes 均采用「CLI 解析 YAML → API 接收结构化对象」模式。

## 后果

- `SandboxManager._config_path` 和 `_load_configs()` 删除
- `_AgentsClient.compile()` 删除
- 约 10 个测试从 YAML 文件构造改为 Python 对象构造
- CLI `harness compile spec.yaml` 内部 `yaml.safe_load()` → `agents.register(spec)`
- SDK 用户如需 YAML 加载，可自行调 `yaml.safe_load()` + 构造对象

### 例外

以下 SDK 层保留 YAML 读取——均属基础设施配置，非编排逻辑：

1. **`HarnessSettings.from_yaml("harness/config/local.yaml")`** — 全局配置 bootstrap。等效于 `.env` 文件，不涉及 Agent/Job 编排。
2. **`resolve_llm_profile()` 读取 LLM 配置 YAML** — API 密钥管理。敏感信息不应硬编码在 Python 中，YAML 是合适的密钥存储格式。程序注入（`HarnessSettings(deepseek_api_key=...)`）优先级高于 YAML 文件（见 2026-05-19 修复）。

原则：编排 YAML（AgentSpec、JobSpec、SandboxConfig）由 CLI 解析。基础设施 YAML（Settings、LLM profile）可留在 SDK 作为默认引导。
