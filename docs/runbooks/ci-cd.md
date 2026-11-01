# CI/CD

## Workflows

| 文件 | 触发 | 内容 |
|---|---|---|
| .github/workflows/ci.yml | push/PR main | Python 3.11/3.13；bubblewrap；ruff 基线；pytest（coverage fail_under=60） |
| .github/workflows/release.yml | v* tag | 构建 sdist/wheel + SHA256SUMS，创建 GitHub Release |

## 本地等价命令

    cd /home/gexingdeyun/OH_workspace
    .venv/bin/ruff check harness tests --select E9,F821
    .venv/bin/python -m pytest tests -q \
      --deselect tests/test_e2e.py::TestHashlineLLMIntegration::test_llm_edits_python_file_with_indentation

coverage 配置在 pyproject [tool.pytest.ini_options] addopts：
--cov=harness --cov-report=term-missing --cov-fail-under=60。
当前全量覆盖率约 83%，阈值 60% 为下限，后续可逐步提高。

## Lint 基线

仓库历史上有约 119 项 F 类 lint 债务，暂不在 CI 中一次性清理；
CI 先强制 E9（语法）与 F821（未定义名）。清理完成后可扩大 select 范围。

## Dependabot

- pip 与 github-actions 分组更新，每周一；
- versioning-strategy: increase-if-necessary：只在当前约束无法满足时才抬高下限，
  避免把 >= 依赖的最小版本无条件抬到最新（含大版本）。

## 已知非 hermetic 测试

tests/test_e2e.py::TestHashlineLLMIntegration::test_llm_edits_python_file_with_indentation
需要真实云端 LLM；CI 与本地回归均 deselect。
