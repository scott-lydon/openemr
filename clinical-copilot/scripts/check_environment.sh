#!/usr/bin/env bash
# scripts/check_environment.sh
# Verifies every external dependency the Week 2 build expects.
# Each check prints "<NAME>: OK" or "<NAME>: MISSING (<hint>)" then continues.
# Exits non-zero only if a REQUIRED dependency is missing.
#
# Usage: bash scripts/check_environment.sh
set -uo pipefail

REQUIRED_FAIL=0
OPTIONAL_WARN=0

ok()   { printf "%-40s OK\n" "$1"; }
miss() { printf "%-40s MISSING (%s)\n" "$1" "$2"; }
warn() { printf "%-40s WARN (%s)\n" "$1" "$2"; OPTIONAL_WARN=$((OPTIONAL_WARN+1)); }
fail() { miss "$1" "$2"; REQUIRED_FAIL=$((REQUIRED_FAIL+1)); }

heading() { echo; echo "== $1 =="; }

# ----------------------------------------------------------------------------
heading "Python interpreter"

if command -v python3 >/dev/null 2>&1; then
  PYV="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
  case "$PYV" in
    3.11|3.12|3.13) ok "python3 ($PYV)" ;;
    *) fail "python3" "found $PYV; need 3.11, 3.12, or 3.13" ;;
  esac
else
  fail "python3" "not on PATH; install via Homebrew or pyenv"
fi

# ----------------------------------------------------------------------------
heading "System libraries"

# libmagic underlies python-magic which the upload handler uses for MIME sniffing.
if [ "$(uname -s)" = "Darwin" ]; then
  brew --prefix libmagic >/dev/null 2>&1 && ok "libmagic" || \
    fail "libmagic" "brew install libmagic"
else
  ldconfig -p 2>/dev/null | grep -q libmagic && ok "libmagic" || \
    fail "libmagic" "apt-get install -y libmagic1"
fi

# ImageMagick used to degrade fixture scans into mid/poor tiers.
if command -v magick >/dev/null 2>&1; then
  ok "imagemagick (magick)"
elif command -v convert >/dev/null 2>&1; then
  warn "imagemagick" "found legacy 'convert'; v7 'magick' preferred"
else
  fail "imagemagick" "brew install imagemagick (macOS) or apt-get install imagemagick (linux)"
fi

# ----------------------------------------------------------------------------
heading "ClamAV antivirus daemon (Layer 1 of sanitization stack)"

if command -v clamdscan >/dev/null 2>&1; then
  if echo "PING" | (timeout 2 clamdscan --no-summary --stdout - 2>&1 || true) | grep -qi "stream:"; then
    ok "clamav daemon"
  else
    warn "clamav daemon" "binary present but daemon may be down: 'brew services start clamav' or 'systemctl start clamav-daemon'"
  fi
else
  fail "clamav" "brew install clamav (macOS) or apt-get install clamav clamav-daemon (linux)"
fi

# ----------------------------------------------------------------------------
heading "PostgreSQL with pgvector"

if command -v psql >/dev/null 2>&1; then
  ok "psql client"
else
  warn "psql client" "not on PATH; the worker will still run via psycopg, but admin commands need psql"
fi

# Skip live db check when DATABASE_URL is absent.
if [ -n "${DATABASE_URL:-}" ]; then
  if psql "$DATABASE_URL" -tAc "SELECT extname FROM pg_extension WHERE extname='vector';" 2>/dev/null | grep -q vector; then
    ok "pgvector extension"
  else
    fail "pgvector extension" "CREATE EXTENSION vector; in your sidecar database"
  fi
else
  warn "pgvector extension" "DATABASE_URL not set; cannot verify"
fi

# ----------------------------------------------------------------------------
heading "Python packages (after pip install -e .[w2,dev,openai,langgraph,postgres,observability,phi])"

check_py() {
  local name="$1" import_as="$2" hint="$3"
  python3 -c "import $import_as" 2>/dev/null && ok "$name" || fail "$name" "$hint"
}

check_py "fastapi"             "fastapi"             "pip install -e ."
check_py "pydantic"            "pydantic"            "pip install -e ."
check_py "structlog"           "structlog"           "pip install -e ."
check_py "pymupdf (fitz)"      "fitz"                "pip install -e .[w2_ingest]"
check_py "Pillow (PIL)"        "PIL"                 "pip install -e .[w2_ingest]"
check_py "python-magic"        "magic"               "pip install -e .[w2_ingest]"
check_py "clamd"               "clamd"               "pip install -e .[w2_ingest]"
check_py "pypdf"               "pypdf"               "pip install -e .[w2_ingest]"
check_py "cohere"              "cohere"              "pip install -e .[w2_rag]"
check_py "rank-bm25"           "rank_bm25"           "pip install -e .[w2_rag]"
check_py "tiktoken"            "tiktoken"            "pip install -e .[w2_rag]"
check_py "llm-guard"           "llm_guard"           "pip install -e .[w2_sanitize]"
check_py "rebuff"              "rebuff"              "pip install -e .[w2_sanitize]"
check_py "presidio-analyzer"   "presidio_analyzer"   "pip install -e .[phi]"
check_py "anthropic"           "anthropic"           "pip install -e .[w2_judges]"
check_py "matplotlib"          "matplotlib"          "pip install -e .[w2_widgets]"
check_py "rapidfuzz"           "rapidfuzz"           "pip install -e .[w2_widgets]"
check_py "openai"              "openai"              "pip install -e .[openai]"
check_py "langgraph"           "langgraph"           "pip install -e .[langgraph]"
check_py "psycopg"             "psycopg"             "pip install -e .[postgres]"
check_py "pgvector (python)"   "pgvector"            "pip install -e .[postgres]"
check_py "alembic"             "alembic"             "pip install -e .[postgres]"
check_py "sqlalchemy"          "sqlalchemy"          "pip install -e .[postgres]"
check_py "opentelemetry"       "opentelemetry"       "pip install -e .[observability]"
check_py "hypothesis"          "hypothesis"          "pip install -e .[w2_test]"
check_py "bandit"              "bandit"              "pip install -e .[w2_test]"

# ----------------------------------------------------------------------------
heading "Phase 2 schema migrations"

DB_URL_FOR_CHECK="${COPILOT_DATABASE_URL:-${DATABASE_URL:-}}"
if [ -n "$DB_URL_FOR_CHECK" ] && command -v psql >/dev/null 2>&1; then
  if psql "$DB_URL_FOR_CHECK" -tAc "SELECT to_regclass('public.agent_jobs');" 2>/dev/null | grep -q agent_jobs; then
    ok "agent_jobs table"
  else
    fail "agent_jobs table" "cd clinical-copilot && alembic upgrade head (with COPILOT_DATABASE_URL set)"
  fi
  if psql "$DB_URL_FOR_CHECK" -tAc "SELECT to_regclass('public.citations');" 2>/dev/null | grep -q citations; then
    ok "citations table"
  else
    fail "citations table" "cd clinical-copilot && alembic upgrade head (migration 0002 lands citations)"
  fi
else
  warn "agent_jobs/citations tables" "DATABASE_URL/COPILOT_DATABASE_URL not set or psql missing; cannot verify"
fi

# ----------------------------------------------------------------------------
heading "spaCy NLP model for Presidio (en_core_web_lg)"

if python3 -c "import spacy; spacy.load('en_core_web_lg')" 2>/dev/null; then
  ok "en_core_web_lg"
else
  fail "en_core_web_lg" "python -m spacy download en_core_web_lg"
fi

# ----------------------------------------------------------------------------
heading "Required environment variables"

require_env() {
  local name="$1" hint="$2"
  if [ -z "${!name:-}" ]; then
    warn "\$$name" "$hint"
  else
    ok "\$$name"
  fi
}

require_env OPENAI_API_KEY        "Vision Language Model (VLM) extraction requires OpenAI BAA endpoint key"
require_env COHERE_API_KEY        "Cohere Rerank requires API key (will fall back if absent)"
require_env ANTHROPIC_API_KEY     "Cross-vendor eval judge (set when running evals)"
require_env DATABASE_URL          "Postgres connection (sidecar + queue + pgvector)"

# ----------------------------------------------------------------------------
heading "Summary"

if [ "$REQUIRED_FAIL" -gt 0 ]; then
  echo
  echo "FAIL: $REQUIRED_FAIL required dependency check(s) failed."
  echo "Fix the items marked MISSING above, then rerun this script."
  exit 1
fi

if [ "$OPTIONAL_WARN" -gt 0 ]; then
  echo
  echo "OK with $OPTIONAL_WARN warning(s); see WARN lines above."
  exit 0
fi

echo
echo "OK: every check passed."
exit 0
