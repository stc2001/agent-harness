# Repo Split Plan — harness / pi

- 日期：2026-11-01
- 状态：已执行（本仓库侧）
- 记录：[issues/I-91-split-pi-repo.md](../../issues/I-91-split-pi-repo.md)

## 结论

harness 内核保留在 OH_workspace；pi 应用层拆到工作区外
/home/gexingdeyun/pi-app（独立 git 仓库，latest 快照初始提交）。

依赖方向：pi → harness（仅 harness.sdk 公开 API）；harness 不依赖 pi。

## 去向

| 内容 | 去向 |
|---|---|
| pi/ 包（CLI、config、memory、policy、runtime、tools、defaults） | pi-app/pi/ |
| 11 个 tests/test_pi_*.py + pi_fake_llm.py + tests/fixtures/tokenizer | pi-app/tests/ |
| docs/adr/app/001、002 | pi-app/docs/adr/（保持 001/002 编号） |
| docs/runbooks/memory-compaction.md | pi-app/docs/runbooks/ |
| pi 的 README/CHANGELOG/LICENSE/CONTRIBUTING/SECURITY/CODEOWNERS/AGENTS/pyproject | pi-app/ 新建 |
| 其余内核代码、44 篇 kernel ADR、registry、examples、tracker、SOP | 留在本仓库 |

## 接口约定

- pi 依赖 agent-harness 的 SDK；发布后以 git/PyPI 版本固定。
- app 层 ADR 在 pi 仓库；引用内核 ADR 用 harness:ADR-NNN。
- 内核 ADR/契约是单一事实源，pi 不复制。

## 验证

- pi-app：pytest 87 passed（复用内核 venv 的 agent-harness）。
- 本仓库：删除 pi 后 pytest 通过；repo_audit WARN=0。
- 本仓库 README 已重写（移除 DAG/coordinator/orchestrator 过时内容与死链）。

## 后续

1. 用户提供 GitHub 账号后：把 pi-app 与 OH_workspace 分别发布为 latest 快照。
2. GitHub 上 pi 使用原生 issue/PR；harness 保留文件式 tracker（soft-serve）。
3. pi 的依赖从本地路径切换为 GitHub tag/commit。
