# hashline_read 结构增强 — 执行计划

## Status

Draft — pending TDD implementation.

## Context

`hashline_read` 当前行为：读取文件全部内容，逐行标注 hash。LLM 调用 `bash "find /"` 等命令时，工具返回 586KB+ 原始输出直接写入 token budget，造成 KV cache 污染和上下文耗尽。

**根因**：工具层对返回结果无大小控制，prompt_assembly 也无截断。

**解决方向**：让 `hashline_read` 成为结构感知的文件读取器——默认返回符号摘要（类似 Aider 的 repo-map），LLM 按需精准拉取具体定义/段落/行范围。

参考：Aider repo-map（tree-sitter 符号提取）、Claude Code LSP tool（符号导航）。

## Decision

### 新增参数

```
hashline_read(path, range=None, function=None, heading=None, max_desc=150)
```

### 返回路由

| 参数 | 行为 | 格式 |
|------|------|------|
| 无 | 结构摘要 | 函数/类/标题树 + 首段简介（≤max_desc 字符） |
| `function="X"` | 按名查定义 | 完整定义（带 hash 标注） |
| `heading="# A"` | 按标题查段落 | 标题下全文至下一同级标题 |
| `range="12:30"` | 行范围 | 现有 hashline 格式 |

### 解析器

| 解析器 | 文件类型 | 实现 |
|--------|---------|------|
| `_python_summary` | `.py` | `ast` 模块：类/函数/方法定义 + docstring |
| `_markdown_summary` | `.md` | 正则 `^#{1,6}\s` 标题树 + 首段 |
| `_plain_truncate` | 其他 | 截断到 max_desc×3，标注 `[... truncated N chars]` |

解析器可插拔：后续用 tree-sitter 替换 `ast`/正则，API 不变。

### Python 摘要格式

```
Go2Policy(BasePolicy)                     → BasePolicy
  "Heuristic locomotion policy for Go2..."
  step(obs: dict) → dict                   :48
    "Compute control outputs from..."
  reset()                                  :75
    "Reset internal state between..."
```

规则：
- `class Name(Bases) → Bases`，继承写一行
- 方法/函数：`name(args) → returns`，`:` 后跟行号
- docstring 第一行作为简介，截断到 `max_desc`
- 装饰器保留（如 `@dataclass`）
- 嵌套类/方法缩进一层

### Markdown 摘要格式

```
# 架构概述                               :1
  系统由三层组成：Gateway 授权层...
## Gateway                                :15
  统一授权引擎，收敛 cap_token...
### PreHook                               :42
  授权前拦截，可返回 AuthorizeResult...
```

规则：
- `#` 层级用缩进表示
- `:N` 为标题所在行号
- 首段正文作为简介，截断到 `max_desc`

### 文件结构

```
harness/tools/
├── hashline.py          # hashlin_read 路由 + 改造签名
├── _structure.py        # 新增：解析器模块
```

### 变更范围

| 文件 | 操作 | 内容 |
|------|------|------|
| `harness/tools/_structure.py` | **新建** | `parse_file_structure`, `_python_summary`, `_markdown_summary`, `_plain_truncate` |
| `harness/tools/hashline.py` | **修改** | `hashline_read` 新增参数 + 路由逻辑；新增 `_find_definition`, `_find_section`, `_read_range` |
| `harness/tools/hashline.py` | **不变** | `hashline_edit`, `_hash_line`, `_read_with_hashes` |
| `tests/test_structure_read.py` | **新建** | 12 个 TDD 测试 |

### 不变约束

- `register(registry)` 壳不变（工具名 `hashline_read` 不变，LLM 无感知重训）
- 现有 `test_builtin_tools.py` 全部通过
- `hashline_edit` 零改动

## 测试矩阵（TDD，12 个）

| # | 测试 | 输入 | 期望 |
|---|------|------|------|
| 1 | Python 类摘要 | `class Foo(Base): ...` | `Foo(Base) → Base\n  "doc..."` |
| 2 | Python 方法/函数 | `def step(obs): ...` | `step(obs)\n  "doc..."` + 行号 |
| 3 | 装饰器保留 | `@dataclass\nclass X` | 摘要含 `@dataclass` |
| 4 | 继承链 | `class A(B, C):` | `A(B, C) → B, C` |
| 5 | 无 docstring | 空函数体 | 有行号，无 description |
| 6 | MD 标题树 | `# H1\ncontent\n## H2` | 层级 + 行号 + 首段 |
| 7 | MD 嵌套层级 | `# A\n## B\n### C` | 正确缩进层级 |
| 8 | 纯文本截断 | 5000 字无结构 | ≤450 chars + `[... truncated]` |
| 9 | `function="X"` 查定义 | `.py` 文件 | 完整类/函数定义（hashed） |
| 10 | `heading="# A"` 查段落 | `.md` 文件 | 该标题下全文（hashed） |
| 11 | `range="5:10"` 行范围 | 任意文件 | 现有 hashline 格式 |
| 12 | 无参兼容 | `.py` 文件无参调用 | 返回摘要，不抛异常 |

## LLM 使用流程

```
1. LLM: read_file("src/policy.py")
   → hashline_read 返回摘要（函数/类树）
2. LLM 判断需要 Go2Policy 的完整定义
   → hashline_read("src/policy.py", function="Go2Policy")
   → 返回 Go2Policy 类完整定义（带 hash）
3. LLM 需要修改 step 方法
   → hashline_edit("src/policy.py", [{"op": "replace", "anchor_hash": "a3", ...}])
```

## Consequences

### Positive

- 消灭 586KB+ 的 bash `find /` 输出污染 token budget
- LLM 拿到结构化摘要后可精准决策读什么
- `function=`/`heading=`/`range=` 三个参数覆盖所有精确读取场景
- 解析器可插拔（后续 tree-sitter 零 API 变更）
- 现有 `hashline_edit` 工作流不受影响

### Negative

- 复杂度增加：hashline.py 约 +60 行，_structure.py 约 +150 行
- Python `ast` 只覆盖 `.py`（90% 场景），其他语言走纯文本截断

## References

- Aider repo-map 设计：https://aider.chat/2023/10/22/repomap.html
- 触发 Issue：evaluator bash `find /` 返回 586,625 字符 → KV cache 污染
