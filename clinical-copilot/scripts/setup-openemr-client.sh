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
#   5   sidecar restarted but is running stale code (drift detected via /diagnostic)
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

# ─── Pick the right Python and ensure cryptography is importable ──────────
# Prefer the sidecar's own .venv/bin/python — it already has every
# dependency from pyproject.toml installed, including the cryptography
# wheel that _openemr_jwt.py imports for RSA keygen and signing.
# Fall back to system python3 only when no .venv exists yet (fresh
# clone before the first launch-sidecar.command run).
if [ -x "$COPILOT_ROOT/.venv/bin/python" ]; then
  PY="$COPILOT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "ERROR: no usable Python found." >&2
  echo "       Install Python 3.11+ with \`brew install python@3.12\` or" >&2
  echo "       run \`bash $LAUNCH_SCRIPT\` once to bootstrap the sidecar's" >&2
  echo "       .venv, then re-run this script." >&2
  exit 1
fi
echo "Python        : $PY ($("$PY" -V 2>&1))"

# Make sure cryptography is importable. The first run on a host that
# only has system python3 will hit this path; the sidecar's .venv
# already has it from PyJWT[crypto] in pyproject.toml.
if ! "$PY" -c 'import cryptography' >/dev/null 2>&1; then
  echo "Installing cryptography for $PY (one-time, ~5 s) …"
  if "$PY" -m pip install --quiet cryptography 2>/dev/null; then
    :
  elif "$PY" -m pip install --quiet --user cryptography 2>/dev/null; then
    :
  elif "$PY" -m pip install --quiet --break-system-packages cryptography 2>/dev/null; then
    :
  else
    echo "ERROR: could not install cryptography for $PY." >&2
    echo "       Try installing it manually:" >&2
    echo "         $PY -m pip install cryptography" >&2
    echo "       Or bootstrap the sidecar .venv first:" >&2
    echo "         bash $LAUNCH_SCRIPT  (Ctrl-C once it boots, then re-run this)" >&2
    exit 1
  fi
fi

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
  KEY="$key" VALUE="$value" ENV_FILE="$ENV_FILE" "$PY" - <<'PYEOF'
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
# Initial bootstrap value. The script later replaces this with whatever
# OpenEMR reports as its canonical token_endpoint via the SMART
# discovery document (/.well-known/smart-configuration), so anything
# in .env can be wrong without breaking the flow.
host_oauth_base() {
  local raw
  raw="$(read_env_var COPILOT_OPENEMR_OAUTH_BASE)"
  if [ -z "$raw" ]; then
    raw="http://localhost:8300/oauth2/${SITE}"
  fi
  echo "$raw" | sed 's|host\.docker\.internal|localhost|g'
}

# ─── Helper: discover OpenEMR's canonical OAuth + FHIR endpoints ──────────
# Per the SMART App Launch v2 spec, every SMART-on-FHIR server publishes
# a `/.well-known/smart-configuration` JSON document advertising the
# absolute URLs of its token_endpoint and other OAuth metadata. OpenEMR's
# JWTClientAuthenticationService uses the same configured site_addr_oath
# to validate the `aud` claim, so reading the discovery document is the
# only reliable way to learn the exact URL to put in `aud` (and to POST
# to). Without it we are guessing — and any guess that does not match
# site_addr_oath byte-for-byte fails with `invalid_client`.
#
# Sets the globals TOKEN_URL and FHIR_BASE_URL on success, exits non-zero
# on failure with an explanatory message.
discover_smart_endpoints() {
  local oauth_seed fhir_seed
  oauth_seed="$(host_oauth_base)"
  # Try the OAuth-base well-known first, then the FHIR-base well-known.
  # OpenEMR serves both, but only the OAuth base is reliably present
  # from a fresh .env.
  local candidates=(
    "${oauth_seed%/}/.well-known/smart-configuration"
    "$(read_env_var COPILOT_OPENEMR_FHIR_BASE)/.well-known/smart-configuration"
    # Fall back to the OpenEMR HTTPS port + canonical site_addr_oath
    # path, since the dev-easy compose puts SSL on :9300.
    "https://localhost:9300/oauth2/${SITE}/.well-known/smart-configuration"
    "https://localhost:9300/apis/${SITE}/fhir/.well-known/smart-configuration"
  )
  local body=""
  local url=""
  for url in "${candidates[@]}"; do
    [ -z "$url" ] && continue
    body="$(curl -sS -k --max-time 5 "$url" 2>/dev/null || true)"
    if [ -n "$body" ] && printf '%s' "$body" | grep -q '"token_endpoint"'; then
      echo "Discovered SMART config at $url"
      break
    fi
    body=""
  done
  if [ -z "$body" ]; then
    echo "ERROR: could not fetch /.well-known/smart-configuration from any of:" >&2
    printf '  %s\n' "${candidates[@]}" >&2
    echo "       OpenEMR may not be reachable. Check the openemr container is up:" >&2
    echo "       docker logs --tail 30 $CONTAINER" >&2
    return 1
  fi
  TOKEN_URL="$(printf '%s' "$body" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["token_endpoint"])')"
  # Some servers publish a `fhir_endpoint` in the document; OpenEMR
  # publishes its FHIR base as the `issuer` field, but the universally
  # safe path is to derive it by stripping `/oauth2/<site>` off the
  # token_endpoint and replacing it with `/apis/<site>/fhir`.
  FHIR_BASE_URL="${TOKEN_URL%/oauth2/${SITE}/token}/apis/${SITE}/fhir"
  if [ -z "$TOKEN_URL" ] || [ "$TOKEN_URL" = "$FHIR_BASE_URL" ]; then
    echo "ERROR: smart-configuration response missing token_endpoint:" >&2
    printf '%s\n' "$body" | head -c 400 >&2
    echo "" >&2
    return 1
  fi
}

# ─── Helper: verify a (client_id, private_key) pair via /token ────────────
# Mints a real SMART Backend Services jwt-bearer assertion (RFC 7523),
# POSTs to OpenEMR /token, and returns 0 only if an access_token comes
# back. Uses the canonical TOKEN_URL discovered from the SMART
# discovery document (NOT whatever is in .env), because OpenEMR's
# PermittedFor constraint on the `aud` claim only matches that exact
# URL byte-for-byte.
verify_credentials() {
  local id="$1" key_path="$2"
  local insecure_flag=""
  if [ "${TOKEN_URL#https://}" != "$TOKEN_URL" ]; then
    insecure_flag="--insecure"
  fi
  "$PY" "$JWT_HELPER" verify \
    --client-id "$id" \
    --private-key "$key_path" \
    --token-url "$TOKEN_URL" \
    $insecure_flag
}

# ─── Helper: surface OpenEMR's actual rejection reason on failure ─────────
# The /token endpoint returns a generic invalid_client even when the real
# cause is a specific constraint violation (signature mismatch, audience
# mismatch, JWKS lookup failure, JTI replay, etc.). The detail lives in
# OpenEMR's PHP error log. Tail the last few hundred lines, grep for
# anything that mentions our client_id, JWT, or assertion, and print
# that plus the JWKS we registered, the JWT header we just sent, and
# the resolved audience so the operator can compare them side by side.
dump_openemr_diagnostics() {
  local container="$1" id="$2" key_path="$3" jwks_path="$4"
  local audience
  audience="$(host_oauth_base)/token"

  echo "─── DIAGNOSTICS ──────────────────────────────────────────────" >&2

  echo "JWT we sent (header + payload, decoded):" >&2
  "$PY" - "$id" "$key_path" "$audience" <<'PYEOF' >&2 2>&1 || true
import sys, json, base64, time, secrets
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key

client_id, key_path, aud = sys.argv[1], sys.argv[2], sys.argv[3]
pem = open(key_path, 'rb').read()
key = load_pem_private_key(pem, password=None)
now = int(time.time())
header = {"alg": "RS384", "typ": "JWT", "kid": "clinical-copilot-sidecar"}
payload = {
    "iss": client_id, "sub": client_id, "aud": aud,
    "exp": now + 240, "iat": now, "jti": secrets.token_urlsafe(16),
}
def b64(d):
    return base64.urlsafe_b64encode(d).rstrip(b'=').decode('ascii')
h = b64(json.dumps(header, separators=(',',':')).encode())
p = b64(json.dumps(payload, separators=(',',':')).encode())
print('  header :', json.dumps(header, indent=2).replace('\n', '\n           '))
print('  payload:', json.dumps(payload, indent=2).replace('\n', '\n           '))

# Public key thumbprint so we can cross-check against the registered JWKS.
pub_pem = key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)
import hashlib
print('  pubkey SHA-256 fp (host):', hashlib.sha256(pub_pem).hexdigest()[:32], '…')
PYEOF

  echo "" >&2
  echo "JWKS registered with OpenEMR (oauth_clients.jwks for this client):" >&2
  docker exec "$container" mariadb \
    -h mysql -u openemr -popenemr -D openemr -N -B \
    -e "SELECT jwks FROM oauth_clients WHERE client_id = '$id';" 2>/dev/null \
    | "$PY" -c '
import sys, json
raw = sys.stdin.read().strip()
if not raw:
    print("  (empty result — client_id not found in oauth_clients)")
else:
    try:
        data = json.loads(raw)
        for k in data.get("keys", []):
            kid = k.get("kid"); alg = k.get("alg")
            use = k.get("use"); kty = k.get("kty")
            print("  kid={} alg={} use={} kty={}".format(kid, alg, use, kty))
        full = json.dumps(data, indent=2).replace("\n", "\n         ")
        print("  full:", full)
    except Exception as e:
        print("  parse error: {}; raw repr: {!r}".format(e, raw[:200]))
' 2>&1 || echo "  (could not query oauth_clients)" >&2

  echo "" >&2
  echo "What OpenEMR thinks ITS token URL is (compare with our aud above):" >&2
  docker exec "$container" mariadb \
    -h mysql -u openemr -popenemr -D openemr -N -B \
    -e "SELECT gl_value FROM globals WHERE gl_name IN ('site_addr_oath','rest_api_path','fhir_address') ORDER BY gl_name;" \
    2>/dev/null | sed 's/^/  /' >&2 || echo "  (could not query globals)" >&2
  echo "  Audience we used        : $audience" >&2

  echo "" >&2
  echo "OpenEMR PHP error log (last ~80 lines, filtered for JWT/assertion/oauth):" >&2
  docker exec "$container" sh -c '
    for f in /var/log/apache2/error.log /var/log/apache2/error_log /var/log/php_errors.log; do
      if [ -f "$f" ]; then echo "── $f ──"; tail -200 "$f"; fi
    done
  ' 2>/dev/null \
    | grep -iE 'jwt|assertion|client_id|oauth|jwks|signature|audience|invalid_client|constraint|RsaSha' \
    | tail -80 \
    | sed 's/^/  /' >&2 \
    || echo "  (no matching log lines)" >&2

  echo "──────────────────────────────────────────────────────────────" >&2
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

# ─── 2.5. Discover canonical OpenEMR endpoints via SMART config ───────────
echo "Discovering OpenEMR SMART endpoints …"
if ! discover_smart_endpoints; then
  exit 1
fi
echo "Token endpoint    : $TOKEN_URL"
echo "FHIR base         : $FHIR_BASE_URL"

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
"$PY" "$JWT_HELPER" generate-keypair \
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

  CLIENT_ID="$(printf '%s' "$JSON_LINE" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["client_id"])')"
  ROTATED="$(printf '%s' "$JSON_LINE" | "$PY" -c 'import json,sys; print("yes" if json.load(sys.stdin).get("rotated") else "no")')"
  PREV_COUNT="$(printf '%s' "$JSON_LINE" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("previous_count", 0))')"
  KEY_COUNT="$(printf '%s' "$JSON_LINE" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("jwks_key_count", 0))')"

  if [ -z "$CLIENT_ID" ]; then
    echo "ERROR: parsed empty client_id from JSON payload." >&2
    echo "JSON: $JSON_LINE" >&2
    exit 2
  fi
  echo "✓ Provisioned (rotated=$ROTATED, removed $PREV_COUNT stale row(s), JWKS keys registered=$KEY_COUNT)."

  # ─── 6. Persist to .env ─────────────────────────────────────────────────
  # Use the canonical OAuth + FHIR base URLs we discovered. The
  # /.well-known/smart-configuration document is the authoritative
  # source — anything else (a stale .env entry, a host.docker.internal
  # reference left over from a docker-deploy attempt) would force the
  # sidecar to send the wrong `aud` and fail every /token call with
  # invalid_client.
  # `local` is a function-only builtin; this block runs at top level.
  OAUTH_BASE="${TOKEN_URL%/token}"
  write_env_var COPILOT_OPENEMR_CLIENT_ID         "$CLIENT_ID"
  write_env_var COPILOT_OPENEMR_PRIVATE_KEY_PATH  "$PRIVATE_KEY_PATH"
  write_env_var COPILOT_OPENEMR_OAUTH_BASE        "$OAUTH_BASE"
  write_env_var COPILOT_OPENEMR_FHIR_BASE         "$FHIR_BASE_URL"
  # OpenEMR development-easy uses a self-signed certificate on :9300;
  # the sidecar must accept it or the FHIR client library refuses
  # the connection.
  if [ "${TOKEN_URL#https://}" != "$TOKEN_URL" ]; then
    write_env_var COPILOT_FHIR_VERIFY_SSL         "false"
  fi
  # Wipe the legacy CLIENT_SECRET so an old value cannot mislead an
  # operator who checks the file. The sidecar does not use it.
  write_env_var COPILOT_OPENEMR_CLIENT_SECRET     ""
  chmod 600 "$ENV_FILE" || true
  echo "✓ Wrote CLIENT_ID, PRIVATE_KEY_PATH, OAUTH_BASE, FHIR_BASE to .env (mode 0600)."

  # ─── 7. Verify the new credentials end-to-end ──────────────────────────
  echo "Verifying with a real jwt-bearer assertion against $(host_oauth_base)/token …"
  if ! verify_credentials "$CLIENT_ID" "$PRIVATE_KEY_PATH"; then
    echo "ERROR: newly provisioned credentials failed verification." >&2
    echo "" >&2
    dump_openemr_diagnostics "$CONTAINER" "$CLIENT_ID" "$PRIVATE_KEY_PATH" "$JWKS_PATH"
    exit 3
  fi
  echo "✓ Verified: OpenEMR issued an access token for the new client."
fi

# ─── 7.5. Pin OpenEMR's clinical_copilot_url global to the local sidecar ──
# The launch button generates URLs from this global. If it is left
# pointing at a previous remote deployment (e.g. http://5.161.253.237:8801)
# every click opens the OLD remote sidecar instead of the local one we
# just verified, and every error message is on stale code. Force it to
# the local sidecar so a single source of truth wins.
DESIRED_LAUNCH_URL="http://localhost:${SIDECAR_PORT}"
echo "Pinning OpenEMR clinical_copilot_url global → $DESIRED_LAUNCH_URL …"
docker exec "$CONTAINER" mariadb -h mysql -uopenemr -popenemr -Dopenemr \
  -e "INSERT INTO globals (gl_name, gl_index, gl_value) VALUES ('clinical_copilot_url', 0, '$DESIRED_LAUNCH_URL') ON DUPLICATE KEY UPDATE gl_value='$DESIRED_LAUNCH_URL';" \
  >/dev/null 2>&1 \
  && echo "✓ Pinned." \
  || echo "WARNING: could not update clinical_copilot_url global; set it via Admin → Globals → Miscellaneous." >&2

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

# ─── Catch a Docker container hijacking port 8801 ─────────────────────────
# A previous `docker compose up` of clinical-copilot/docker-compose.yml
# leaves Docker's port-forwarder (com.docker.backend) holding *:8801 on
# the host. Browser requests then route to the in-container sidecar,
# which is built from a (stale) image — every error in the UI looks
# like the sidecar restart did nothing. Stop that compose stack first
# so uvicorn on 127.0.0.1:8801 actually serves the requests.
if lsof -nP -iTCP:${SIDECAR_PORT} -sTCP:LISTEN 2>/dev/null | grep -qi 'docker'; then
  echo "Detected a Docker container forwarding TCP port ${SIDECAR_PORT}; stopping it."
  echo "  (this is the clinical-copilot/docker-compose.yml stack — the dev-easy stack is unaffected)"
  docker compose -f "$COPILOT_ROOT/docker-compose.yml" down >/dev/null 2>&1 || true
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

# ─── Force the .venv to pick up the latest source ─────────────────────────
# A stale editable install (or stale .pyc cache) was the root cause of a
# multi-hour debugging session: the sidecar reported errors from old
# code paths even after the source had been rewritten. Nuke pyc caches
# and re-link the editable install so the next launcher run cannot get
# this wrong.
if [ -d "$COPILOT_ROOT/.venv" ]; then
  echo "Refreshing sidecar editable install (pip install -e . --quiet) …"
  find "$COPILOT_ROOT/sidecar" "$COPILOT_ROOT/bff" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
  rm -rf "$COPILOT_ROOT/clinical_copilot.egg-info" 2>/dev/null || true
  if ! "$COPILOT_ROOT/.venv/bin/python" -m pip install --quiet --upgrade -e "$COPILOT_ROOT" 2>/tmp/copilot-pip.err; then
    echo "WARNING: pip install -e . failed; the sidecar may load stale code." >&2
    tail -10 /tmp/copilot-pip.err >&2 || true
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

# ─── Verify the relaunched sidecar is running CURRENT code ────────────────
# /health only proves a process is listening — the failure mode that
# wasted multiple debugging cycles was the sidecar coming up with a
# stale .venv that imported old openemr_oauth.py / chat.py code. The
# /diagnostic endpoint exposes the git hash, the auth method actually
# loaded, and the purpose-check class (membership vs strict equality),
# so we can refuse to declare success when any of these drifts.
DIAG_URL="http://127.0.0.1:${SIDECAR_PORT}/diagnostic"
EXPECTED_HASH="$(git -C "$COPILOT_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "Verifying sidecar is running current code via $DIAG_URL …"

DIAG_JSON="$(curl -fsS "$DIAG_URL" 2>/dev/null || echo '')"
if [ -z "$DIAG_JSON" ]; then
  echo "WARNING: /diagnostic did not respond. The sidecar is up but may be running stale code." >&2
  echo "         Open $DIAG_URL in a browser once you have a chance." >&2
else
  # Extract every field we care about with one Python invocation so a
  # parse error fails loudly instead of trickling through grep.
  DIAG_REPORT="$(printf '%s' "$DIAG_JSON" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
v = d.get("version", {}); c = d.get("checks", {}); cfg = d.get("config", {})
print("git_hash:" + str(v.get("git_hash")))
print("auth_method:" + str(c.get("auth_method")))
print("purpose_check:" + str(c.get("task_token_purpose_check")))
print("module_path:" + str(v.get("openemr_oauth_module")))
print("key_present:" + str(c.get("private_key_file", {}).get("present")))
print("oauth_base:" + str(cfg.get("openemr_oauth_base")))
')"
  RUNNING_HASH=$(printf '%s' "$DIAG_REPORT" | sed -n 's/^git_hash://p')
  AUTH_METHOD=$(printf '%s'  "$DIAG_REPORT" | sed -n 's/^auth_method://p')
  PURPOSE_CHECK=$(printf '%s' "$DIAG_REPORT" | sed -n 's/^purpose_check://p')
  MODULE_PATH=$(printf '%s'  "$DIAG_REPORT" | sed -n 's/^module_path://p')
  KEY_PRESENT=$(printf '%s'  "$DIAG_REPORT" | sed -n 's/^key_present://p')
  echo "  git hash       : $RUNNING_HASH (expected $EXPECTED_HASH)"
  echo "  auth method    : $AUTH_METHOD"
  echo "  purpose check  : $PURPOSE_CHECK"
  echo "  module path    : $MODULE_PATH"
  echo "  private key    : present=$KEY_PRESENT"

  STALE=0
  if [ -n "$EXPECTED_HASH" ] && [ "$EXPECTED_HASH" != "unknown" ] && [ "$RUNNING_HASH" != "$EXPECTED_HASH" ]; then
    echo "ERROR: running git hash ($RUNNING_HASH) does not match repo HEAD ($EXPECTED_HASH)." >&2
    STALE=1
  fi
  if [ "$AUTH_METHOD" != "private_key_jwt" ]; then
    echo "ERROR: sidecar auth method is '$AUTH_METHOD', expected 'private_key_jwt'." >&2
    echo "       The running code is the legacy HTTP-Basic + client_secret version." >&2
    STALE=1
  fi
  if [ "$PURPOSE_CHECK" != "membership_in_authorized_purposes" ]; then
    echo "ERROR: sidecar purpose check is '$PURPOSE_CHECK', expected 'membership_in_authorized_purposes'." >&2
    echo "       The running chat.py is the legacy strict-equality version." >&2
    STALE=1
  fi
  if [ "$STALE" = "1" ]; then
    echo "" >&2
    echo "Diagnosis: the sidecar restarted but is still importing stale code." >&2
    echo "Most likely the .venv editable install or pyc cache is broken. Fix with:" >&2
    echo "  rm -rf $COPILOT_ROOT/.venv" >&2
    echo "  rm -rf $COPILOT_ROOT/clinical_copilot.egg-info" >&2
    echo "  find $COPILOT_ROOT -type d -name __pycache__ -exec rm -rf {} +" >&2
    echo "  bash $LAUNCH_SCRIPT  # rebuild .venv from scratch" >&2
    echo "  bash $0              # rerun this script" >&2
    exit 5
  fi
  echo "✓ Sidecar is running current code (auth=jwt-bearer, purpose=membership)."
fi

echo "✅ All set. Open the Clinical Co-Pilot from any patient summary in OpenEMR."
echo "── done ──"
