# Docs

harness 内核文档地图。app 层（pi）文档在独立仓库 ../pi-app。

| 路径 | 内容 |
|---|---|
| adr/ | 44 篇内核 ADR（architecture/communication/engineering/methodology/persistence/sandbox/security/session） |
| agents/ | agent 协作约定：domain / issue-tracker / triage-labels / human-review-tracker |
| sop/ | soft-serve issue/MR 工作流、并发排查、测试策略 |
| plan(s)/ | 设计与重构计划（含 repo-split-plan） |
| archive/ | 历史归档：TODO 旧文档、已解决的问题清单、被取代的设计计划 |
| evidence/ | ADR/issue 引用的实测证据 |
| reference_research_paper/ | 外部研究资料 |
| harness_doc/ | 本机测试 key（被 .gitignore 忽略；不随仓库发布） |

边界：

- 内核决策写 docs/adr/；app 层决策在 pi 仓库的 docs/adr/。
- SDK 契约、事件词汇、沙箱/会话语义以本仓库为准；pi 通过 pinned 版本引用。
- 历史已解决清单见 docs/archive/solved-issues-through-I-43.md。
