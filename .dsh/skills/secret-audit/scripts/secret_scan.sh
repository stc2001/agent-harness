#!/usr/bin/env bash
# Offline secret scan for tracked files (default) or git history (--history).
set -u
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git repo" >&2; exit 2; }
cd "$ROOT" || exit 2

MODE=tree
if [ "$#" -gt 0 ] && [ "$1" = "--history" ]; then
  MODE=history
fi

PATTERNS='-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----
AKIA[0-9A-Z]{16}
gh[pousr]_[A-Za-z0-9]{30,}
xox[baprs]-[A-Za-z0-9-]{10,}
sk-[A-Za-z0-9]{20,}
AIza[0-9A-Za-z_-]{35}'

echo "mode=$MODE"

echo "--- suspicious file names ---"
git ls-files | grep -nE '(^|/)(\.env|session_key|id_rsa|id_ed25519)(\.|$)|\.(pem|key|p12)$' || true

echo "--- pattern hits (rotation required, never paste full values) ---"
if [ "$MODE" = "tree" ]; then
  HITS=$(git grep -nIE -f <(printf '%s\n' "$PATTERNS") -- . 2>/dev/null || true)
else
  HITS=$(git log -p --all --no-color 2>/dev/null | grep -nIE -f <(printf '%s\n' "$PATTERNS") || true)
fi

if [ -n "$HITS" ]; then
  printf '%s\n' "$HITS" | head -80
  COUNT=$(printf '%s\n' "$HITS" | wc -l)
  echo "hits=$COUNT"
  echo "Next: rotate/revoke the credential, then plan removal; do not rewrite history as a first response."
  exit 1
fi

echo "no pattern hits"
exit 0
