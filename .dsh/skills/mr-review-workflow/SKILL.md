---
name: mr-review-workflow
description: Review and merge a change recorded as merge-requests/I-NN.md — verify head SHA, acceptance criteria, tests and review rows; approve, merge with --no-ff, update the tracker. Use when reviewing an MR, before merging, or when asked to check another agent's work.
whenToUse: An MR branch is ready, the user asks for a review or merge, or a PR-like change must be validated.
---

# MR Review Workflow

An MR is a branch plus merge-requests/I-NN.md. Review is evidence-based:
read the diff, run the checks, then decide.

## When to use

- A branch mr/I-NN-slug exists and needs approval.
- Another agent/person asks for review.
- You are about to merge anything into main.

## Review checklist

1. Read the issue (issues/I-NN-slug.md) and the MR record
   (merge-requests/I-NN.md). Confirm the acceptance criteria are testable.
2. Fetch and inspect the branch:
       git fetch origin mr/I-NN-slug
       git log --oneline main..origin/mr/I-NN-slug
       git diff --stat main...origin/mr/I-NN-slug
   Record the reviewed head SHA.
3. Read the full diff. Check for: unrelated changes, debug leftovers,
   secrets, large files, generated artifacts, absolute host paths.
4. Run the checks the MR claims: make test, or the focused pytest nodes.
   Do not accept "tests pass" without seeing the output.
5. Verify tracker consistency: MR head matches the code commit, branch name is
   mr/I-NN-slug, issue and MR ids match, labels are canonical.
6. Decide: approved, changes-requested, or rejected. Write the decision as a
   row in the MR review table and set status; commit to the MR branch.

## Merge (only when approved)

    git switch main
    git fetch origin main
    git merge --no-ff mr/I-NN-slug
    git push origin main

Then update the record on main (tracker-only commits are allowed):

    status: merged, head: <code commit>, merged_as: <merge commit sha>
    issue status: closed
    commit messages: docs(mr): I-NN 记录合并结果
                     docs(issues): I-NN 关闭（MR 已合并 <sha>）

## Do not

- Do not rebase and force-push the MR branch; it erases review commits.
- Do not merge with a red or unverified check, or with unresolved
  changes-requested.
- Do not approve your own unreviewed work without recording the evidence; if
  you self-review, say so in the review table.
- Do not push code directly to main; only the merge commit lands there.

## Output

Short review verdict: decision, evidence (commands + results), blockers,
next action. Point to file paths instead of pasting diffs.

Attribution: adapted from laurigates/claude-plugins git-pr, git-pr-feedback and
git-triage (MIT, © Lauri Gates) and this repo's
docs/sop/soft-serve-issue-mr-workflow.md. See .dsh/skills/_attribution/NOTICE.md.
