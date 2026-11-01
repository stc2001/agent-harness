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

## Hermetic tokenizer

CI 的 test job 设置 HARNESS_TOKENIZER_DIR 指向 tests/fixtures/tokenizer
（内含最小 byte-level tokenizer.json）。本机没有 tokenizer_dir/ 时也可用同样方式
让测试脱离真实 model 文件：

    HARNESS_TOKENIZER_DIR=$PWD/tests/fixtures/tokenizer pytest tests -q

## 已知非 hermetic 测试

CI 与本地回归 deselect：

- tests/test_e2e.py::TestHashlineLLMIntegration::test_llm_edits_python_file_with_indentation
- tests/test_agent_runtime.py::TestAgentRuntime::test_run_loop_basic
- tests/test_tools.py::TestRealLLMTools
- tests/test_sandbox.py::TestFirewallPropagation::test_host_network_allows_connections

前三个需要真实云端 LLM（CI 无 key）；最后是 runner 网络限制下的已知 flake。

另外排除环境相关用例：

- tests/test_i33_execguard_feas.py（依赖 bwrap 内 /workspace/guard 等构建产物）
- Python 3.11：tests/test_i34_execguard.py 与
  tests/test_policy_cedar.py::test_guard_to_kernel_end_to_end
  （mephi42-seccomp 无 cp311 发行包；3.13 才安装 seccomp）
