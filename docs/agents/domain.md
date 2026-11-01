# Domain Docs — Single-Context Layout

This project uses a **single-context** layout:
- One CONTEXT.md at the repo root (project-wide domain glossary)
- One docs/adr/ directory for Architecture Decision Records

## Consumer rules for agents

When a skill asks you to "use the project's domain glossary":
1. Read CONTEXT.md from the repo root
2. Use the vocabulary defined there throughout your response
3. If CONTEXT.md doesn't exist yet, create it by synthesizing the domain language
   from existing docs in docs/harness_doc/

When a skill asks you to "respect ADRs in the area":
1. Read docs/adr/<type>/ for relevant decisions. Types: architecture,
   communication, engineering, methodology, persistence, sandbox, security,
   session (docs/adr/orchestration/ exists but is empty; app-layer ADRs moved
   to the pi repository)
2. Reference ADR numbers in your reasoning
3. If no ADR exists for a decision you're about to make, propose creating one

## Current state

- CONTEXT.md: created from the original design documents (removed in commit 1809749;
  the glossary is now the source of truth)
- docs/adr/: 已创建 44 个 ADR，分布在 8 个非空类型目录 —— architecture 34、
  communication 1、engineering 1、methodology 3、persistence 2、sandbox 1、
  security 1、session 1；编号范围 001–055，缺号 007/008/013/017/025–028/031/032；
  orchestration/ 为空目录。app 层 ADR（001/002）已移至 pi 仓库。
