# ADR-005：JSONL + meta.json vs SQLite

**日期**：2026-05-12  
**状态**：已采纳  

## 上下文

SessionStore 需要持久化三种数据：事件日志、会话元数据、检查点快照。候选：
- **SQLite**：单文件关系型数据库，支持索引查询
- **JSONL + JSON**：纯文本追加写日志 + JSON 元数据文件

## 决策

**选择 JSONL（事件日志）+ meta.json（元数据）。**

## 理由

1. **追加写友好**。事件日志只 append 不修改，JSONL 每行一个 JSON 对象，追加一行即完成写入。SQLite 的 `INSERT` 需要事务开销，对于追加为主的场景是过度设计。
2. **人类可读**。JSONL 文件可以直接 `cat`、`grep`、`tail -f`，方便调试和人工审计。
3. **rewind/fork 简单**。rewind 只需在内存中重建逻辑视图，fork 只需逐行复制事件文件。SQLite 的 rewind 需要标记删除或写 undo 日志。
4. **零配置**。不需要建表、迁移、索引优化。SessionStore 的 per-session 文件锁已实现线程安全。

## 后果

- 放弃了 SQLite 的索引查询能力（对于 MVP 不需要）
- 元数据从 `metadata.db` 变为 `meta.json`，破坏了按事件 ID 断点续读的接口（`after_event_id` → `from_idx`，见审计报告 D 项）
- 如果未来需要跨 session 查询/聚合，可以考虑引入 SQLite 作为索引层
