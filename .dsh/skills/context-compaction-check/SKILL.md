---
name: context-compaction-check
description: "大型项目上下文压缩后的状态确认标准操作程序。当用户说'压缩了上下文'、'上下文被压缩'、'重新确认状态'、'检查记忆是否遗漏'时触发。适用于代码量>5000行、模块数>5、测试数>50的项目。强制执行三层验证：文档层（ADR/TODO/术语表）、代码层（文件存在性/函数签名/密钥）、验证层（测试计数/回归/已知flake）。不确认不编码。"
---

# 上下文压缩后状态确认 SOP

**触发条件**：对话历史被压缩后，任何涉及代码修改、架构决策或项目状态的对话之前。

**核心原则**：压缩会丢弃细节，以下信息极易失真——不确认不编码。

---

## 立即执行（1-2 分钟）

### Step 1: 文档层验证

```bash
# ADR 完整性
find docs/adr -name "*.md" | sort

# TODO 状态
cat TODO.md | head -50

# 领域词汇表
cat docs/agents/domain.md 2>/dev/null || cat CONTEXT.md | head -30

# README 关键信息（项目名称、版本、最近变更）
grep -E "^# |^## |版本|Version|^\*\*" README.md 2>/dev/null | head -15

# pyproject.toml 元数据（依赖变化、Python 版本要求）
grep -E "^name|^version|^requires-python|^dependencies" pyproject.toml 2>/dev/null
```

| 检查项 | 通过标准 |
|--------|---------|
| ADR 数量 | 编号连续，数量与记忆一致 |
| 最新 ADR | 能复述决策、理由、后果，无"似曾相识但记不清" |
| TODO | 已完成项标 ✅，待做项无已完成内容残留 |
| 术语表 | 定义与当前实现一致 |
| README | 项目名称、版本、关键特性与记忆一致；开发环境配置段落存在 |
| pyproject.toml | 依赖列表无意外增减，Python 版本要求未变 |

### Step 2: 代码层验证

```bash
# 核心文件存在性
ls -lt harness/**/*.py tests/**/*.py | head -15

# 最近修改文件（与记忆中"最近做了什么"交叉核对）
ls -lt harness/**/*.py tests/**/*.py 2>/dev/null | head -5

# 密钥硬编码检查
grep -rn "sk-" harness/cli/ harness/agent_runtime/ 2>/dev/null || echo "clean"

# 最近重构痕迹（如有 git）
git diff --name-only HEAD~1 2>/dev/null || echo "no git"

# README 开发环境配置段落是否存在
grep -n "venv\|virtualenv\|pip install\|开发环境\|Development" README.md 2>/dev/null || echo "No dev env section"
```

| 检查项 | 通过标准 |
|--------|---------|
| 文件路径 | 与记忆一致，无"以为在 A 实际在 B" |
| 函数签名 | 参数、返回值与 ADR/设计文档一致 |
| 硬编码密钥 | 生产代码中无硬编码（测试 fixtures 除外） |
| 重构痕迹 | 确认重构范围，无"以为拆了实际没拆" |
| 最近修改文件 | 与记忆中"最近做了什么"交叉核对 |

### Step 3: 验证层验证

```bash
# 测试计数
pytest tests/ --collect-only -q 2>/dev/null | tail -1

# 快速回归（排除 E2E）
pytest tests/ -q --ignore=tests/test_e2e.py 2>&1 | tail -3
```

| 检查项 | 通过标准 |
|--------|---------|
| 测试计数 | 与 TODO / 记忆中最后一次数字一致 |
| 全量回归 | 全部通过，失败数 ≤ 已知 flake 数 |
| 新增测试 | 能说出每个新增测试验证什么 |
| 已知 flake | 原因和修复状态清晰 |

---

## 失败处理

```
Layer 1 失败 → 修复文档，标记 delta，重写 ADR 摘要
Layer 2 失败 → 回读代码，更新记忆，必要时写新 ADR
Layer 3 失败 → 立即停止编码，诊断根因
             → 测试数不对：检查文件是否丢失/未保存
             → 新失败出现：回读相关文件重建上下文
```

---

## 快速脚本（可复用）

保存到 `scripts/compaction_check.sh`：

```bash
#!/bin/bash
echo "=== ADR count ==="
find docs/adr -name "*.md" | sort

echo "=== Latest 3 ADRs ==="
ls -lt docs/adr/**/*.md 2>/dev/null | head -3

echo "=== README 关键信息 ==="
grep -E "^# |^## |版本|Version|^\*\*" README.md 2>/dev/null | head -10

echo "=== pyproject.toml 元数据 ==="
grep -E "^name|^version|^requires-python" pyproject.toml 2>/dev/null

echo "=== Test count ==="
pytest tests/ --collect-only -q 2>/dev/null | tail -1

echo "=== Quick smoke ==="
pytest tests/ -q --ignore=tests/test_e2e.py 2>&1 | tail -3

echo "=== Recent files ==="
ls -lt harness/**/*.py tests/**/*.py 2>/dev/null | head -10

echo "=== Secret check ==="
grep -rn "sk-" harness/cli/ harness/agent_runtime/ 2>/dev/null || echo "No hardcoded keys"

echo "=== README dev env check ==="
grep -n "venv\|virtualenv\|pip install\|开发环境" README.md 2>/dev/null || echo "No dev env section"
```

---

## 输出格式

确认完成后，向用户报告：

```
已确认，记忆准确，无遗漏。

| 层级 | 检查项 | 结果 |
|------|--------|------|
| Layer 1 | ADR 数量 | N 个，编号连续 |
| Layer 1 | 最新 ADR | XXX — 内容已复核 |
| Layer 1 | TODO 状态 | P0-X 已标记 ✅ |
| Layer 2 | 文件路径 | 与记忆一致 |
| Layer 2 | 最近修改 | pool.py (2天前), client.py (今天) |
| Layer 2 | 硬编码密钥 | [clean / 仍有 XXX 在 YYY 行] |
| Layer 3 | 测试数 | N 个，0 失败 |
| Layer 3 | E2E 状态 | X/Y 通过 |
| Layer 3 | README dev env | [存在 / 缺失] |

[如有问题，列出待修复项]
```

---

## 关键记忆锚点

**测试计数是最可靠的锚点**。如果压缩后你记得「118 个测试」但实际 138 个——你做了一堆事但被压缩了；反过来如果记得 138 实际 118——文件丢了。这个数字是「状态是否漂移」的最快指标。