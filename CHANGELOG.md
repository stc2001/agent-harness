# Changelog

本项目采用「latest 快照」发布：公开快照不携带 git 历史。因此本文件除了常规
变更记录外，还提供**项目历史摘要**（按月与里程碑凝练）；完整 git 历史仅保留
在本地私有 soft-serve 仓库。

格式遵循 Keep a Changelog；版本遵循 Semantic Versioning。当前版本 0.1.0，
尚未创建 release tag。

## [Unreleased]

## [0.1.0] - 2026-10-26

首个整合快照（与 pyproject version 0.1.0 对应）。

### Added

- 单机 Agent Harness 核心：agent_runtime、session/session_store、sandbox
  （bubblewrap / docker 后端）、gateway 授权、tools、prompt_assembly、skills 注册表。
- pi 模式通用 agent CLI（app 层）与记忆压缩状态机（2026-11-01 拆至独立仓库 pi-app）。
- soft-serve 文件式 issue/MR 工作流：issues/I-NN、merge-requests/I-NN、
  pre-receive 分支保护与 tracker-only 放行、push 审计日志。
- 46 篇 ADR、docs/agents、docs/sop、docs/evidence 等治理文档。
- 7 个 workspace 治理 skills（trace/commit/MR/audit/secret/deps/governance-files）
  与对应脚本、模板。
- 本地 pre-push 护栏（.githooks，不入库）：阻止非本地远端推送全量历史。

### Changed

- 移除固定 DAG 编排（ADR-038），改为 APP 决定调度、框架提供原语。
- SDK 作为唯一切入点（ADR-015）；prompt 组装提权到 SDK（ADR-022）。
- 策略中间件与工具执行期强制（ADR-048 / ADR-051）。
- 仓库拆分：pi 应用层移出本仓库，独立为同级 pi-app 仓库（I-91，2026-11-01）。
- CI/CD：新增 GitHub Actions 测试矩阵（Python 3.11/3.13）与 Release 工作流；
  Dependabot 改为 increase-if-necessary 分组更新（I-95）。

### Fixed

- 当前任务按轮截断与装配期 fail loudly（ADR-053 / I-45）。
- 工具输出限长、tokenizer 回退零值、测量口径等系列修复（I-51…I-72）。
- 仓库治理漂移：TODO 归档、docs/agents 约定更新、证据目录归位（I-76…I-87）。

### Security

- 移除被跟踪的 legacy key 文件，测试字面量改为环境变量/占位符（I-75）；
  历史仅保留在本地私有仓库，不随快照发布。
- 非本地远端推送默认拒绝全量历史（.githooks/pre-push，I-88）。

## Project history (2026-05-17 → 2026-10-26, 739 commits)

| 月份 | 提交数 | 主题 |
|---|---:|---|
| 2026-05 | 132 | MVP Phase 1 模块、refactor 001–003、基础 harness 骨架 |
| 2026-06 | 141 | 架构解耦、DAG/Orchestrator 移除（ADR-038）、hooks/tool registry |
| 2026-07 | 22 | 低活跃期：收尾与小修 |
| 2026-08 | 39 | 任务边界与崩溃恢复（ADR-039/040）、reasoning 契约（041）、hooks 统一（042）、生命周期（043）、Stop 双轨（044） |
| 2026-09 | 93 | SDK/CLI 观测、计量与截断、tokenizer/本地后端、策略执行期（ADR-045–055） |
| 2026-10 | 312 | soft-serve issue/MR 工作流（I-44 起）、pi CLI、记忆压缩、治理整理与 skills（I-71–I-89） |

### Milestones

| 日期 | 里程碑 |
|---|---|
| 2026-05-17 | first commit：MVP 骨架 |
| 2026-06-18 | ADR-038：移除固定 DAG 编排 |
| 2026-08-03 | ADR-039：任务边界与崩溃恢复 |
| 2026-10-02 | I-44：issue → MR 工作流在 soft-serve 落地；I-45–I-50：截断/后端/tokenizer/SOP |
| 2026-10-19 | I-51–I-72：计量与截断、策略、SDK 公开面、pi CLI、记忆压缩、工具输出限长 |
| 2026-10-20 | I-73：agent 指令 AGENTS.md 入库 |
| 2026-10-21 | I-74–I-76：治理 skills、密钥移出索引、TODO/ 归档 |
| 2026-10-25 | I-84–I-87：tracker/domain 文档修正、sessions 语义澄清、个人学习目录与证据目录整理 |
| 2026-10-26 | I-88：本地 pre-push 护栏；I-89：MIT LICENSE + 本 CHANGELOG |
| 2026-11-01 | I-91：pi 应用层拆分为独立仓库 pi-app（latest 快照起点） |

> 逐 issue 细节见 issues/（I-44…I-91）；架构决策见 docs/adr/（44 篇 kernel ADR）；
> 历史已解决清单见 docs/archive/solved-issues-through-I-43.md。
> pi 应用层历史见 ../pi-app/CHANGELOG.md。
