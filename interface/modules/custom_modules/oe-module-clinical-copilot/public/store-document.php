<?php

/**
 * HTTP endpoint: store a clinical document into OpenEMR's documents store.
 *
 * Used by the Clinical Co-Pilot sidecar when the sidecar runs in a
 * different container/host than OpenEMR (i.e. production deployments
 * like Hetzner where `docker exec` is unavailable). For local-dev the
 * sidecar may still use {@see cli-store-document.php} via docker exec
 * but production should use this HTTP endpoint exclusively.
 *
 * Auth model
 * ----------
 *
 * This endpoint is invoked machine-to-machine by the sidecar, not by a
 * browser session. OpenEMR's standard OAuth+ACL flow (client_credentials
 * vs user-context, `patients/docs/write` scope) cannot be satisfied
 * without a much larger refactor — see the header comment of
 * `cli-store-document.php` for the underlying constraint.
 *
 * Instead, the endpoint validates a shared secret in an
 * `X-Copilot-Token` header against the env var
 * `COPILOT_STORE_DOCUMENT_SECRET` (read inside the OpenEMR container).
 * The secret is high-entropy, deployed via the same `.env` mechanism
 * the rest of the stack uses, and rotated by re-deploying.
 *
 * Comparison is constant-time (`hash_equals`) so timing attacks cannot
 * leak the secret one byte at a time.
 *
 * The endpoint refuses to start if the secret is missing or empty in
 * the environment — it would otherwise accept anonymous writes.
 *
 * Request shape
 * -------------
 *
 *   POST /interface/clinical_copilot/store-document.php
 *   Headers:
 *     X-Copilot-Token: <secret>
 *     Content-Type: application/json
 *   Body (JSON) — provide EITHER `pid` OR `patient_fhir_uuid`:
 *     {
 *       "pid":               <int>,           // legacy patient_data.pid; or
 *       "patient_fhir_uuid": "<uuid>",        // FHIR uuid (dashed form)
 *       "category":          "Lab Report",    // existing category name
 *       "filename":          "hba1c_basic.pdf",
 *       "mime":              "application/pdf",
 *       "bytes_base64":      "<base64 of file>"
 *     }
 *
 *   When both are provided, `pid` wins. The sidecar usually passes
 *   `patient_fhir_uuid` because that is the form the SMART launch token
 *   carries — letting PHP resolve to pid avoids a second round trip.
 *
 * Response shape
 * --------------
 *
 *   200 OK  application/json
 *     { "document_id": <int>, "filename": "…", "size": <bytes>, … }
 *
 *   4xx/5xx application/json
 *     { "error": "<kebab_code>", "message": "<human cause>" }
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// Set $ignoreAuth BEFORE requiring globals.php so OpenEMR's bootstrap
// skips the session+cookie auth check. The shared-secret check below
// replaces it.
$ignoreAuth = true;

// OpenEMR's globals.php reads $_GET['site'] to pick which site config to
// load; default to "default" if missing so the bootstrap resolves.
$_GET['site'] = $_GET['site'] ?? 'default';

require_once __DIR__ . '/../../../../../globals.php';
require_once __DIR__ . '/../src/Internal/_store_document_impl.php';

/**
 * Emit a JSON error response and exit. Used for every non-success path.
 */
function _emit_error(string $code, string $message, int $httpStatus): never
{
    http_response_code($httpStatus);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(['error' => $code, 'message' => $message]) . "\n";
    error_log("[store-document.php] $code ($httpStatus): $message");
    exit;
}

// ─── method check ─────────────────────────────────────────────────────
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    _emit_error(
        'method_not_allowed',
        'Only POST is accepted. Send the document as a JSON body with '
            . 'fields {pid, category, filename, mime, bytes_base64}.',
        405,
    );
}

// ─── shared-secret auth ──────────────────────────────────────────────
$expectedSecret = getenv('COPILOT_STORE_DOCUMENT_SECRET');
if (!is_string($expectedSecret) || $expectedSecret === '') {
    // Fail closed: never accept an upload if the secret isn't
    // configured. Surface the cause loudly in the response so the
    // sidecar log + ops can fix it instead of silently dropping
    // documents.
    _emit_error(
        'secret_not_configured',
        'COPILOT_STORE_DOCUMENT_SECRET is missing/empty in the OpenEMR '
            . "container's environment. Set it in the openemr service's "
            . '`environment:` block of docker-compose.yml (or .env), '
            . 'and set the matching COPILOT_STORE_DOCUMENT_SECRET in the '
            . "sidecar's environment. Without it this endpoint would "
            . 'accept anonymous writes, so it refuses to run.',
        503,
    );
}

$providedSecret = $_SERVER['HTTP_X_COPILOT_TOKEN'] ?? '';
if (!is_string($providedSecret) || $providedSecret === '') {
    _emit_error(
        'missing_auth',
        'Required header `X-Copilot-Token` is missing or empty.',
        401,
    );
}
if (!hash_equals($expectedSecret, $providedSecret)) {
    _emit_error(
        'bad_auth',
        'X-Copilot-Token does not match COPILOT_STORE_DOCUMENT_SECRET. '
            . 'Confirm both sides read the same secret value (sidecar '
            . '.env vs OpenEMR container env). The comparison is '
            . 'constant-time so length mismatches register as bad_auth, '
            . 'not as a different error.',
        401,
    );
}

// ─── body parse ──────────────────────────────────────────────────────
$rawBody = file_get_contents('php://input');
if ($rawBody === false || $rawBody === '') {
    _emit_error(
        'empty_body',
        'Request body is empty. Send a JSON object with '
            . '{pid, category, filename, mime, bytes_base64}.',
        400,
    );
}

try {
    $payload = json_decode($rawBody, true, 32, JSON_THROW_ON_ERROR);
} catch (\JsonException $exc) {
    _emit_error(
        'bad_json',
        'Request body is not valid JSON: ' . $exc->getMessage(),
        400,
    );
}
if (!is_array($payload)) {
    _emit_error(
        'bad_json',
        'Request body must decode to a JSON object, not an array or scalar.',
        400,
    );
}

// Accept `pid` directly, OR `patient_fhir_uuid` and resolve to pid in
// PHP. The sidecar usually has the FHIR uuid (from the SMART launch
// token) and used to shell out to `docker exec mysql` to translate;
// doing the lookup here removes that second docker-exec path.
$pid = null;
$rawPid = $payload['pid'] ?? null;
if (is_int($rawPid) && $rawPid > 0) {
    $pid = $rawPid;
} elseif (is_string($rawPid) && ctype_digit($rawPid) && (int) $rawPid > 0) {
    $pid = (int) $rawPid;
}

if ($pid === null) {
    $fhirUuid = $payload['patient_fhir_uuid'] ?? null;
    if (is_string($fhirUuid) && $fhirUuid !== '') {
        $resolved = resolve_pid_from_fhir_uuid($fhirUuid);
        if ($resolved === null) {
            _emit_error(
                'no_such_patient',
                "patient_data has no row with uuid matching FHIR uuid "
                    . var_export($fhirUuid, true) . '. Either the patient '
                    . 'was deleted between launch and upload, or the '
                    . "uuid is malformed (expected dashed 8-4-4-4-12 hex).",
                404,
            );
        }
        $pid = $resolved;
    }
}

if ($pid === null) {
    _emit_error(
        'missing_pid',
        'Provide either `pid` (positive int) or `patient_fhir_uuid` '
            . '(dashed UUID string). Got pid=' . var_export($rawPid, true)
            . ', patient_fhir_uuid=' . var_export(
                $payload['patient_fhir_uuid'] ?? null,
                true,
            ) . '.',
        400,
    );
}

$category    = (string) ($payload['category']     ?? 'Lab Report');
$filename    = (string) ($payload['filename']     ?? '');
$mime        = (string) ($payload['mime']         ?? 'application/pdf');
$bytesBase64 = (string) ($payload['bytes_base64'] ?? '');

if ($bytesBase64 === '') {
    _emit_error(
        'missing_bytes',
        'Field `bytes_base64` is required and must be the base64-encoded '
            . 'document bytes.',
        400,
    );
}

$bytes = base64_decode($bytesBase64, true);
if ($bytes === false || $bytes === '') {
    _emit_error(
        'bad_base64',
        '`bytes_base64` did not decode to non-empty bytes. '
            . 'Confirm the sidecar base64-encodes WITHOUT URL-safe '
            . 'substitutions (use standard +/= alphabet).',
        400,
    );
}

// ─── delegate to shared impl ─────────────────────────────────────────
$result = store_document_impl(
    pid: $pid,
    category: $category,
    filename: $filename,
    mime: $mime,
    bytes: $bytes,
);

if (!$result->ok) {
    _emit_error($result->code, $result->message, $result->httpStatus);
}

http_response_code(200);
header('Content-Type: application/json; charset=utf-8');
echo json_encode($result->result) . "\n";
exit;
