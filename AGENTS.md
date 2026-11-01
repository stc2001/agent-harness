# AGENTS.md — agent-harness

> 本文件是 agent 在本仓库工作的操作手册（AGENTS.md = README for agents）。
> 人类向文档见 README.md；应用层 pi 在 ../pi-app（独立仓库，有自己的 AGENTS.md）。

## 项目定位与边界

- 本仓库只有 **harness kernel**：SDK、CLI、sandbox、gateway、session/session_store、
  prompt_assembly、tools、skills、agent_spec、orchestration。
- 依赖方向：pi → harness.sdk；harness 不依赖 pi。不要把 pi 的代码/文档复制回来。
- 无固定 DAG：框架提供原语，APP 决定调度（ADR-038）。

## 环境

- Python >= 3.11；Linux + bubblewrap，可选 Docker。
- 首次安装：

      python3 -m venv .venv
      .venv/bin/pip install -e ".[dev]"

- fallback tokenizer 需要 transformers：本地可用 `.[local]` 或 `.[dev]`。
- `harness/config/models.yaml` 是本机未跟踪配置，不要提交；
  `harness/config/local.yaml` 仅含开发占位 hmac，不要写入真实密钥。

## 常用命令

| 目的 | 命令 |
|---|---|
| 全量测试（CI 同款） | 见下方 test 命令 |
| lint 基线 | `.venv/bin/ruff check harness tests --select E9,F821` |
| 仓库卫生审计 | `python3 .dsh/skills/repo-hygiene-audit/scripts/repo_audit.py` |
| 本地 soft-serve | `./.soft-serve/start.sh`（后台） |
| GitHub 发布 | `scripts/publish_github.sh` |

测试命令（coverage 阈值 60% 由 pyproject addopts 提供；当前约 83%）：

      .venv/bin/python -m pytest tests -q \
        --deselect tests/test_e2e.py::TestHashlineLLMIntegration::test_llm_edits_python_file_with_indentation \
        --deselect tests/test_agent_runtime.py::TestAgentRuntime::test_run_loop_basic \
        --deselect tests/test_tools.py::TestRealLLMTools \
        --deselect tests/test_sandbox.py::TestFirewallPropagation::test_host_network_allows_connections \
        --ignore=tests/test_i33_execguard_feas.py

CI 还使用 Python 3.11/3.13 矩阵；3.11 额外跳过 seccomp 相关用例
（mephi42-seccomp 无 cp311 包），详见 docs/runbooks/ci-cd.md。

## 工作流（必须）

本仓库用文件式 issue/MR（soft-serve）跟踪；代码变更不要直接提交 main：

1. 先建 issue：复制 `issues/TEMPLATE.md` 为 `issues/I-NN-slug.md`，写可判定的验收标准。
2. 从 main 建分支：`mr/I-NN-slug`。
3. 提交：`<type>(<scope>): I-NN <summary>`（feat/fix/docs/test/chore 等）。
4. 推送分支（origin = 本地 soft-serve）；在 `merge-requests/I-NN.md` 记录 head/自检/评审。
5. 评审通过后 `git merge --no-ff` 到 main；更新 MR 的 `merged_as` 与 issue 的 `status: closed`。
6. 只有 tracker-only（issues/**、merge-requests/**）可以直接提交 main。

## main 保护与 GitHub 发布

- GitHub main 已启用 ruleset：**禁止 force push、禁止删除**；正常 fast-forward 追加。
- GitHub 发布只允许通过 `scripts/publish_github.sh`：fetch 远端 tip → 以远端 tip 为父、
  本地 HEAD tree 为内容生成同步提交 → fast-forward push。不要 force，不要删除远端分支。
- 本地 739+ 提交历史（含旧 key）**永远不推送**到 GitHub；GitHub 只保留从快照起的历史。
- 本机加速器会劫持 github.com:22，GitHub remote 使用 `ssh.github.com:443`；
  不要改回 github.com:22，也不要把 token 写进 `.githooks` 或仓库文件。
- `.githooks/pre-push` 是本地未版本化护栏：允许 release 快照及其追加链，拒绝本地全量历史；
  不要提交 `.githooks/`。

## 代码约定

- Python 3.11+；使用 type hints 与 `from __future__ import annotations`；行宽 100。
- 新依赖必须写进 pyproject（运行时进 dependencies，仅测试进 dev extra）；CI 在干净环境安装。
- 公共 SDK/事件/行为变更：同步更新 docs/developer-sdk-contracts.md、docs/behavior-contract.md；
  架构决策写 docs/adr/；用户可见行为写 CHANGELOG.md。
- 测试必须 hermetic：mock LLM/网络/时间；不要依赖本机 tokenizer_dir、真实 key 或本地服务。
- 不要为了过 lint 一次性重写历史代码；当前 CI 只强制 E9/F821，其余 F 类债务另行处理。
- sandbox/gateway 是 fail-closed 安全边界：不要为方便绕过策略、capability token 或沙箱挂载限制。

## 文档地图

| 文档 | 用途 |
|---|---|
| CONTEXT.md | 领域词汇表 |
| docs/README.md | 文档索引与边界 |
| docs/adr/ | 44 篇内核 ADR |
| docs/developer-sdk-contracts.md / docs/behavior-contract.md | SDK 与行为契约 |
| docs/agents/ | domain / issue-tracker / triage-labels |
| docs/sop/ | issue/MR、并发、测试 SOP |
| docs/runbooks/ci-cd.md | CI/Release/Dependabot/非 hermetic 清单 |
| docs/runbooks/github-publish.md | GitHub fast-forward 发布 |
| docs/archive/ | 历史归档（不要改写） |
| docs/plans/repo-split-plan.md | harness/pi 拆分记录 |

## Agent 完成定义

- issue/MR 已更新（head、自检、评审、merged_as、issue closed）；
- 测试与 lint 基线通过；新增行为有测试；必要时更新文档/ADR/CHANGELOG；
- 需要上 GitHub 时用 publish 脚本追加，并报告本地/远端 commit SHA；
- 不提交密钥、sessions/、runs/、`.soft-serve/`、`.venv/`、本机 `models.yaml`。

<!-- CODEGRAPH_START -->
## CodeGraph

This project has a CodeGraph MCP server (`codegraph_*` tools) configured. CodeGraph is a tree-sitter-parsed knowledge graph of every symbol, edge, and file. Reads are sub-millisecond and return structural information grep cannot.

### When to prefer codegraph over native search

Use codegraph for **structural** questions — what calls what, what would break, where is X defined, what is X's signature. Use native grep/read only for **literal text** queries (string contents, comments, log messages) or after you already have a specific file open.

| Question | Tool |
|---|---|
| "Where is X defined?" / "Find symbol named X" | `codegraph_search` |
| "What calls function Y?" | `codegraph_callers` |
| "What does Y call?" | `codegraph_callees` |
| "How does X reach/become Y? / trace the flow from X to Y" | `codegraph_trace` (one call = the whole path, incl. callback/React/JSX dynamic hops) |
| "What would break if I changed Z?" | `codegraph_impact` |
| "Show me Y's signature / source / docstring" | `codegraph_node` |
| "Give me focused context for a task/area" | `codegraph_context` |
| "See several related symbols' source at once" | `codegraph_explore` |
| "What files exist under path/" | `codegraph_files` |
| "Is the index healthy?" | `codegraph_status` |

### Rules of thumb

- **Answer directly — don't delegate exploration.** For "how does X work" / architecture questions, answer with 2-3 codegraph calls: `codegraph_context` first, then ONE `codegraph_explore` for the source of the symbols it surfaces. For a specific **flow** ("how does X reach Y") start with `codegraph_trace` from→to — one call returns the whole path with dynamic hops bridged — then ONE `codegraph_explore` for the bodies; don't rebuild the path with `codegraph_search` + `codegraph_callers`. Codegraph IS the pre-built index, so spawning a separate file-reading sub-task/agent — or running a grep + read loop — repeats work codegraph already did and costs more for the same answer.
- **Trust codegraph results.** They come from a full AST parse. Do NOT re-verify them with grep — that's slower, less accurate, and wastes context.
- **Don't grep first** when looking up a symbol by name. `codegraph_search` is faster and returns kind + location + signature in one call.
- **Don't chain `codegraph_search` + `codegraph_node`** when you just want context — `codegraph_context` is one call.
- **Don't loop `codegraph_node` over many symbols** — one `codegraph_explore` call returns several symbols' source grouped in a single capped call, while each separate node/Read call re-reads the whole context and costs far more.
- **Index lag — check the staleness banner, don't guess a wait.** When a codegraph response starts with "⚠️ Some files referenced below were edited since the last index sync…", the listed files are pending re-index — Read those specific files for accurate content. Files NOT in that banner are fresh and codegraph is authoritative for them. `codegraph_status` also lists pending files under "Pending sync".

### If `.codegraph/` doesn't exist

The MCP server returns "not initialized." Ask the user: *"I notice this project doesn't have CodeGraph initialized. Want me to run `codegraph init -i` to build the index?"*
<!-- CODEGRAPH_END -->
