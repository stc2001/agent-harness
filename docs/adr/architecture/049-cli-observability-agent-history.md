# ADR-049：CLI 观测契约（agent/human 双模）与 Agent 绑定历史查询（I-32）

## Status

Accepted (2026-09-07)

## Context

观测输出同时服务人类与 agent（开发者 agent 以 CLI 为事实源）：需机器可解析且人类可读；且按 agent 名称（而非 uuid）查询其历史 session/sandbox 绑定缺少持久图数据。

## Decision

### 1. CLI 输出双模契约（logs / session show / agents）

- 默认 agent 模式：首行单条 JSON 摘要（truncated bool/计数/直方图/holder/…）后接详情窗口（--tail）。
- `--user_type human`：markdown 摘要 + 全量详情。
- `--out-file PATH`：摘要+全量落盘；`--follow` 流式。
- CLI 只调用 SDK（ADR-015）。

### 2. Agent 绑定历史 = session×sandbox 二部图（I-32）

- 边 = (agent, instance_id, session_id, sandbox_id, first_ts, last_ts)；同三元组重绑合并 last_ts、保留 first_ts。
- 存储：session meta.json.binding_history（时间序）+ 顶层 sandbox_id（最近）；事件流 append-only 不变。
- SDK：agents.history_sessions/history_sandboxes/history_bindings(agent_name, session_id)（agent 必填；bindings 仅该 agent 该 session 的边）。
- CLI：harness agents --name A --history_sessions | --history_sandboxes | --history_bindings --session SID，双模输出。
- 正确性：差分随机测试（DRT）——内存 sqlite 存同边 reference，多种子比对（含过滤/去重/排序）。

> **2026-09-30 注记（I-42）**：观测读面与语义读面已二分——CLI `logs` 默认语义视图，`--audit` 显示物理日志；命名与实现规则见 ADR-050 I-42 注记。

> **2026-09-30 注记（I-43）**：新增 `harness preview`（组装产物预览，与 SDK `sessions.preview_llm_messages` 同源）；读面三分见 ADR-050 I-43 注记。

## Consequences

- 正面：agent 可直接消费 CLI 观测；按名检索历史绑定；DRT 保障复合查询。
- 代价：meta.json 结构性扩展（binding_history）；查询需扫 sessions（量大时后续加索引，本次不做）。

## References

- ADR-015（CLI 依赖 SDK）、ADR-037（append-only）、I-31 / I-32