---
name: state-machine-refactor
description: >
  批量修改重复代码结构时使用。状态机驱动、逐块替换、逐块 diff 验证的方法论。
  适用场景：批量移除try/finally脚手架、批量迁移with语句、批量注入fixtures、
  批量重命名重复模式。触发条件：用户提到"批量修改"、"重复结构"、"逐块替换"、
  "状态机修改"、"try/finally清理"、"缩进安全的重构"，或需要修改同一文件中多处
  同构代码块且正则替换会破坏缩进时。
---

# 状态机驱动的结构代码变换

## 核心原则

用**有限状态机**建模目标代码结构，**逐块执行**替换，**逐块 diff 验证**，**绝不使用正则处理 Python 缩进**。

正则的 `re.DOTALL` 在此场景下是灾难——它跨越块边界、误吞空行、合并相邻函数定义。状态机每次只读一行、只做一个决定，不会跨块污染。

## 适用场景

纯 sed/regex 会失败，但逐块手改太慢的情况：

- 移除 `try:/finally:` 脚手架（手动清理逻辑迁移到自动清理后）
- 迁移 `with open()` → `pathlib.Path.read_text()`
- 给 20 个测试函数加 `monkeypatch` 参数
- 批量注入/移除装饰器
- 批量替换重复的上下文管理器模式

## 工作流

### Step 1: 识别模式

用 `grep -n` 找到所有同构块，确认数量、确认块间不重叠：

```bash
grep -n "_orig = _patch\|try:\|finally:\|_unpatch" tests/target.py
```

输出每块的起止行号。确认块的数量（如"8 个块"）。

### Step 2: 定义状态机

画一个有限状态图，五个状态以内：

```
NORMAL → SAW_PATCH → SAW_TRY → IN_BODY → SAW_FINALLY → NORMAL
```

每个状态只做一件事：
- **读入一行**
- **判断行内容**（是否匹配当前状态的触发条件）
- **决定输出什么**（原样输出 / 修改后输出 / 不输出）
- **转换到下一状态**

写下来：

| 当前状态 | 读入行 | 动作 | 下一状态 |
|---------|--------|------|---------|
| NORMAL | `_orig = _patch(...)` | 输出 `_patch(...)`（去 `_orig =`）+ 空行 | SAW_PATCH |
| NORMAL | 其他 | 原样输出 | NORMAL |
| SAW_PATCH | `try:`（同缩进级） | 不输出 | SAW_TRY |
| SAW_TRY | 任何行 | 去 4 空格缩进后输出 | IN_BODY |
| IN_BODY | 非 `finally:` 行 | 去 4 空格缩进后输出 | IN_BODY |
| IN_BODY | `finally:`（同缩进级） | 不输出 | SAW_FINALLY |
| SAW_FINALLY | `_unpatch(...)` | 不输出 | NORMAL |

### Step 3: 逐块执行

用 `edit` 工具，**一次只替换一个块**。不要写脚本批处理。

每块手动构造 `oldText → newText`：
- 读块的起止行：`read(offset=行号, limit=行数)`
- 构造 newText：去缩进（通常 strip 4 spaces）、去 try/finally 壳、保留所有原始内容
- 用 `edit` 工具执行替换

**关键**：如果块内部有 `def` 或 `class`（嵌套结构），单独构造 `newText`，因为内部缩进层级不同。

### Step 4: 逐块 diff

每替换一个块后立即 `git diff`：

```bash
git diff tests/target.py | head -50
```

检查：
- 删除的行是否正确（`try:`、`finally:`、`_unpatch`、`_orig =`）
- 新增的行是否正确（缩进去掉 4 空格、内容不变）
- 有没有意外合并相邻行（正则的典型失败模式）

如果 diff 不对，`git checkout` 回滚该文件，重新构造该块的 newText。

### Step 5: 全量回归

所有块处理完后：

```bash
python3 -c "compile(open('tests/target.py').read(), 'x', 'exec')"  # 语法检查
pytest tests/target.py tests/related.py -q                          # 行为回归
```

## 常见失败模式

| 现象 | 原因 | 修复 |
|------|------|------|
| `IndentationError: unexpected indent` | 某块去缩进不完整 | `git checkout` 回滚，重读该块，手动构造 |
| 函数定义被合并到上一行 | 正则 `re.DOTALL` 跨块吞噬 | 切回逐块 edit |
| `NameError: monkeypatch not defined` | 某测试函数签名漏加参数 | grep 所有 `def test_`，逐个确认 |
| `AttributeError: _orig not defined` | 某块 `_orig =` 去掉了但后面引用了 | 确认块内无后续引用 `_orig` |

## 示例：批量移除 try/finally

本次会话中实际执行的完整序列，8 个块：

```bash
# Step 1: 识别
grep -n "_orig = _patch\|try:\|finally:\|_unpatch" test_pool_config_change.py
# 输出 8 对 try:/finally: 块

# Step 2: 状态机（见上表）

# Step 3-4: 逐块执行 + diff（8 次 edit + 8 次 git diff）

# Step 5: 回归
python3 -c "compile(open('...').read(), 'x', 'exec')"  # OK
pytest tests/test_pool_config_change.py tests/test_pool.py  # 21 passed
```

结果：零语法错误、零测试回归、零残留 `_orig`/`try`/`finally`/`_unpatch`。