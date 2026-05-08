#!/usr/bin/env bash
# Launch the modern patient dashboard dev server.
#
# Three pieces of state are easy to drop on a manual `npm run dev`
# restart and break the dashboard's silent OIDC dance:
#
#   1. NODE_TLS_REJECT_UNAUTHORIZED=0 — Node rejects OpenEMR's
#      self-signed localhost cert when fetching the OIDC discovery
#      doc. Without this set, Auth.js fires its "Configuration"
#      error and the dashboard renders the /login error page
#      instead of the patient cards.
#
#   2. PORT=8400 — the dashboard's OAuth client's redirect_uri is
#      registered as https://localhost:8400/api/auth/callback/openemr
#      in OpenEMR's oauth_clients table. Running on any other port
#      makes Auth.js's callback fail mid-OAuth.
#
#   3. --experimental-https — the dashboard runs as HTTPS so it can
#      be loaded inside OpenEMR's HTTPS shell iframe without
#      tripping mixed-content policies. Chrome silently allows the
#      HTTP-in-HTTPS iframe for localhost; Safari (and Firefox by
#      default) don't, and the iframe goes black with no rendered
#      content. Next.js 13.5+ accepts --experimental-https; on
#      first launch it auto-generates a self-signed cert at
#      certificates/localhost.pem.
#
# Idempotent: if anything is already listening on :8400 it kills it
# first. Logs go to /tmp/dashboard-dev.log so a second tab can
# `tail -f /tmp/dashboard-dev.log`.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
LOG="/tmp/dashboard-dev.log"
PORT="${PORT:-8400}"

echo "── Patient Dashboard launcher ──"
echo "Working dir : $ROOT"
echo "Port        : $PORT"
echo "Log         : $LOG"

# Kill anything on the target port — typically a stale `next dev`
# from a previous session.
EXISTING_PID="$(lsof -ti:"$PORT" 2>/dev/null || true)"
if [ -n "$EXISTING_PID" ]; then
    echo "Killing existing process(es) on :$PORT — $EXISTING_PID"
    kill "$EXISTING_PID" 2>/dev/null || true
    sleep 2
    # Hard kill if still alive.
    EXISTING_PID="$(lsof -ti:"$PORT" 2>/dev/null || true)"
    if [ -n "$EXISTING_PID" ]; then
        kill -9 "$EXISTING_PID" 2>/dev/null || true
        sleep 1
    fi
fi

if [ ! -d node_modules ]; then
    echo "Installing dependencies (first run)…"
    npm install --silent
fi

# If openssl cert files don't exist yet, generate them. These are
# preferred over Next.js's mkcert auto-generate path because mkcert
# tries to install a root CA via sudo, which doesn't work
# non-interactively in launch scripts. Browsers will warn the first
# time the dashboard is loaded; the user accepts the cert by visiting
# https://localhost:8400 directly. Subsequent loads (incl. iframes)
# work without prompts.
CERT_DIR="$ROOT/certificates"
CERT_KEY="$CERT_DIR/localhost-key.pem"
CERT_CRT="$CERT_DIR/localhost.pem"
if [ ! -f "$CERT_KEY" ] || [ ! -f "$CERT_CRT" ]; then
    echo "Generating self-signed cert at $CERT_DIR …"
    mkdir -p "$CERT_DIR"
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$CERT_KEY" \
        -out "$CERT_CRT" \
        -days 825 \
        -subj "/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1" \
        >/dev/null 2>&1
    echo "Done. ACCEPT THE CERT IN YOUR BROWSER:"
    echo "  - Visit https://localhost:$PORT directly (any path)"
    echo "  - Click through the 'Not Secure' warning once"
    echo "  - The OpenEMR iframe will then load without mixed-content blocks"
fi

echo "Starting on https://localhost:$PORT/  (logs: $LOG)"
echo "Press Ctrl-C to stop."
exec env \
    NODE_TLS_REJECT_UNAUTHORIZED=0 \
    PORT="$PORT" \
    npx next dev \
        --experimental-https \
        --experimental-https-key "$CERT_KEY" \
        --experimental-https-cert "$CERT_CRT" \
        --port "$PORT" 2>&1 | tee "$LOG"
