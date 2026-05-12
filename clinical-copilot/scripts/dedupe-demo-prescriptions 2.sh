#!/usr/bin/env bash
#
# Idempotent fix for duplicate prescriptions on the demo patients.
#
# Background:
#   The Clinical Co-Pilot demo seed (Barbara/Suzie/penicillin demo) at
#   some point inserted each prescription row twice — same drug, same
#   dosage, same date_added, same patient. The legacy PHP dashboard
#   silently de-duped on display, but the FHIR MedicationRequest mapper
#   emits one resource per row, so the modern dashboard at
#   patient-dashboard/ surfaces every duplicate. There is no clinical
#   reason for the duplicates (refills are tracked elsewhere).
#
# Fix:
#   DELETE every prescription row for the demo patients whose
#   (drug, dosage, date_added) tuple matches a lower-id row. A self-join
#   makes this idempotent: re-running on a clean table is a no-op.
#
# Usage:
#   bash clinical-copilot/scripts/dedupe-demo-prescriptions.sh
#
# Safe on:
#   - the local docker/development-easy stack
#   - the Hetzner deployment (after `ssh root@5.161.253.237`)
#
# Failure modes are surfaced explicitly. If the OpenEMR container is not
# running, the script prints a precise message and exits non-zero.

set -euo pipefail

CONTAINER="${OPENEMR_CONTAINER:-development-easy-openemr-1}"
DB_USER="${OPENEMR_DB_USER:-openemr}"
DB_PASS="${OPENEMR_DB_PASS:-openemr}"
DB_NAME="${OPENEMR_DB_NAME:-openemr}"
DB_HOST="${OPENEMR_DB_HOST:-mysql}"

DEMO_PIDS="${DEMO_PIDS:-87413,87414,87415}"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}\$"; then
  echo "ERROR: OpenEMR container '${CONTAINER}' is not running." >&2
  echo "  - Local dev: cd docker/development-easy && docker compose up -d" >&2
  echo "  - Hetzner:   the auto-deploy cron should keep it up; check 'docker ps'." >&2
  echo "  - Override the container name with OPENEMR_CONTAINER=<name>." >&2
  exit 1
fi

echo "→ Looking for duplicate prescriptions on demo pids (${DEMO_PIDS})…"

BEFORE=$(docker exec "${CONTAINER}" mariadb -h "${DB_HOST}" -u"${DB_USER}" -p"${DB_PASS}" -D"${DB_NAME}" -N -B -e "
SELECT COUNT(*) FROM prescriptions WHERE patient_id IN (${DEMO_PIDS});
")
echo "  before: ${BEFORE} prescriptions for demo patients"

docker exec "${CONTAINER}" mariadb -h "${DB_HOST}" -u"${DB_USER}" -p"${DB_PASS}" -D"${DB_NAME}" -e "
DELETE p1 FROM prescriptions p1
INNER JOIN prescriptions p2
  ON p1.patient_id = p2.patient_id
  AND p1.drug = p2.drug
  AND p1.dosage = p2.dosage
  AND p1.date_added = p2.date_added
  AND p1.id > p2.id
WHERE p1.patient_id IN (${DEMO_PIDS});
"

AFTER=$(docker exec "${CONTAINER}" mariadb -h "${DB_HOST}" -u"${DB_USER}" -p"${DB_PASS}" -D"${DB_NAME}" -N -B -e "
SELECT COUNT(*) FROM prescriptions WHERE patient_id IN (${DEMO_PIDS});
")
echo "  after:  ${AFTER} prescriptions for demo patients"

REMOVED=$((BEFORE - AFTER))
if [ "${REMOVED}" -gt 0 ]; then
  echo "✓ Removed ${REMOVED} duplicate row(s)."
else
  echo "✓ No duplicates found — table was already clean (script is idempotent)."
fi
