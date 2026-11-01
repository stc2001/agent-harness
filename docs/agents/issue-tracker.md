# Issue Tracker — File-based Markdown

Issues live under issues/ as one markdown file per ID; merge requests live under
merge-requests/. soft-serve only serves git (no issue/PR feature); the server
pre-receive hook enforces the workflow.

## Convention

- issues/<ID>-<slug>.md — one issue per file. ID is I-NN; next = current max + 1.
- Copy issues/TEMPLATE.md; keep frontmatter: id, title, labels, author, created, status.
- Issue status values: open, in-review, closed, wontfix.
- Labels: docs/agents/triage-labels.md only (needs-triage, needs-info,
  ready-for-agent, ready-for-human, wontfix). Do not invent labels.
- Branch: mr/<ID>-<slug> (one MR = one branch).
- MR record: merge-requests/<ID>.md with id, issue, branch, base, author, opened,
  status, head, merged_as. MR status: open, approved, changes-requested, rejected, merged.

## Push policy

- Tracker-only commits (issues/**, merge-requests/**) may push main directly.
- Code/doc changes go on mr/<ID>-<slug>; after review merge with --no-ff, then push main.
- Never rebase + force-push an MR branch: it erases review commits.

## Agent instructions

Creating an issue:

1. Find the next id: ls issues | grep -oE 'I-[0-9]+' | sort -t- -k2 -n | tail -1
2. cp issues/TEMPLATE.md issues/I-NN-slug.md; fill frontmatter and testable acceptance criteria.
3. git add issues/I-NN-slug.md; commit "docs(issues): I-NN 发起 — <title>"; push origin main.

Reading issues:

1. List issues/I-*.md; read frontmatter and body.
2. Ignore TEMPLATE.md, README.md, and the legacy backlog file.

## Legacy backlog

issues/python-harness-issues.md holds the pre-I-44 backlog (I-15…I-43) plus the
2026-10-19 leftover registration. It is being migrated into I-NN files; new work
must use I-NN files, not append to the legacy log.

See also: issues/README.md, docs/sop/soft-serve-issue-mr-workflow.md,
docs/agents/triage-labels.md.
