# Agent Harness（内核）

> 单机本地 Agent 基础设施 —— 沙箱隔离、统一授权、可审计、可重现

本仓库只包含 harness **内核**（kernel）。上层应用 pi（pi-mode CLI、记忆压缩、
PI_HOME 配置）已拆分到同级仓库 ../pi-app；拆分记录见
docs/plans/repo-split-plan.md。

## 能力

- **SDK + CLI 单一入口**（ADR-015）：Python SDK 用于集成，CLI 用于交互。
- **沙箱隔离**：bubblewrap（Linux）+ Docker 双后端；路径级读写权限，fail-closed。
- **统一授权**：capability token + 策略中间件 + 工具执行期强制（ADR-035/048/051）。
- **事件日志**：append-only JSONL；rewind/fork/checkpoint 为日志级运算；
  Session 无状态（ADR-024/037/050）。
- **Prompt 组装与预算**：按轮截断、不可再精简集、装配期 fail loudly（ADR-053）。
- **Skills 注入**：文件系统化 + 渐进披露（ADR-021）。
- **无固定 DAG**：框架提供原语，APP 决定调度（ADR-038）。

## 系统要求

- Python >= 3.11
- Linux（bubblewrap 沙箱；无 bwrap 时可切换 Docker 后端）
- 可选：Docker

## 安装

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    .venv/bin/harness --help

## 快速开始

CLI（内核管理面）：

    harness compile registry/test-worker.yaml
    harness ps
    harness start <instance_id>
    harness logs <session_id>
    harness preview <session_id>       # 预览装配后的 LLM 上下文
    harness session list

Python SDK：

    from harness.sdk import HarnessClient, HarnessSettings, AgentSpec

    client = HarnessClient(HarnessSettings(hmac_secret="..."))
    client.tools.register(...)          # 可选：注册工具
    result = client.run(spec, "your task", vars={})

完整契约见 docs/developer-sdk-contracts.md 与 docs/behavior-contract.md。

## 项目结构

    harness/           内核实现
      sdk/             公共 SDK（唯一入口）
      cli/             命令行
      agent_runtime/   LLM 循环 + 工具执行
      sandbox/         bubblewrap / docker 后端
      gateway/         统一授权（capability + policy + hooks）
      session/         SessionManager（无状态）
      session_store/   JSONL 持久化
      prompt_assembly/ 组装与预算
      tools/           内置工具
      skills/          skills 注册表
      agent_spec/      YAML 注册表与校验
      orchestration/   pool + model_pool
    tests/             内核测试（不含 app 层）
    registry/          AgentSpec 示例
    docs/              ADR / 契约 / SOP / 归档
    scripts/           本地运维脚本（soft-serve、smoke）
    issues/            文件式 issue tracker
    merge-requests/    MR 记录

## CLI 命令

| 命令 | 说明 |
|---|---|
| compile / rm / ps | AgentSpec → 实例；列表/删除 |
| start / stop / exec | 会话生命周期与消息注入 |
| logs / preview / session | 事件日志、上下文预览、会话管理 |
| gate | Human Gate 决策 |
| audit / agents / skills | 审计日志、实例绑定历史、skills |

## 测试

    make test
    # 或
    .venv/bin/python -m pytest tests -q

已知非 hermetic 用例（真实云端 LLM）：tests/test_e2e.py 的 hashline 集成测试；
全量回归可加 --deselect 剔除。

## 文档

- 术语表：[CONTEXT.md](CONTEXT.md)
- SDK 契约：[docs/developer-sdk-contracts.md](docs/developer-sdk-contracts.md)
- 行为契约：[docs/behavior-contract.md](docs/behavior-contract.md)
- ADR（44 篇内核决策）：[docs/adr/](docs/adr/)
- Agent 协作约定：[docs/agents/](docs/agents/)
- issue/MR 流程：[docs/sop/soft-serve-issue-mr-workflow.md](docs/sop/soft-serve-issue-mr-workflow.md)
- 历史归档：[docs/archive/](docs/archive/)；实测证据：[docs/evidence/](docs/evidence/)
- 变更历史与项目历史摘要：[CHANGELOG.md](CHANGELOG.md)
- 拆分记录：[docs/plans/repo-split-plan.md](docs/plans/repo-split-plan.md)

## 参与贡献与安全

- 贡献流程：[CONTRIBUTING.md](CONTRIBUTING.md)
- 安全报告：[SECURITY.md](SECURITY.md)
- 评审归属：[CODEOWNERS](CODEOWNERS)

## 相关仓库

- pi 应用层：../pi-app（独立仓库；GitHub 发布待定）

## 许可证

MIT License，见 [LICENSE](LICENSE)。
