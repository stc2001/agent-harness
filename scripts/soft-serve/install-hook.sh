#!/usr/bin/env bash
#
# Install the Soft Serve push-policy hook from this repo (source of truth) into
# the running server's global hooks directory.
#
#   scripts/soft-serve/pre-receive   ->  <SOFT_SERVE_DATA_PATH>/hooks/pre-receive
#
# Soft Serve runs every global hook on each push; a non-zero exit rejects the
# push. Re-run this after editing the hook in the repo.
#
# Usage:  ./scripts/soft-serve/install-hook.sh [--uninstall]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$REPO_ROOT/scripts/soft-serve/pre-receive"
DATA_PATH="${SOFT_SERVE_DATA_PATH:-$REPO_ROOT/.soft-serve/data}"
DST="$DATA_PATH/hooks/pre-receive"

if [ "${1:-}" = "--uninstall" ]; then
  if [ -e "$DST" ]; then
    mv "$DST" "$DST.disabled"
    echo "policy hook disabled -> $DST.disabled"
    echo "（重新启用：./scripts/soft-serve/install-hook.sh）"
  else
    echo "no hook installed at $DST"
  fi
  exit 0
fi

[ -f "$SRC" ] || { echo "missing hook source: $SRC" >&2; exit 1; }
[ -d "$DATA_PATH" ] || {
  echo "server data dir not found: $DATA_PATH" >&2
  echo "先启动服务端一次（./.soft-serve/start.sh）以生成数据目录。" >&2
  exit 1
}

install -m 755 "$SRC" "$DST"
# Drop a stale disabled copy so the hooks dir has one unambiguous state.
if [ -e "$DST.disabled" ]; then
  rm -f "$DST.disabled"
  echo "removed stale $DST.disabled"
fi
bash -n "$DST"
echo "installed policy hook -> $DST"
echo "验证：直接推 main 的代码提交应被拒绝，推 mr/* 分支应放行。"
echo "决策审计：tail $DATA_PATH/log/push-audit.log"
