---
name: agent-trace-governance
description: Govern agent work traces in this repo — decide what agent output to persist, distil agent work into ADR/issues/MR/handoff/CHANGELOG, and check trace consistency. Use when finishing a task, recording a decision, or auditing what agent work is versioned.
whenToUse: A task is ending, a decision or review must be recorded durably, or the user asks what agent traces should be kept.
---

# Agent Trace Governance

Rule of this repository: agent-authored work is part of the project record.
Keep the traces that explain **what was decided and why**; do not keep raw output.

## Retention levels

| Level | Content | Where |
|---|---|---|
| L1 — versioned | ADR, issues/I-NN-slug.md, merge-requests/I-NN.md (review rows, head, merged_as), docs/agents/human-review-tracker.md, CHANGELOG.md, handoff/decision docs, commit messages with issue refs | tracked in git |
| L2 — host only | sessions/ (this harness's product runtime logs), .scratch/ working notes, .soft-serve/data/log/push-audit.log, raw tool output | never commit; runtime data for debug/replay/metrics, rotated — NOT distilled into ADR/issue |
| L3 — forbidden | secrets/tokens, .soft-serve keys and data, large binaries, node_modules, build output | blocked by ignore rules and secret scan |

sessions/ is **not** the coding agent's conversation trace. It is this harness's own
SessionStore output (./sessions/<uuid>/events.jsonl + meta.json + audit.jsonl and
sessions/audit_log.jsonl), written by harness/session_store while the product runs.
The coding agent's own session store is ~/.dsh/sessions (DSH_HOME), outside this repo.
L1 keeps only distilled engineering artifacts; runtime logs are never mined into ADRs.

## When to use

- A decision was made and future sessions need the reasoning (ADR).
- Work is scoped, started, reviewed, or closed (issue and MR files).
- A session ends and another agent will continue (handoff).
- A release is cut (CHANGELOG + tag).
- Someone asks "what did the agent do / how do we know".

Do not use this to archive raw transcripts or to rewrite history.

## Distillation workflow

1. Decision required? Write docs/adr/<type>/NNN-slug.md; reference the issue id.
   Types live in docs/adr/ (app, architecture, communication, engineering,
   methodology, orchestration, persistence, sandbox, security, session).
2. Scope or plan? Copy issues/TEMPLATE.md to issues/I-NN-slug.md; fill frontmatter
   and acceptance criteria (must be True/False verifiable).
3. Work ready? Branch mr/I-NN-slug and create merge-requests/I-NN.md from TEMPLATE.
4. Review? Append a row to the MR review table; set status approved or
   changes-requested; commit to the MR branch per docs/sop.
5. Session ending? Write a handoff to docs/agents/handoffs/YYYY-MM-DD-slug.md.
   Handoffs under .scratch/ are L2 and are lost on a fresh clone - move them.
6. Release? Add a CHANGELOG entry that cites the issue and MR id.
7. Periodic? Reduce push-audit.log to a monthly note (release notes or an ADR)
   rather than committing the raw log.

## Trace schema (validated by repo-hygiene-audit)

Issue frontmatter: id, title, labels, author, created, status.
Allowed issue status: open, in-review, closed, wontfix.
MR frontmatter: id, issue, branch, base, author, opened, status, head, merged_as.
Allowed MR status: open, approved, changes-requested, rejected, merged.
Consistency rules:

- file name starts with the same id as frontmatter id
- MR branch is mr/<id>-<slug>
- head is the short SHA of the code commit; merged_as is the merge commit on main
- labels come from docs/agents/triage-labels.md (six canonical labels)

## Do not

- Commit secrets, tokens, absolute host paths, node_modules, sessions/, or raw
  tool output.
- Do not mine sessions/ (product runtime data) for governance artifacts; if a run
  matters, write a diagnosis/ADR from evidence and do not commit the logs.
- Strip agent provenance (co-author trailers, run ids, decision context):
  provenance is part of the record. Do not adopt skills that delete it.
- Write durable traces into .scratch/ (ignored) or .github/ (not used here).
- Rewrite git history to erase agent traces; supersede them with a new ADR.

## Check

    python3 .dsh/skills/repo-hygiene-audit/scripts/repo_audit.py --json
    git ls-files AGENTS.md issues merge-requests docs/adr docs/agents

Attribution: design adapted from kinoward/dayu-harness-skill (MIT, © kinoward)
and this repository's ADR + file-tracker conventions. See
.dsh/skills/_attribution/NOTICE.md.
