---
name: repo-hygiene-audit
description: Audit repository hygiene and agent-trace artifacts offline — governance files, ignore rules, secrets, large files, stale branches, issue/MR schema and docs drift. Use for periodic hygiene checks, pre-release review, or when asked whether the repo is healthy.
whenToUse: A periodic hygiene check is due, a release is being prepared, or the user asks what is missing or inconsistent in the repo.
---

# Repo Hygiene Audit

Read-only audit. Report findings; never delete or rewrite anything without an
explicit opt-in and a permanent-loss check.

## Run

    python3 .dsh/skills/repo-hygiene-audit/scripts/repo_audit.py
    python3 .dsh/skills/repo-hygiene-audit/scripts/repo_audit.py --json

Exit code 0 means no hard failures (secrets or broken tracker records);
warnings still need judgement.

## What it checks

- Governance files: LICENSE, CONTRIBUTING.md, SECURITY.md, CODEOWNERS,
  CHANGELOG.md, AGENTS.md — present and tracked.
- Ignore rules: sessions/, .scratch/, .soft-serve/ ignored; AGENTS.md not ignored.
- Secrets: filenames and high-signal patterns in tracked files (full scan is
  secret-audit's job).
- Large files: tracked files over 2 MB.
- Tracker schema: issues/I-*.md and merge-requests/I-*.md frontmatter, ids,
  status enums, head and merged_as consistency.
- Docs drift: docs/agents/issue-tracker.md pointing at .scratch, and the ADR
  count claimed in docs/agents/domain.md.
- Branches: merged local branches that can be cleaned up (report only).
- Runtime volume: harness sessions/ (product runtime data, not coding-agent traces)
  and push-audit log size.

## Triage order

1. Secrets or credentials: rotate first, then plan removal (see secret-audit).
2. Broken tracker records: fix the record, not the audit.
3. Missing governance files: create from governance-files templates.
4. Ignore-rule gaps: fix .gitignore; verify with git check-ignore -v.
5. Large files or stale branches: propose, confirm, then act.
6. Docs drift: correct the doc to match the repo.

## Manual checks that complement the script

    git status --short
    git ls-files -s | head -50
    git ls-files -z | xargs -0 du -k | sort -n | tail -20
    git branch --merged main
    git worktree list
    git log --oneline --since=30.days | wc -l

## Safety

- Read-only by default. Deleting branches, worktrees, stashes or untracked
  files is never automatic: explicit opt-in plus a permanent-loss check.
- Do not run destructive maintenance (gc, prune, stash drop) without the user
  saying so; prefer incremental maintenance over gc --aggressive.

Attribution: adapted from rjwalters/repo (MIT, © rjwalters) audit/branches/
gitignore/links/orphans and Joopinhontas/claude-repo-audit (MIT). See
.dsh/skills/_attribution/NOTICE.md.
