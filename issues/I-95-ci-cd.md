---
id: I-95
title: GitHub CI/CD 完善：Actions 测试矩阵 + Release + Dependabot 策略
labels: [needs-triage]
author: lead
created: 2026-11-01
status: closed
---

## 背景 / What & Why

GitHub agent 已在远端 main 添加 2 个文件（c169257 .github/dependabot.yml、
1b91d2b pyproject coverage addopts），但**没有 GitHub Actions workflow**，CI 未真正运行；
Dependabot 用默认 increase 策略产生了 PR #1（把 >= 下限整体抬高，含 openai 3.x 大版本）。

## 范围

1. 新增 .github/workflows/ci.yml：
   - push/pr main；permissions contents: read；concurrency 取消旧运行；
   - matrix Python 3.11 / 3.13；安装 bubblewrap；pip install -e ".[dev]"；
   - lint 基线 ruff --select E9,F821（先清掉 4 个 F821 未定义名）；
   - pytest（deselect 已知非 hermetic hashline 用例；coverage fail_under=60 继承 pyproject）。
2. 新增 .github/workflows/release.yml：v* tag 构建 sdist/wheel 并创建 GitHub Release。
3. 改进 .github/dependabot.yml：
   - versioning-strategy: increase-if-necessary（避免无意义的 >= 下限抬升）；
   - pip 与 github-actions 分组、每周一次、open-pull-requests-limit 5。
4. 修复 4 个 F821：sandbox/impl.py Any、sdk/client.py SandboxConfig/Callable、
   tests/test_tools.py fixture 返回类型。
5. 本地同步远端 agent 提交内容（dependabot.yml、pyproject addopts）；
   merge 后用 scripts/publish_github.sh 正常追加到 GitHub。
6. 处理 PR #1：Dependabot 旧策略产生的纯下限抬升 PR；新策略下不再需要，
   关闭（删除 dependabot 分支即自动关闭 PR）。若需要真实升级，后续按需手动做。

## 验收标准

- [ ] YAML 可解析；ci.yml/release.yml 存在
- [ ] ruff --select E9,F821 通过；pytest（deselect）通过；coverage >= 60
- [ ] GitHub main 以 fast-forward 追加包含 workflows 的提交（父=1b91d2b）
- [ ] PR #1 已关闭（分支已删）
- [ ] repo_audit WARN=0

## 不在范围内

- 清理全部 119 项 ruff F 类历史债务（另开 issue）
- 配置 PyPI 发布密钥（GitHub Release 先做）

## 关联

- I-93（GitHub 追加发布）；用户指示（2026-11-01）；PR #1
