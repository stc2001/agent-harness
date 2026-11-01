---
id: I-75
title: 排除 API key 类隐私数据出 git（gitignore + 仅取消跟踪，不删本地文件）
labels: [tooling]
author: lead
created: 2026-10-21
status: closed
---

## 背景 / What & Why

用户指示：检查 API key 这类隐私数据，应把它们排除出 git；**不删除本地文件**，
用 gitignore 的方式处理。

只读扫描（git grep + 文件名扫描）确认，当前唯一被 git 跟踪的密钥内容是：
docs/harness_doc/deepseek_keys_for_test（1 行 legacy key，自首个提交
2e418b0 起被跟踪，I-72 / python-harness-issues L3 已登记）；同一 key 还以
字面量出现在 tests/conftest.py 与 tests/test_e2e.py 的离线 fallback 中。
session_key、examples/.keys/ 已忽略；harness/config/local.yaml 的
hmac_secret 是文档化的 dev 占位符（mvp-dev-secret-change-in-production，
ADR-015），非 API key，本次不动。

## 范围

1. .gitignore 新增「隐私/凭据」段：显式忽略
   docs/harness_doc/deepseek_keys_for_test，并加通用模式
   .env / .env.local / .env.*.local / *.pem / *.key / *.p12 / *.pfx /
   *.keystore / id_rsa* / id_ed25519* / credentials.json。
2. git rm --cached docs/harness_doc/deepseek_keys_for_test：仅从索引移除，
   **本地文件保留**（36 bytes 仍在磁盘）。
3. 清理代码中的 key 字面量（gitignore 对已跟踪代码无效）：
   - tests/conftest.py 离线 fallback 改为
     os.environ.get("DEEPSEEK_API_KEY", "invalid-hermetic-key")；
   - tests/test_e2e.py 删除 _LEGACY_KEY 字面量，fallback 同上。
   本地仍优先读 examples/.keys/auth（已忽略），行为不变。
4. 不做历史改写；历史中的 key 需先轮换，是否 purge 另案决定（用户/owner）。

## 验收标准

- [ ] git grep -nE 'sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}' 无输出
- [ ] git ls-files docs/harness_doc/deepseek_keys_for_test 为空；
      git check-ignore -v 命中根 .gitignore；本地文件仍存在
- [ ] .venv/bin/python -m pytest tests/test_e2e.py -q：7 passed / 2 skipped /
      1 deselected（该 1 例为已知非 hermetic hashline LLM 测试，见
      python-harness-issues L4）
- [ ] tests/test_skills.py 3 passed；harness/、pi/ 零 diff
- [ ] 合并后 origin/main 不再包含该文件

## 不在范围内 / Out of scope

- 历史重写（filter-repo/BFG）与 key 轮换（owner 决策；I-72 已登记）
- harness/config/local.yaml 的 dev hmac 占位符（ADR-015 已说明）
- harness/config/models.yaml（本地地址，无密钥）

## 关联

- 用户直接指示（2026-10-21）
- I-72、python-harness-issues L3/L4
- I-74（secret-audit skill 首次扫出该 key）
