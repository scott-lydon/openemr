#!/usr/bin/env bash
# bootstrap.sh — bring a fresh host up to a working clinical-copilot.
#
# Used in the disaster-recovery drill (W2_VERIFICATION_CHECKLIST.md
# section 12.4). Target: complete in under 60 minutes on a fresh
# Hetzner instance. Idempotent — safe to re-run.
#
# Tagging: this script runs in [SSH terminal: root@<hetzner>] or in a
# fresh container shell.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CC="$REPO_ROOT/clinical-copilot"

echo "[bootstrap] Step 1: system packages"
if [ "$(uname)" = "Linux" ]; then
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    libmagic1 imagemagick \
    clamav clamav-daemon \
    postgresql-client \
    curl ca-certificates
elif [ "$(uname)" = "Darwin" ]; then
  brew bundle --file=- <<EOF
brew "python@3.12"
brew "libmagic"
brew "imagemagick"
brew "clamav"
brew "postgresql@16"
EOF
fi

echo "[bootstrap] Step 2: python venv + deps"
cd "$CC"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[w2,dev,openai,langgraph,postgres,observability,phi]"
python -m spacy download en_core_web_lg

echo "[bootstrap] Step 3: environment check"
bash scripts/check_environment.sh || true

echo "[bootstrap] Step 4: docker compose up (if available)"
if command -v docker >/dev/null 2>&1; then
  cd "$REPO_ROOT/docker/development-easy"
  docker compose up --detach --wait
  cd "$CC"
fi

echo "[bootstrap] Step 5: alembic migrations"
if [ -n "${COPILOT_DATABASE_URL:-}" ] || [ -n "${DATABASE_URL:-}" ]; then
  alembic upgrade head
fi

echo "[bootstrap] Step 6: install pre-push hook"
bash scripts/install_hooks.sh || true

echo "[bootstrap] done. Run 'pytest tests/sidecar/w2 evals/golden_w2/test_meta.py -q' to verify."
