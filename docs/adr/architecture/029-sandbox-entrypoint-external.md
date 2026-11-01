# ADR-029: 沙箱 entrypoint 外部化 — 消除 L1→L2 依赖

**日期**: 2026-05-22  
**状态**: 已接受  

## 上下文

`harness/sandbox/impl.py`（沙箱层，L1）中的 `_build_runtime_script()` 内嵌了一个 ~80 行的 Python f-string 模板，包含 `from harness.agent_runtime.impl import AgentRuntime`。这导致沙箱层直接依赖 Agent 运行时层（L2），违反了分层架构。

bwrap 的入口机制通过 `python -c "{inline_script}"` 执行这段内嵌代码。技术上可行但架构上不合理——沙箱不应知道容器内运行的具体代码。

## 决策

**将 entrypoint 脚本提取为独立文件 `harness/agent_main.py`。沙箱层只引用文件路径，不包含代码内容。**

```
修复前：                              修复后：
sandbox/impl.py                      sandbox/impl.py
  _build_runtime_script() ← 80行模板    spawn():
    → python -c "{script}"              → python harness/agent_main.py
                                        （只知道文件路径）
                                      agent_main.py ← 独立文件
                                        包含 AgentRuntime 启动逻辑
```

与 Docker 的架构一致：daemon（containerd/runc）不知道容器 entrypoint 的内容，只负责启动。

## 后果

- 沙箱层的 `_build_runtime_script()` 函数删除（~80 行），`import AgentRuntime` 从沙箱层移除
- 新增 `harness/agent_main.py`（~85 行）作为独立入口模块
- 测试无需修改——沙箱通过 `config.command` 参数注入脚本的能力保持不变
- 分层更清晰：L1（沙箱）只做 namespace 隔离，L2（agent）的入口脚本独立管理
