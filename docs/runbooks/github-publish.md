# GitHub Publish — normal fast-forward append

## Rules

1. GitHub agent-harness main starts from ONE root snapshot commit
   (current base: 1a16f6b, 2026-11-01).
2. Every later publish appends ONE sync commit on top of the current remote main:
   - parent = current remote main tip
   - tree = local HEAD tree
   - normal fast-forward push; never force, never delete the remote branch.
3. Local soft-serve history (739+ commits, old key) is never pushed to GitHub.

## Command

    cd /home/gexingdeyun/OH_workspace
    scripts/publish_github.sh

The script:

- fetches GitHub main and reads the remote tip;
- exits 0 when the local tree is identical to the remote tree;
- creates a commit with parent = remote tip and tree = local HEAD tree;
- pushes fast-forward and verifies with ls-remote.

SSH uses ssh.github.com:443 and ~/.ssh/github_ed25519
(the accelerator hijacks github.com:22, so this bypasses it).

## Local guard

.githooks/pre-push (local only, not tracked) for non-local remotes:

- allows root snapshot commits;
- allows an append chain rooted at a release snapshot (max 200 commits);
- rejects local full history (root is not a release snapshot);
- rejects tip trees containing sessions/.soft-serve/.venv/.scratch/diary/
  node_modules or the legacy key file.

## Failure handling

- If the remote tip moved, re-run the script; it fetches the new tip and parents
  the new commit on it.
- Do not use --force. If the cloud really must be reset, confirm separately.
