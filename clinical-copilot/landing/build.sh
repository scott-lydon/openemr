#!/usr/bin/env bash
#
# build.sh — produce the sales-site distribution artifacts.
#
# Run from anywhere; the script resolves the openemr repo root by walking
# up from its own location. Idempotent — re-running overwrites the
# previous downloads/ and legal/ contents.
#
# What it does:
#
# 1. Copies clinical-copilot/legal/*.md into landing/legal/ so the
#    in-browser markdown viewer (baa.html, privacy.html, terms.html)
#    can fetch the legal docs over a static-host (the markdown source
#    of truth still lives in clinical-copilot/legal/).
#
# 2. Packages the OpenEMR module folder
#    interface/modules/custom_modules/oe-module-clinical-copilot
#    into landing/downloads/oe-module-clinical-copilot-<version>.zip
#    The zip preserves the canonical install layout — recipients can
#    unzip directly into their OpenEMR's custom_modules/.
#
# 3. Generates a SHA-256 sidecar file
#    landing/downloads/<name>.zip.sha256 so visitors can verify the zip.
#
# 4. Creates a stable "latest" alias by copying the versioned zip to
#    oe-module-clinical-copilot-latest.zip and writing the matching
#    .sha256, so the install.html download button never goes stale.
#
# 5. Writes landing/install.html#version-string and #sha256-string
#    inline updates by piping through a small sed pass.
#
# Every failure must produce a clear error message so the operator can
# fix the cause without having to grep the script. That is enforced by
# `set -euo pipefail` plus explicit error messages on every command that
# could fail.

set -euo pipefail

# -----------------------------------------------------------------------------
# Resolve paths
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LANDING_DIR="${SCRIPT_DIR}"
COPILOT_DIR="$(cd "${LANDING_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${COPILOT_DIR}/.." && pwd)"
MODULE_DIR="${REPO_ROOT}/interface/modules/custom_modules/oe-module-clinical-copilot"
LEGAL_SRC_DIR="${COPILOT_DIR}/legal"

if [[ ! -d "${MODULE_DIR}" ]]; then
    echo "FATAL: module directory not found at ${MODULE_DIR}." >&2
    echo "The build expected the OpenEMR repo layout. Run this script from inside the openemr repo." >&2
    exit 2
fi

if [[ ! -d "${LEGAL_SRC_DIR}" ]]; then
    echo "FATAL: legal source directory not found at ${LEGAL_SRC_DIR}." >&2
    echo "Expected the markdown legal docs (BAA, ToS, Privacy, Trust) to live there." >&2
    exit 2
fi

# -----------------------------------------------------------------------------
# Resolve version
# -----------------------------------------------------------------------------
VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
    # Try to derive from the module's composer.json `version` field.
    if command -v jq >/dev/null 2>&1; then
        VERSION="$(jq -r '.version // empty' "${MODULE_DIR}/composer.json" 2>/dev/null || true)"
    fi
fi
if [[ -z "${VERSION}" ]]; then
    # Fall back to a git-derived version, then to 0.1.0.
    if git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        VERSION="$(git -C "${REPO_ROOT}" describe --tags --always --dirty 2>/dev/null || true)"
        VERSION="${VERSION#v}"
    fi
fi
if [[ -z "${VERSION}" ]]; then
    VERSION="0.1.0"
fi
echo "[build] version: ${VERSION}"

# -----------------------------------------------------------------------------
# 1. Copy legal markdown into landing/legal/ so the in-browser viewer can
#    fetch it under the static host root.
# -----------------------------------------------------------------------------
DEST_LEGAL_DIR="${LANDING_DIR}/legal"
echo "[build] mirroring legal docs from ${LEGAL_SRC_DIR} to ${DEST_LEGAL_DIR}"
mkdir -p "${DEST_LEGAL_DIR}"
# Copy every .md from legal/. Keep filenames exact (the HTML viewers
# reference them by name).
for f in "${LEGAL_SRC_DIR}"/*.md; do
    cp -f "${f}" "${DEST_LEGAL_DIR}/$(basename "${f}")"
done
ls "${DEST_LEGAL_DIR}" | sed 's/^/    /'

# -----------------------------------------------------------------------------
# 2. Build the module zip into landing/downloads/.
# -----------------------------------------------------------------------------
DOWNLOADS_DIR="${LANDING_DIR}/downloads"
mkdir -p "${DOWNLOADS_DIR}"

ZIP_BASENAME="oe-module-clinical-copilot-${VERSION}.zip"
ZIP_OUT="${DOWNLOADS_DIR}/${ZIP_BASENAME}"

# Use a clean staging dir so the zip layout is deterministic. We want the
# archive to contain the module folder at its canonical relative path —
# `interface/modules/custom_modules/oe-module-clinical-copilot/...` — so
# operators can unzip from the openemr install root.
STAGE_DIR="$(mktemp -d -t oe-module-clinical-copilot.XXXX)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

ARCHIVE_REL="interface/modules/custom_modules/oe-module-clinical-copilot"
mkdir -p "${STAGE_DIR}/${ARCHIVE_REL}"

echo "[build] copying module into staging dir"
# Exclude vendor/ and node_modules/ and macOS dot files. The module ships
# without vendor/ — composer install runs on the customer side.
rsync -a --delete \
    --exclude=".DS_Store" \
    --exclude="._*" \
    --exclude="vendor/" \
    --exclude="node_modules/" \
    --exclude="*.swp" \
    "${MODULE_DIR}/" "${STAGE_DIR}/${ARCHIVE_REL}/"

echo "[build] zipping module to ${ZIP_OUT}"
( cd "${STAGE_DIR}" && zip -r -q "${ZIP_OUT}" "interface" )

# -----------------------------------------------------------------------------
# 3. Generate sha256 sidecar.
# -----------------------------------------------------------------------------
echo "[build] hashing zip"
if command -v shasum >/dev/null 2>&1; then
    ( cd "${DOWNLOADS_DIR}" && shasum -a 256 "${ZIP_BASENAME}" > "${ZIP_BASENAME}.sha256" )
elif command -v sha256sum >/dev/null 2>&1; then
    ( cd "${DOWNLOADS_DIR}" && sha256sum "${ZIP_BASENAME}" > "${ZIP_BASENAME}.sha256" )
else
    echo "FATAL: neither shasum nor sha256sum is available on PATH; cannot generate checksum." >&2
    exit 3
fi

# -----------------------------------------------------------------------------
# 4. Create a stable "latest" alias so the website does not need editing
#    on every release.
# -----------------------------------------------------------------------------
LATEST_ZIP="oe-module-clinical-copilot-latest.zip"
echo "[build] aliasing ${ZIP_BASENAME} -> ${LATEST_ZIP}"
cp -f "${DOWNLOADS_DIR}/${ZIP_BASENAME}" "${DOWNLOADS_DIR}/${LATEST_ZIP}"
( cd "${DOWNLOADS_DIR}" && shasum -a 256 "${LATEST_ZIP}" > "${LATEST_ZIP}.sha256" 2>/dev/null \
    || sha256sum "${LATEST_ZIP}" > "${LATEST_ZIP}.sha256" )

# -----------------------------------------------------------------------------
# 5. Patch install.html with the resolved version and SHA-256 so the
#    page shows current values even before a static-site rebuild has
#    re-run any templater.
# -----------------------------------------------------------------------------
INSTALL_HTML="${LANDING_DIR}/install.html"
SHA_HEX="$(awk '{print $1}' "${DOWNLOADS_DIR}/${LATEST_ZIP}.sha256")"

echo "[build] patching install.html with version=${VERSION} sha256=${SHA_HEX}"
# In-place sed with macOS / GNU compatibility (-i '' on macOS, -i on Linux).
SED_INPLACE=(-i)
if [[ "$(uname -s)" == "Darwin" ]]; then
    SED_INPLACE=(-i '')
fi

# The two markers we wrote into install.html earlier.
# - id="version-string" wraps the human version
# - id="sha256-string" wraps the SHA-256 line
sed "${SED_INPLACE[@]}" -e "s|<strong style=\"color: var(--text);\" id=\"version-string\">[^<]*</strong>|<strong style=\"color: var(--text);\" id=\"version-string\">v${VERSION}</strong>|" "${INSTALL_HTML}"
sed "${SED_INPLACE[@]}" -e "s|<code id=\"sha256-string\">[^<]*</code>|<code id=\"sha256-string\">SHA-256: ${SHA_HEX}</code>|" "${INSTALL_HTML}"

# -----------------------------------------------------------------------------
# 6. Write a small DOWNLOADS.md describing what is in here.
# -----------------------------------------------------------------------------
cat > "${DOWNLOADS_DIR}/README.md" <<EOF
# Clinical Co-Pilot — module downloads

This folder is generated by \`clinical-copilot/landing/build.sh\`. Do not edit
files here by hand; they will be overwritten on the next build.

Current build:

| File | Purpose |
|---|---|
| \`${ZIP_BASENAME}\` | Versioned module zip, ready to drop into OpenEMR's \`interface/modules/custom_modules/\` |
| \`${ZIP_BASENAME}.sha256\` | SHA-256 of the versioned zip |
| \`${LATEST_ZIP}\` | Stable alias pointing at the latest build |
| \`${LATEST_ZIP}.sha256\` | SHA-256 of the latest alias |

The sales site (\`install.html\`) links to the \`latest\` alias so the page
does not need editing per release. The \`-${VERSION}\` archive is kept
alongside it for reproducibility.
EOF

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
echo "[build] done."
echo "  version: ${VERSION}"
echo "  zip:     ${ZIP_OUT}"
echo "  sha256:  ${SHA_HEX}"
echo "  latest:  ${DOWNLOADS_DIR}/${LATEST_ZIP}"
