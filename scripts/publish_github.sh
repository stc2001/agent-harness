#!/usr/bin/env bash
# Publish the current HEAD tree to GitHub main as a FAST-FORWARD append.
#
# Model:
#   - GitHub main starts from one root "release: latest snapshot" commit.
#   - Every later publish creates ONE commit whose parent is the current GitHub
#     main tip and whose tree is the local HEAD tree, then pushes normally
#     (no force, no deleting the remote branch).
#   - Local soft-serve history (700+ commits, old key) is never pushed.
#
# Usage: scripts/publish_github.sh [remote]   (default: github)
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

REMOTE=github
if [ "$#" -gt 0 ]; then
  REMOTE="$1"
fi
BRANCH=main

GIT_SSH_CMD="ssh -F /dev/null -i $HOME/.ssh/github_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$HOME/.ssh/known_hosts"

echo "== fetch $REMOTE/$BRANCH"
git -c core.sshCommand="$GIT_SSH_CMD" fetch -q "$REMOTE" "$BRANCH"
REMOTE_SHA=$(git rev-parse FETCH_HEAD)
echo "remote tip: $REMOTE_SHA"

TREE=$(git rev-parse 'HEAD^{tree}')
REMOTE_TREE=$(git rev-parse "$REMOTE_SHA^{tree}")
if [ "$TREE" = "$REMOTE_TREE" ]; then
  echo "no content change vs remote; nothing to publish"
  exit 0
fi

LOCAL_SHA=$(git rev-parse --short HEAD)
MSG="release: sync latest snapshot ($(date +%F), local $LOCAL_SHA)"

NEW=$(git commit-tree "$TREE" -p "$REMOTE_SHA" -m "$MSG")
if ! git merge-base --is-ancestor "$REMOTE_SHA" "$NEW"; then
  echo "refusing: generated commit is not a fast-forward of remote" >&2
  exit 1
fi
echo "new commit: $NEW (parent $REMOTE_SHA)"

echo "== push (fast-forward, no force)"
git -c core.sshCommand="$GIT_SSH_CMD" push "$REMOTE" "$NEW:refs/heads/$BRANCH"

echo "== verify"
git -c core.sshCommand="$GIT_SSH_CMD" ls-remote "$REMOTE" "refs/heads/$BRANCH"
