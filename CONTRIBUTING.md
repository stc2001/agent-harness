# Contributing

本仓库是单机 Agent Harness **内核**（Python >= 3.11）；上层应用 pi 在独立的
pi-app 仓库。所有内核变更——包括 agent 生成的变更——都遵循同一套 issue → MR 流程。

## 工作流

1. 先建 issue：复制 issues/TEMPLATE.md 为 issues/I-NN-slug.md，填写 frontmatter 与
   可判定的验收标准；标签只用 docs/agents/triage-labels.md 的六种。
2. 从 main 建分支：mr/I-NN-slug。
3. 提交：<type>(<scope>): I-NN <summary>（type: feat/fix/perf/refactor/docs/test/ci/build/chore）。
4. 推送分支：git push -u origin mr/I-NN-slug（本地 soft-serve，ssh://localhost:23231）。
5. 在 merge-requests/I-NN.md 记录 MR（head / 自检 / 评审表），请求评审。
6. 评审通过后 merge --no-ff 到 main，并更新 MR（merged_as）与 issue（closed）。

细节见 docs/sop/soft-serve-issue-mr-workflow.md 与 issues/README.md。

## 规则

- main 受服务端 pre-receive 保护：只接受 merge commit 或 tracker-only（issues/**、merge-requests/**）提交。
- 禁止对 main force-push；禁止 rebase force-push 已评审的 MR 分支（会抹掉评审记录）。
- 提交前运行测试：make test（或针对性的 pytest 节点）；lint：make lint。
- 不提交密钥/令牌、大文件、.soft-serve/、sessions/、.scratch/、.venv/。
- 涉及架构决策的变更先写/更新 ADR（docs/adr/<type>/），并在 commit/issue 中引用编号。
- 文档与代码同 commit 更新；用户可见行为变更同步 CHANGELOG.md。

## 本地环境

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    make test

沙箱默认使用 bubblewrap；Docker 后端需本机 Docker。测试中的已知非 hermetic 用例见
python-harness-issues.md L4。

## 安全

漏洞与凭据问题不要公开披露；报告方式见 SECURITY.md。提交前可运行：

    bash .dsh/skills/secret-audit/scripts/secret_scan.sh

## 许可证

贡献即表示同意以 MIT License（见 LICENSE）发布。
