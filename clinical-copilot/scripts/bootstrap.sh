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

echo "[bootstrap] Step 4a: OpenEMR docker compose (PHP stack) up (if available)"
if command -v docker >/dev/null 2>&1; then
  cd "$REPO_ROOT/docker/development-easy"
  docker compose up --detach --wait
  cd "$CC"
fi

echo "[bootstrap] Step 4b: Clinical Co-Pilot sidecar stack (postgres + sidecar + bff)"
# This stack lives in clinical-copilot/docker-compose.yml and is the
# production-shape stack used on Hetzner. Without this step a fresh host
# has the OpenEMR stack running but no sidecar / no postgres / no corpus,
# and every W2 chat request returns "no claims surfaced".
if command -v docker >/dev/null 2>&1; then
  cd "$CC"
  docker compose up --detach --wait postgres sidecar bff || \
    docker compose up --detach postgres sidecar bff
fi

echo "[bootstrap] Step 5: alembic migrations + guideline corpus load"
# Run BOTH inside the sidecar container so the env vars (COPILOT_DATABASE_URL,
# COPILOT_OPENAI_API_KEY) are inherited from the container's env_file. The
# old version of this step ran `alembic` from the host shell and silently
# no-op'd whenever COPILOT_DATABASE_URL wasn't exported on the host —
# which on Hetzner it never was, leaving the corpus empty for days.
if command -v docker >/dev/null 2>&1 && docker compose ps sidecar 2>/dev/null | grep -q sidecar; then
  cd "$CC"
  docker compose exec -T sidecar alembic upgrade head
  # Build corpus with the same embedder the retriever uses at query time.
  # COPILOT_USE_OPENAI_EMBEDDINGS=true forces OpenAI embeddings even if the
  # container's .env left the flag unset — misalignment between corpus
  # embeddings and query embeddings is the #1 silent RAG failure mode.
  docker compose exec -T -e COPILOT_USE_OPENAI_EMBEDDINGS=true sidecar \
    python -m sidecar.rag.build_corpus
  # Verify the corpus is non-empty before declaring success.
  docker compose exec -T sidecar python -c "
import os
from sqlalchemy import create_engine, text
e = create_engine(os.environ['COPILOT_DATABASE_URL'])
with e.connect() as c:
    n = c.execute(text('select count(*) from guideline_chunks')).scalar()
    print(f'guideline_chunks rows: {n}')
    if n < 20:
        raise SystemExit(f'corpus load failed: expected >=20 rows, got {n}')
"
elif [ -n "${COPILOT_DATABASE_URL:-}" ] || [ -n "${DATABASE_URL:-}" ]; then
  # Host-only path (no docker stack). Venv from step 2 is active.
  alembic upgrade head
  COPILOT_USE_OPENAI_EMBEDDINGS=true python -m sidecar.rag.build_corpus
else
  echo "[bootstrap] Step 5 SKIPPED: no docker sidecar container and no COPILOT_DATABASE_URL." >&2
  echo "[bootstrap]   This is the failure mode that left Hetzner with an empty corpus." >&2
  echo "[bootstrap]   Set COPILOT_DATABASE_URL and re-run, or start the docker stack first." >&2
fi

echo "[bootstrap] Step 6: install pre-push hook"
bash scripts/install_hooks.sh || true

echo "[bootstrap] done. Run 'pytest tests/sidecar/w2 evals/golden_w2/test_meta.py -q' to verify."
