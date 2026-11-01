# ADR-003：同步调用 vs asyncio

**日期**：2026-05-12  
**状态**：已采纳  

## 上下文

模块间通信方式的选择。候选：
- **asyncio**：异步事件循环，Coordinator ↔ Orchestrator 间通过 `asyncio.Queue` 通信
- **同步调用**：Python 直接函数调用，参数传递，返回值接收

## 决策

**选择同步函数调用。**

## 理由

1. **AI4S 流水线的天然串行依赖**。Phase 内的节点依赖关系决定了必须等待上游完成，asyncio 的并发优势无法发挥。
2. **Agent 的 LLM 调用是阻塞 I/O**。AgentRuntime 的 `execute_task` 等待 LLM API 响应，不是事件驱动模型。用 asyncio 包装阻塞调用增加复杂度而无收益。
3. **已被工业实践验证**。OpenHands V1 使用同步模型处理类似 Agent 编排，OpenClaw 的 Gateway 也是同步 daemon。
4. **代码简洁**。同步调用无需 async/await 着色、无需事件循环管理、调试堆栈清晰。

## 后果

- 代码量减少（无 async/await 污染）
- 并行执行通过 `ThreadPoolExecutor` 在同步框架上实现（已验证可行）
- 未来如果需要真正的异步 I/O（如 WebSocket 推送），可以引入 asyncio 但仅限特定模块
