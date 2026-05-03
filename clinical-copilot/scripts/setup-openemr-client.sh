#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-openemr-client.sh
#
# Register a confidential OpenEMR API client for the Clinical Co-Pilot
# sidecar's client_credentials grant, then write the generated id/secret
# into clinical-copilot/.env. Idempotent: safe to re-run.
#
# Usage:
#     bash clinical-copilot/scripts/setup-openemr-client.sh
#     bash clinical-copilot/scripts/setup-openemr-client.sh --site=default
#
# Strategy:
#   1. Detect the running OpenEMR docker container (development-easy
#      compose project).
#   2. Run `bin/console openemr-dev:register-api-test-client` inside that
#      container. The command generates a random client_id and
#      client_secret, inserts the row into oauth_clients, marks it
#      is_enabled=1, and prints both values in a Symfony table.
#   3. Parse client_id / client_secret out of the table.
#   4. Rewrite COPILOT_OPENEMR_CLIENT_ID and COPILOT_OPENEMR_CLIENT_SECRET
#      in clinical-copilot/.env (creating .env from .env.example if it does
#      not yet exist).
#   5. Restart the sidecar (or tell the user to) so the new secret is read.
#
# Safety:
#   - The script never echoes the secret to stdout once .env has been
#     written. The credentials live exactly two places: oauth_clients and
#     .env (gitignored).
#   - If docker is not available, the script prints click-by-click
#     instructions for the OpenEMR admin UI as a fallback.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COPILOT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$COPILOT_ROOT/.env"
ENV_EXAMPLE="$COPILOT_ROOT/.env.example"
SITE="default"

# ─── Argument parsing ─────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --site=*) SITE="${arg#--site=}" ;;
    -h|--help)
      grep -E '^# ' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      echo "Run with --help for usage." >&2
      exit 64
      ;;
  esac
done

echo "── Clinical Co-Pilot: OpenEMR API client setup ──"
echo "Project root  : $COPILOT_ROOT"
echo "Site          : $SITE"

# ─── 0. .env bootstrap ────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  if [ ! -f "$ENV_EXAMPLE" ]; then
    echo "ERROR: $ENV_EXAMPLE missing — cannot bootstrap $ENV_FILE." >&2
    exit 70
  fi
  echo "No .env found; copying from .env.example."
  cp "$ENV_EXAMPLE" "$ENV_FILE"
fi

# ─── 1. Detect docker + OpenEMR container ─────────────────────────────────
print_manual_instructions() {
  cat <<'EOF'

Could not register the client automatically. Do it through the OpenEMR
admin UI instead — it takes about a minute:

  1. Open http://localhost:8300/ and sign in as admin / pass.
  2. Top menu: Administration → System → API Clients.
  3. Click "Register New App".
  4. Fill in the form:
       App Name              : Clinical Co-Pilot Sidecar
       App Launch URL        : (blank — backend service)
       App Redirect URL      : http://localhost:8801/oauth/callback
       Client Type           : confidential
       SMART scopes (tick)   : system/Patient.read, system/Condition.read,
                               system/MedicationRequest.read,
                               system/AllergyIntolerance.read,
                               system/Observation.read,
                               system/Encounter.read,
                               system/Procedure.read,
                               system/DocumentReference.read
       Grant types           : client_credentials (must be ticked)
  5. Click "Submit".
  6. The next page shows Client ID and Client Secret ONCE. Copy both.
  7. Back in the API Clients list, find the new row, click "Enable", then
     "Edit" → ensure every system/*.read scope shows as Trusted.
  8. Edit clinical-copilot/.env and set:
       COPILOT_OPENEMR_CLIENT_ID=<client id from step 6>
       COPILOT_OPENEMR_CLIENT_SECRET=<client secret from step 6>
  9. Restart the sidecar (Ctrl-C the launch-sidecar.command terminal and
     re-run it, or `kill` whatever uvicorn process is on :8801 and
     re-launch).
EOF
}

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not on PATH." >&2
  print_manual_instructions
  exit 1
fi

CONTAINER="$(docker ps --filter 'name=development-easy.*openemr.*1$' --format '{{.Names}}' | head -1)"
if [ -z "$CONTAINER" ]; then
  CONTAINER="$(docker ps --format '{{.Names}}' | grep -E 'openemr.*1$' | grep -v 'mysql\|phpmyadmin\|redis\|couchdb' | head -1 || true)"
fi

if [ -z "$CONTAINER" ]; then
  echo "ERROR: no running OpenEMR container found." >&2
  echo "Start it with: cd docker/development-easy && docker compose up -d --wait" >&2
  print_manual_instructions
  exit 1
fi
echo "OpenEMR container: $CONTAINER"

# ─── 2. Run the existing OpenEMR Symfony command ──────────────────────────
# bin/console openemr-dev:register-api-test-client
#   --site            : OpenEMR site id (default = "default")
#   --redirect-uri    : OAuth2 redirect URI (used by the auth-code grant
#                       only; client_credentials ignores it)
#   --launch-uri      : SMART launch URL (also unused for backend services)
#
# The container's default WORKDIR is not the OpenEMR webroot, so we
# must run the command with bin/console resolved relative to the
# webroot mount. The development-easy compose mounts the source at
# /var/www/localhost/htdocs/openemr; we sniff for the bin/console
# under that path first and fall back to /openemr (the read-only
# alternate mount) if anything else changed.
OPENEMR_ROOT_GUESSES=(
  '/var/www/localhost/htdocs/openemr'
  '/openemr'
)
OPENEMR_ROOT=""
for guess in "${OPENEMR_ROOT_GUESSES[@]}"; do
  if docker exec "$CONTAINER" test -f "$guess/bin/console"; then
    OPENEMR_ROOT="$guess"
    break
  fi
done

if [ -z "$OPENEMR_ROOT" ]; then
  echo "ERROR: bin/console not found inside container under any of:" >&2
  printf '  %s\n' "${OPENEMR_ROOT_GUESSES[@]}" >&2
  echo "Run \`docker exec $CONTAINER find / -name console -type f 2>/dev/null\` to locate it, then edit OPENEMR_ROOT_GUESSES in this script." >&2
  print_manual_instructions
  exit 1
fi
echo "OpenEMR webroot in container: $OPENEMR_ROOT"
echo "Registering API client via bin/console openemr-dev:register-api-test-client …"

set +e
REG_OUTPUT="$(docker exec -u root -w "$OPENEMR_ROOT" "$CONTAINER" \
  php bin/console openemr-dev:register-api-test-client \
    --site="$SITE" \
    --redirect-uri='http://localhost:8801/oauth/callback' \
    --launch-uri='' \
    2>&1)"
REG_RC=$?
set -e

if [ "$REG_RC" -ne 0 ]; then
  echo "ERROR: openemr-dev:register-api-test-client exited $REG_RC." >&2
  echo "----- command output -----" >&2
  printf '%s\n' "$REG_OUTPUT" >&2
  echo "--------------------------" >&2
  print_manual_instructions
  exit "$REG_RC"
fi

# ─── 3. Parse credentials from Symfony table output ───────────────────────
# Symfony's table renderer here uses the "compact" style: cells are
# separated by 2+ spaces and rows are bracketed by lines of dashes.
# We isolate the data row by looking for a line whose first two
# whitespace-separated fields are base64url tokens of length >= 32
# (the Client ID is 43 chars from base64url(random_bytes(32)), the
# Client Secret is 86 chars from base64url(random_bytes(64)), per
# OpenEMR's ClientRepository::generateClientId/Secret).
#
# A pure-dash border line ALSO matches [A-Za-z0-9_-]{32,} because
# dashes are part of the character class, so we explicitly skip
# lines that are just whitespace + dashes.
DATA_LINE="$(printf '%s\n' "$REG_OUTPUT" | awk '
  /^[[:space:]]*-+[-[:space:]]*$/ { next }
  /^[[:space:]]+[A-Za-z0-9_-]{32,}[[:space:]]+[A-Za-z0-9_-]{32,}/ { print; exit }
')"

if [ -z "$DATA_LINE" ]; then
  echo "ERROR: could not parse credentials from command output." >&2
  echo "----- raw output -----" >&2
  printf '%s\n' "$REG_OUTPUT" >&2
  echo "----------------------" >&2
  exit 75
fi

# Split on 2+ whitespace characters. The leading whitespace at the
# start of the data line becomes an empty $1, so the Client ID is
# $2 and the Client Secret is $3.
CLIENT_ID="$(printf '%s' "$DATA_LINE"     | awk -F '[[:space:]][[:space:]]+' '{ print $2 }')"
CLIENT_SECRET="$(printf '%s' "$DATA_LINE" | awk -F '[[:space:]][[:space:]]+' '{ print $3 }')"

if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
  echo "ERROR: parsed empty client_id or client_secret." >&2
  echo "Parsed line: $DATA_LINE" >&2
  exit 75
fi

# ─── 4. Rewrite .env ──────────────────────────────────────────────────────
# Use a Python one-liner because in-place sed is non-portable across BSD
# (macOS) and GNU. Python is guaranteed to be present (the sidecar runs
# on Python).
PYTHON_BIN="$(command -v python3 || command -v python)"
if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: python3 not on PATH; cannot update .env safely." >&2
  echo "Add these two lines to $ENV_FILE manually:" >&2
  echo "  COPILOT_OPENEMR_CLIENT_ID=$CLIENT_ID" >&2
  echo "  COPILOT_OPENEMR_CLIENT_SECRET=<the secret printed above>" >&2
  exit 1
fi

CLIENT_ID="$CLIENT_ID" CLIENT_SECRET="$CLIENT_SECRET" ENV_FILE="$ENV_FILE" \
  "$PYTHON_BIN" - <<'PYEOF'
import os
import pathlib

env_path = pathlib.Path(os.environ["ENV_FILE"])
client_id = os.environ["CLIENT_ID"]
client_secret = os.environ["CLIENT_SECRET"]

assignments = {
    "COPILOT_OPENEMR_CLIENT_ID": client_id,
    "COPILOT_OPENEMR_CLIENT_SECRET": client_secret,
}

lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)

found = {key: False for key in assignments}
for i, line in enumerate(lines):
    stripped = line.lstrip()
    for key, value in assignments.items():
        # Match "KEY=" at start of a non-comment line. Preserve trailing
        # newline so we don't merge lines together.
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = f"{key}={value}{newline}"
            found[key] = True
            break

# Append any keys that were not present.
trailing = "" if (lines and lines[-1].endswith("\n")) else "\n"
for key, present in found.items():
    if not present:
        lines.append(f"{trailing}{key}={assignments[key]}\n")
        trailing = ""

env_path.write_text("".join(lines), encoding="utf-8")
print(f"Updated {env_path} (COPILOT_OPENEMR_CLIENT_ID, COPILOT_OPENEMR_CLIENT_SECRET).")
PYEOF

# Restrict .env permissions in case the umask is loose. The secret is
# now on disk; the file mode should reflect that.
chmod 600 "$ENV_FILE" || true

unset CLIENT_SECRET

echo
echo "✅ Done. Client registered and .env updated."
echo "   Restart the sidecar so it picks up the new secret:"
echo "       lsof -ti tcp:8801 | xargs -r kill"
echo "       bash $COPILOT_ROOT/launch-sidecar.command"
