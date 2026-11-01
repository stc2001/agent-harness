---
name: git-commit-workflow
description: Commit in this repository with conventional messages, issue/MR linkage, author identity and verification before commit. Use when staging or committing changes, or when a commit is rejected by the server policy.
whenToUse: The user says commit/save/stage, or a direct push to main was rejected.
---

# Git Commit Workflow

A commit is a promise: what is in it works, and it links to why it exists.

## When to use

- The user says commit, save, stage, or "make a commit".
- You finished a multi-file change and want to record it on the MR branch.
- A push was rejected by pre-receive and you need to see why.

## Preconditions

- An issue exists: issues/I-NN-slug.md (create it first; tracker-only commits
  may go directly to main, code may not).
- You are on a branch named mr/I-NN-slug, not on main (main only accepts merge
  commits and tracker-only commits).
- The server is running: ./.soft-serve/start.sh; verify with
  git ls-remote origin refs/heads/main.

## Steps

1. Inspect: git status --short; git diff; git diff --cached.
2. Stage explicitly, never "git add .":
       git add path/to/file1 path/to/file2
   Keep generated files, .scratch/, sessions/, .soft-serve/ out of the commit.
3. Hygiene gate before committing:
   - no secrets or tokens (run secret-audit when unsure)
   - no file over 2 MB, no node_modules, no build output
   - related changes only; split unrelated work
   - tests the change touches have been run (make test or the focused pytest node)
4. Identify the issue the change serves. Put it in the subject:
       <type>(<scope>): I-NN <short imperative summary>
   Types: feat, fix, perf, refactor, docs, test, ci, build, chore.
   Scope: module or area, e.g. governance, git-plugin, harness, docs.
   Examples used in this repo:
       docs(issues): I-73 发起 — agent 指令 AGENTS.md 入库
       chore(governance): I-73 版本化 AGENTS.md
       merge: I-73 agent 指令 AGENTS.md 入库
5. Optional provenance trailers (keep them, do not strip):
       Co-authored-by: <agent or pair> <email>
       Refs: I-NN
6. Commit on the MR branch and push the branch:
       git push -u origin mr/I-NN-slug
7. Tracker-only change (only issues/** or merge-requests/**)? Then a direct
   push to main is allowed:
       git push origin main

## Never

- Never push code or docs straight to main; pre-receive rejects it.
- Never force-push main; published history is immutable.
- Never amend or rewrite commits that were already reviewed on an MR branch.
- Never hide agent involvement: provenance trailers are evidence, not noise.

## If pre-receive rejects

Read the message: delete/force on main, non-merge commit on main, or non-mr
branch name under strict mode. Move the work to mr/I-NN-slug, or use a merge
commit from an approved MR.

Attribution: adapted from laurigates/claude-plugins git-commit and
git-commit-workflow (MIT, © Lauri Gates) plus docs/sop/soft-serve-issue-mr-workflow.md.
See .dsh/skills/_attribution/NOTICE.md.
