#!/usr/bin/env bash
# install_hooks.sh — install the clinical-copilot pre-push hook.
#
# Symlinks .git/hooks/pre-push to clinical-copilot/.git-hooks/pre-push.
# Re-running is idempotent (the symlink target is replaced).
#
# Usage:
#
#   bash clinical-copilot/scripts/install_hooks.sh
#
# Tagging: this script runs in the [Local Mac terminal].

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
CC_HOOKS="$REPO_ROOT/clinical-copilot/.git-hooks"
GIT_HOOKS_DIR="$REPO_ROOT/.git/hooks"

if [ ! -d "$CC_HOOKS" ]; then
  echo "ERROR: $CC_HOOKS does not exist." >&2
  exit 1
fi
if [ ! -d "$GIT_HOOKS_DIR" ]; then
  echo "ERROR: $GIT_HOOKS_DIR does not exist; is this a real git checkout?" >&2
  exit 1
fi

for hook in pre-push; do
  src="$CC_HOOKS/$hook"
  dst="$GIT_HOOKS_DIR/$hook"
  if [ ! -f "$src" ]; then
    echo "WARN: $src missing; skipping" >&2
    continue
  fi
  chmod +x "$src"
  ln -sf "$src" "$dst"
  echo "installed: $dst -> $src"
done

echo "done."
