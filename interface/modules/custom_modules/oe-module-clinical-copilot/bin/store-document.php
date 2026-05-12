<?php

/**
 * CLI helper: store a clinical document into OpenEMR's documents store
 * for a given patient.
 *
 * Used by the Clinical Co-Pilot sidecar's chat-side PDF upload flow
 * (sidecar/api/documents.py). The sidecar accepts the multipart upload
 * from the chat UI, validates the launch token + patient context, and
 * then invokes this script via `docker exec` so the file lands in the
 * patient's profile (visible from OpenEMR's Documents tab).
 *
 * Why a CLI helper instead of a REST call:
 *
 *   - OpenEMR's FHIR DocumentReference endpoint advertises `create` in
 *     the CapabilityStatement but the controller has no POST handler;
 *     the route returns 404 / "Route not found" with any token.
 *
 *   - OpenEMR's standard REST API at `/api/patient/{pid}/document`
 *     does accept POST, but it requires a user-context token (the ACL
 *     check is `patients/docs/write` against the OpenEMR user the
 *     token was issued to), which the sidecar's client_credentials
 *     flow can't produce. Switching grants is a much bigger refactor.
 *
 *   - This CLI uses OpenEMR's `\Document::createDocument` directly,
 *     which is the same code path the API + the Documents UI use.
 *     Storage, encryption, hashing, and DB rows are correct because
 *     OpenEMR did the work.
 *
 * Usage (from the sidecar host):
 *
 *   docker compose -f docker/development-easy/docker-compose.yml \
 *     exec -T openemr php \
 *       /var/www/localhost/htdocs/openemr/interface/clinical_copilot/cli-store-document.php \
 *       --pid=87413 \
 *       --category=Lab\ Report \
 *       --filename=hba1c_basic.pdf \
 *       --mime=application/pdf \
 *       --bytes-base64=<base64-encoded PDF bytes>
 *
 * Stdout on success: a single line of JSON
 *   {"document_id": <int>, "filename": "<name>", "size": <bytes>}
 *
 * Stderr + non-zero exit on failure with a specific cause line so the
 * sidecar's log explains what to fix without rummaging.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

if (php_sapi_name() !== 'cli') {
    // STDERR is only defined in CLI context. Using fwrite(STDERR, ...)
    // here throws "Undefined constant STDERR" which makes Apache return
    // a misleading 500 instead of the 403 the user actually deserves.
    // The HTTP-side substitute is `store-document.php`; we point at it
    // explicitly so a misaddressed sidecar surfaces a clear redirect
    // rather than the confusing "method not allowed" of a fatal.
    http_response_code(403);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode([
        'error' => 'cli_only',
        'message' => 'cli-store-document.php is invokable only as a CLI '
            . 'script. For HTTP, POST to store-document.php in the same '
            . 'directory (validates X-Copilot-Token shared secret).',
    ]) . "\n";
    exit(1);
}

// globals.php expects a populated $_SERVER (HTTP_HOST, REQUEST_URI,
// etc.) and a positive site identifier — none of which exist in a
// CLI context. We synthesize the minimum it needs so the bootstrap
// resolves and we get the database connection + autoloader.
$_SERVER['HTTP_HOST']     = $_SERVER['HTTP_HOST']     ?? 'localhost';
$_SERVER['SERVER_NAME']   = $_SERVER['SERVER_NAME']   ?? 'localhost';
$_SERVER['SERVER_PORT']   = $_SERVER['SERVER_PORT']   ?? '443';
$_SERVER['REQUEST_URI']   = $_SERVER['REQUEST_URI']   ?? '/interface/clinical_copilot/cli-store-document.php';
$_SERVER['SCRIPT_NAME']   = $_SERVER['SCRIPT_NAME']   ?? '/interface/clinical_copilot/cli-store-document.php';
$_SERVER['REQUEST_METHOD'] = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$_SERVER['HTTPS']         = $_SERVER['HTTPS']         ?? 'on';
$_GET['site']             = $_GET['site']             ?? 'default';

// Bypass session requirement for CLI: globals.php expects a session by
// default; we set this flag so the bootstrap skips that branch.
$ignoreAuth = true;

require_once __DIR__ . '/../../../../../globals.php';
require_once __DIR__ . '/../src/Internal/_store_document_impl.php';

function _bail(string $code, string $message, int $exitCode = 2): never
{
    fwrite(STDERR, "[cli-store-document] $code: $message\n");
    fwrite(STDOUT, json_encode(['error' => $code, 'message' => $message]) . "\n");
    exit($exitCode);
}

// Parse CLI args. Each is `--key=value`.
$opts = [];
foreach (array_slice($argv ?? [], 1) as $arg) {
    if (!str_starts_with($arg, '--')) {
        _bail('bad_arg', "Argument '$arg' is not in --key=value form.");
    }
    $eq = strpos($arg, '=');
    if ($eq === false) {
        _bail('bad_arg', "Argument '$arg' is missing '='.");
    }
    $opts[substr($arg, 2, $eq - 2)] = substr($arg, $eq + 1);
}

$pid       = (int) ($opts['pid'] ?? 0);
$category  = (string) ($opts['category'] ?? 'Lab Report');
$filename  = (string) ($opts['filename'] ?? '');
$mime      = (string) ($opts['mime'] ?? 'application/pdf');
$bytesB64  = (string) ($opts['bytes-base64'] ?? '');

if ($bytesB64 === '') {
    _bail('missing_bytes', "Pass --bytes-base64=<base64-encoded-bytes>.");
}

$bytes = base64_decode($bytesB64, true);
if ($bytes === false || $bytes === '') {
    _bail('bad_base64', "--bytes-base64 did not decode to non-empty bytes.");
}

$result = store_document_impl(
    pid: $pid,
    category: $category,
    filename: $filename,
    mime: $mime,
    bytes: $bytes,
);

if (!$result->ok) {
    // CLI mode: exit 2 on any failure with the kebab/snake code in
    // stderr so the docker-exec caller can grep it out.
    _bail($result->code, $result->message);
}

fwrite(STDOUT, json_encode($result->result) . "\n");
exit(0);
