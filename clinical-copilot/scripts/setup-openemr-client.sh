#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# setup-openemr-client.sh
#
# One command, fully automated. Takes the Clinical Co-Pilot sidecar from
# "broken because COPILOT_OPENEMR_CLIENT_SECRET is empty" to "fully
# verified, sidecar restarted, /health responding". Safe to re-run any
# number of times: orphan client rows never accumulate.
#
# Usage:
#     bash clinical-copilot/scripts/setup-openemr-client.sh
#     bash clinical-copilot/scripts/setup-openemr-client.sh --site=default
#     bash clinical-copilot/scripts/setup-openemr-client.sh --no-restart
#     bash clinical-copilot/scripts/setup-openemr-client.sh --force
#
# Steps:
#   0. Bootstrap clinical-copilot/.env from .env.example if missing.
#   1. Detect the running OpenEMR docker container.
#   2. Resolve the OpenEMR webroot inside that container.
#   3. Read the existing client_id and client_secret from .env, and
#      verify them against OpenEMR's /token endpoint. If they still
#      work, skip provisioning entirely (unless --force is passed).
#   4. Run `bin/console clinical-copilot:provision-api-client` inside
#      the container. That command idempotently deletes every
#      "Clinical Co-Pilot Sidecar"-named oauth_clients row, inserts a
#      fresh one, and prints a single JSON line with the credentials.
#   5. Parse the JSON, write COPILOT_OPENEMR_CLIENT_ID and
#      COPILOT_OPENEMR_CLIENT_SECRET into .env, chmod 600.
#   6. Verify the new credentials against /token. If verification
#      fails, exit non-zero with the exact HTTP status and body.
#   7. Restart the sidecar:
#        a. kill anything bound to TCP port 8801,
#        b. relaunch launch-sidecar.command in the background,
#        c. poll /health until it returns 200 (max 60 s),
#        d. on timeout, print the tail of .launch.log and exit non-zero.
#      --no-restart skips this whole block (useful in CI).
#
# Exit codes:
#   0   success
#   1   environment problem (no docker, no container, no python3, etc.)
#   2   provisioning command failed inside the container
#   3   credential verification against /token failed
#   4   sidecar relaunched but never became healthy
#  64   bad CLI argument
#  70   .env.example missing (cannot bootstrap)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COPILOT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$COPILOT_ROOT/.env"
ENV_EXAMPLE="$COPILOT_ROOT/.env.example"
LAUNCH_SCRIPT="$COPILOT_ROOT/launch-sidecar.command"
LAUNCH_LOG="$COPILOT_ROOT/.launch.log"
KEYS_DIR="$COPILOT_ROOT/.keys"
PRIVATE_KEY_PATH="$KEYS_DIR/openemr-jwt-bearer.pem"
JWKS_PATH="$KEYS_DIR/openemr-jwt-bearer.jwks.json"
JWT_HELPER="$SCRIPT_DIR/_openemr_jwt.py"
SITE="default"
RESTART=1
FORCE=0
SIDECAR_PORT=8801
SIDECAR_HEALTH_URL="http://127.0.0.1:${SIDECAR_PORT}/health"
SIDECAR_HEALTH_TIMEOUT_SECONDS=60

# ─── Argument parsing ─────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --site=*)        SITE="${arg#--site=}" ;;
    --no-restart)    RESTART=0 ;;
    --force)         FORCE=1 ;;
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

echo "── Clinical Co-Pilot setup ──"
echo "Project root  : $COPILOT_ROOT"
echo "Site          : $SITE"
echo "Auto-restart  : $([ "$RESTART" = "1" ] && echo yes || echo no)"
echo "Force re-prov : $([ "$FORCE"   = "1" ] && echo yes || echo no)"

# ─── Helper: read a single env var from .env (last assignment wins) ───────
read_env_var() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  awk -F '=' -v k="$key" '
    $0 ~ "^[[:space:]]*"k"=" {
      sub("^[^=]*=", "", $0)
      val = $0
    }
    END { print val }
  ' "$ENV_FILE"
}

# ─── Helper: rewrite a key=value pair in .env (or append if absent) ───────
# Uses python3 because in-place sed is non-portable across BSD (macOS)
# and GNU. python3 is required for the sidecar so it is always present.
write_env_var() {
  local key="$1" value="$2"
  KEY="$key" VALUE="$value" ENV_FILE="$ENV_FILE" python3 - <<'PYEOF'
import os
import pathlib

env_path = pathlib.Path(os.environ["ENV_FILE"])
key = os.environ["KEY"]
value = os.environ["VALUE"]

lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
found = False
for i, line in enumerate(lines):
    stripped = line.lstrip()
    if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
        newline = "\n" if line.endswith("\n") else ""
        lines[i] = f"{key}={value}{newline}"
        found = True
        # Don't break — overwrite every assignment of this key,
        # otherwise a stale duplicate later in the file would
        # silently override the value we just wrote.
trailing = "" if (lines and lines[-1].endswith("\n")) else "\n"
if not found:
    lines.append(f"{trailing}{key}={value}\n")
env_path.write_text("".join(lines), encoding="utf-8")
PYEOF
}

# ─── Helper: derive the host-side OAuth base URL ──────────────────────────
# Inside the sidecar container the OAuth base typically points at
# host.docker.internal so the sidecar can reach OpenEMR running on the
# host's docker daemon. From this script (running on the host) the
# same endpoint is at localhost.
host_oauth_base() {
  local raw
  raw="$(read_env_var COPILOT_OPENEMR_OAUTH_BASE)"
  if [ -z "$raw" ]; then
    raw="http://localhost:8300/oauth2/${SITE}"
  fi
  echo "$raw" | sed 's|host\.docker\.internal|localhost|g'
}

# ─── Helper: verify a (client_id, private_key) pair via /token ────────────
# Mints a real SMART Backend Services jwt-bearer assertion (RFC 7523),
# POSTs to OpenEMR /token, and returns 0 only if an access_token comes
# back. Delegates to scripts/_openemr_jwt.py so the JWT signing logic
# lives in one place.
verify_credentials() {
  local id="$1" key_path="$2"
  local base
  base="$(host_oauth_base)"
  local url="${base%/}/token"
  local insecure_flag=""
  if [ "${base#https://}" != "$base" ]; then
    insecure_flag="--insecure"
  fi
  python3 "$JWT_HELPER" verify \
    --client-id "$id" \
    --private-key "$key_path" \
    --token-url "$url" \
    $insecure_flag
}

# ─── 0. Bootstrap .env ────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  if [ ! -f "$ENV_EXAMPLE" ]; then
    echo "ERROR: $ENV_EXAMPLE missing — cannot bootstrap $ENV_FILE." >&2
    exit 70
  fi
  echo "No .env found; copying from .env.example."
  cp "$ENV_EXAMPLE" "$ENV_FILE"
fi

# ─── 1. Docker + container detection ──────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not on PATH. Install Docker Desktop from" >&2
  echo "       https://www.docker.com/products/docker-desktop/" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon not reachable. Start Docker Desktop and retry." >&2
  exit 1
fi

CONTAINER="$(docker ps --format '{{.Names}}' \
  | grep -E 'openemr' \
  | grep -Ev 'mysql|phpmyadmin|redis|couchdb|mariadb' \
  | head -1 || true)"

if [ -z "$CONTAINER" ]; then
  echo "ERROR: no running OpenEMR container found." >&2
  echo "Start the dev stack with:" >&2
  echo "    cd \"$COPILOT_ROOT/../docker/development-easy\" && docker compose up --detach --wait" >&2
  exit 1
fi
echo "OpenEMR container : $CONTAINER"

# ─── 2. Locate OpenEMR webroot inside the container ───────────────────────
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
  echo "Run \`docker exec $CONTAINER find / -name console -type f 2>/dev/null\` to locate it." >&2
  exit 1
fi
echo "OpenEMR webroot   : $OPENEMR_ROOT (in container)"

# ─── 3. Generate the RSA keypair (idempotent) ─────────────────────────────
# RFC 7523 §3 — the sidecar signs jwt-bearer assertions with this
# private key; OpenEMR verifies them against the JWKS we register on
# the oauth_clients row in step 4. Re-running with the keypair
# already in place leaves it untouched. --force regenerates.
if [ "$FORCE" = "1" ]; then
  echo "--force passed; removing any existing keypair so it is regenerated."
  rm -f "$PRIVATE_KEY_PATH" "$JWKS_PATH"
fi
echo "Ensuring RSA keypair at $PRIVATE_KEY_PATH …"
python3 "$JWT_HELPER" generate-keypair \
  --private-out "$PRIVATE_KEY_PATH" \
  --jwks-out "$JWKS_PATH" \
  --kid 'clinical-copilot-sidecar'

# ─── 4. Skip provisioning when existing creds still work ──────────────────
EXISTING_ID="$(read_env_var COPILOT_OPENEMR_CLIENT_ID)"
EXISTING_KEY_PATH="$(read_env_var COPILOT_OPENEMR_PRIVATE_KEY_PATH)"
CLIENT_ID=""
SKIPPED_PROVISION=0

if [ "$FORCE" = "0" ] \
   && [ -n "$EXISTING_ID" ] \
   && [ -n "$EXISTING_KEY_PATH" ] \
   && [ -f "$EXISTING_KEY_PATH" ]; then
  echo "Trying existing credentials in .env (client_id=${EXISTING_ID:0:12}…) …"
  if verify_credentials "$EXISTING_ID" "$EXISTING_KEY_PATH" 2>/dev/null; then
    echo "✓ Existing credentials are still valid; skipping registration."
    CLIENT_ID="$EXISTING_ID"
    SKIPPED_PROVISION=1
  else
    echo "  Existing credentials no longer work; reprovisioning."
  fi
fi

# ─── 5. Provision (or rotate) via the Symfony command ─────────────────────
if [ -z "$CLIENT_ID" ]; then
  echo "Provisioning Clinical Co-Pilot API client in OpenEMR …"

  # The container can read host-mounted paths; clinical-copilot/ is
  # mounted at /var/www/localhost/htdocs/openemr/clinical-copilot via
  # the development-easy compose. Translate the host JWKS path to its
  # in-container equivalent before passing it to the Symfony command.
  CONTAINER_JWKS_PATH="$OPENEMR_ROOT/clinical-copilot/.keys/openemr-jwt-bearer.jwks.json"
  if ! docker exec "$CONTAINER" test -f "$CONTAINER_JWKS_PATH"; then
    echo "ERROR: $CONTAINER_JWKS_PATH not visible inside the OpenEMR container." >&2
    echo "       The development-easy compose mounts the openemr/ checkout" >&2
    echo "       at $OPENEMR_ROOT, so .keys/ should be visible there. Check" >&2
    echo "       the docker volume mappings if this fails." >&2
    exit 2
  fi

  set +e
  PROV_OUTPUT="$(docker exec -u root -w "$OPENEMR_ROOT" "$CONTAINER" \
    php bin/console clinical-copilot:provision-api-client \
      --site="$SITE" \
      --jwks-json="@${CONTAINER_JWKS_PATH}" \
      2>/tmp/copilot-prov.err)"
  PROV_RC=$?
  set -e

  if [ "$PROV_RC" -ne 0 ]; then
    echo "ERROR: clinical-copilot:provision-api-client exited $PROV_RC." >&2
    echo "----- stdout -----" >&2
    printf '%s\n' "$PROV_OUTPUT" >&2
    echo "----- stderr -----" >&2
    cat /tmp/copilot-prov.err >&2 || true
    echo "------------------" >&2
    exit 2
  fi

  # Find the JSON payload. The Symfony command writes exactly one
  # JSON line to stdout; downstream wrappers may add framework noise
  # (deprecation banners, etc.) on adjacent lines, so grep for the
  # one starting with `{"client_id"`.
  JSON_LINE="$(printf '%s\n' "$PROV_OUTPUT" | grep -E '^\{"client_id"' | tail -1)"
  if [ -z "$JSON_LINE" ]; then
    echo "ERROR: provisioning command stdout did not contain a JSON payload." >&2
    echo "----- raw stdout -----" >&2
    printf '%s\n' "$PROV_OUTPUT" >&2
    echo "----------------------" >&2
    exit 2
  fi

  CLIENT_ID="$(printf '%s' "$JSON_LINE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["client_id"])')"
  ROTATED="$(printf '%s' "$JSON_LINE" | python3 -c 'import json,sys; print("yes" if json.load(sys.stdin).get("rotated") else "no")')"
  PREV_COUNT="$(printf '%s' "$JSON_LINE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("previous_count", 0))')"
  KEY_COUNT="$(printf '%s' "$JSON_LINE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("jwks_key_count", 0))')"

  if [ -z "$CLIENT_ID" ]; then
    echo "ERROR: parsed empty client_id from JSON payload." >&2
    echo "JSON: $JSON_LINE" >&2
    exit 2
  fi
  echo "✓ Provisioned (rotated=$ROTATED, removed $PREV_COUNT stale row(s), JWKS keys registered=$KEY_COUNT)."

  # ─── 6. Persist to .env ─────────────────────────────────────────────────
  write_env_var COPILOT_OPENEMR_CLIENT_ID         "$CLIENT_ID"
  write_env_var COPILOT_OPENEMR_PRIVATE_KEY_PATH  "$PRIVATE_KEY_PATH"
  # Wipe the legacy CLIENT_SECRET so an old value cannot mislead an
  # operator who checks the file. The sidecar does not use it.
  write_env_var COPILOT_OPENEMR_CLIENT_SECRET     ""
  chmod 600 "$ENV_FILE" || true
  echo "✓ Wrote COPILOT_OPENEMR_CLIENT_ID and COPILOT_OPENEMR_PRIVATE_KEY_PATH to .env (mode 0600)."

  # ─── 7. Verify the new credentials end-to-end ──────────────────────────
  echo "Verifying with a real jwt-bearer assertion against $(host_oauth_base)/token …"
  if ! verify_credentials "$CLIENT_ID" "$PRIVATE_KEY_PATH"; then
    echo "ERROR: newly provisioned credentials failed verification." >&2
    echo "       Most common causes:" >&2
    echo "         * OpenEMR mariadb not yet healthy (wait 30 s, retry)" >&2
    echo "         * OpenEMR's JWTClientAuthenticationService rejected the" >&2
    echo "           assertion: check the OpenEMR error log for hints" >&2
    echo "           (docker exec $CONTAINER tail -50 /var/log/apache2/error.log)" >&2
    exit 3
  fi
  echo "✓ Verified: OpenEMR issued an access token for the new client."
fi

# ─── 7. Restart the sidecar ───────────────────────────────────────────────
if [ "$RESTART" = "0" ]; then
  echo "Skipping sidecar restart (--no-restart). Restart manually with:"
  echo "    lsof -ti tcp:${SIDECAR_PORT} | xargs -r kill && bash $LAUNCH_SCRIPT"
  echo "── done ──"
  exit 0
fi

# Skip restart if we did nothing: existing creds were valid AND no sidecar is up.
if [ "$SKIPPED_PROVISION" = "1" ] && ! lsof -ti "tcp:${SIDECAR_PORT}" >/dev/null 2>&1; then
  echo "Sidecar is not running and creds are unchanged; nothing to restart."
  echo "Start the sidecar with:  bash $LAUNCH_SCRIPT"
  echo "── done ──"
  exit 0
fi

SIDECAR_PIDS="$(lsof -ti "tcp:${SIDECAR_PORT}" 2>/dev/null || true)"
if [ -n "$SIDECAR_PIDS" ]; then
  echo "Stopping running sidecar PID(s): $SIDECAR_PIDS"
  # shellcheck disable=SC2086
  kill $SIDECAR_PIDS 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if ! lsof -ti "tcp:${SIDECAR_PORT}" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  if lsof -ti "tcp:${SIDECAR_PORT}" >/dev/null 2>&1; then
    echo "  Sidecar did not exit after SIGTERM; sending SIGKILL." >&2
    # shellcheck disable=SC2086
    kill -9 $SIDECAR_PIDS 2>/dev/null || true
    sleep 1
  fi
fi

if [ ! -x "$LAUNCH_SCRIPT" ] && [ ! -f "$LAUNCH_SCRIPT" ]; then
  echo "ERROR: $LAUNCH_SCRIPT not found; cannot relaunch sidecar." >&2
  echo "       Start it manually once it exists." >&2
  exit 1
fi

echo "Relaunching sidecar in background (logs: $LAUNCH_LOG) …"
# `nohup` so the background process survives this script's exit.
# `setsid` would be cleaner but is not available on macOS by default;
# `disown` removes the job from the current shell's job table so the
# parent shell does not reap it on exit.
nohup bash "$LAUNCH_SCRIPT" > "$LAUNCH_LOG" 2>&1 &
SIDECAR_PID=$!
disown 2>/dev/null || true

echo "Waiting up to ${SIDECAR_HEALTH_TIMEOUT_SECONDS}s for ${SIDECAR_HEALTH_URL} …"
HEALTHY=0
for _ in $(seq 1 "$SIDECAR_HEALTH_TIMEOUT_SECONDS"); do
  if curl -fsS "$SIDECAR_HEALTH_URL" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  # If the launcher exited (e.g. dependency install failure), bail
  # immediately rather than wait the full timeout.
  if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
    echo "ERROR: launcher process $SIDECAR_PID exited before /health responded." >&2
    echo "----- last 40 lines of $LAUNCH_LOG -----" >&2
    tail -40 "$LAUNCH_LOG" >&2 || true
    echo "----------------------------------------" >&2
    exit 4
  fi
  sleep 1
done

if [ "$HEALTHY" != "1" ]; then
  echo "ERROR: sidecar did not become healthy within ${SIDECAR_HEALTH_TIMEOUT_SECONDS}s." >&2
  echo "----- last 40 lines of $LAUNCH_LOG -----" >&2
  tail -40 "$LAUNCH_LOG" >&2 || true
  echo "----------------------------------------" >&2
  exit 4
fi

echo "✓ Sidecar is healthy on $SIDECAR_HEALTH_URL"
echo "✅ All set. Open the Clinical Co-Pilot from any patient summary in OpenEMR."
echo "── done ──"
