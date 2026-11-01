# ADR-014：AgentSpec 与 LLM 凭证分离——模板与实例解耦

**日期**：2026-05-16  
**状态**：已采纳

## 上下文

Agent Harness 的 AgentSpec（注册表 YAML）最初包含 `model`、`api_key`、`base_url` 等字段。这导致两个问题：

1. **凭证泄露风险**：AgentSpec 文件通常提交到 Git，API Key 硬编码其中会被历史记录永久保留
2. **多环境困难**：开发和生产需要不同的 Key，但同一份 YAML 无法同时满足

另一种方案是把 API Key 直接塞进 `harness/config/local.yaml`（全局单一 Key），但这导致所有 Agent 共用同一个 LLM 凭证，无法支持"Worker 用 DeepSeek、Validator 用 OpenAI"的多供应商场景。

## 决策

**将 AgentSpec 拆分为两个独立层：AgentSpec（模板）+ RuntimeConfig（凭证 + 运行时参数）。**

```
AgentSpec (模板) ──┐
                   ├──► Agent 实例 = 模板 + 凭证引用
RuntimeConfig ─────┘
```

### AgentSpec（模板）

纯类型定义，无敏感信息，可提交到 Git：

```yaml
name: test-worker
description: Shell command executor
llm_profile: deepseek-prod    # ← 引用名，不是凭证
llm_fallback: openai-gpt4o    # ← 可选 fallback
model: deepseek-v4-flash
system_prompt: "You are a worker."
tools: [bash, read_file]
```

### RuntimeConfig（凭证）

存放 API Key、base_url、timeout 等运行时参数，**不进入版本控制**：

```yaml
# ~/.harness/llm.d/deepseek-prod.yaml
provider: deepseek
api_key: "${DEEPSEEK_API_KEY}"
base_url: https://api.deepseek.com/v1
model: deepseek-v4-flash
timeout: 30
```

### 三层查找优先级

```
项目级 configs/llm.yaml      ← 最高，per-repo 覆盖
用户级 ~/.harness/llm.d/     ← 个人配置
系统级 /etc/harness/llm.d/   ← 系统默认值
```

AgentSpec 中只写 `llm_profile: "deepseek-prod"`，运行时按优先级查找同名配置。

## 理由

1. **安全与共享的分离**。模板可以开源共享，凭证保持私有。类比 Docker Image（可共享）vs Docker Registry 认证（私有）。
2. **多供应商支持**。同一流水线中不同 Agent 可以调用不同 LLM（Worker→DeepSeek、Validator→OpenAI），无需修改模板。
3. **环境隔离**。开发/测试/生产各自维护独立的 `configs/llm.yaml`，同一份 AgentSpec 模板通用。
4. **凭证轮换无代码改动**。换 Key 只需更新 `~/.harness/llm.d/` 中的文件，AgentSpec 不动。

## 后果

- AgentSpec 新增 `llm_profile`、`llm_fallback` 字段
- 新增 `LLMConfig` Pydantic 模型、`resolve_llm_profile()` 三层查找函数
- `AgentInstancePool.acquire()` 需要在创建 `AgentRuntime` 前解析 `spec.llm_profile` 并注入 `api_key`/`base_url`
- `HarnessClient` 初始化时注入 `resolve_llm` 回调，实现配置与执行层的解耦

## 未采纳的替代方案

- **方案 A：全局单一 Key**（`harness/config/local.yaml` 只有一个 `deepseek_api_key`）—— 不支持多供应商
- **方案 B：每个 AgentSpec 内嵌完整 LLM 配置** —— 凭证进入 Git，安全风险
- **方案 C：环境变量唯一来源** —— 无法支持同一主机上多 Agent 多供应商
