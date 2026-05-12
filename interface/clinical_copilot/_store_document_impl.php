<?php

/**
 * Shared impl: store a clinical document into OpenEMR's documents store
 * for a given patient.
 *
 * Called by both `cli-store-document.php` (docker exec, local dev) and
 * `store-document.php` (HTTP, prod). One code path so failure modes,
 * argument validation, and JSON shape stay in sync.
 *
 * Both callers MUST `require_once` `globals.php` (with the appropriate
 * $ignoreAuth / $_SERVER synthesis) BEFORE requiring this file — this
 * impl relies on the OpenEMR autoloader, sqlQuery(), and \Document.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use OpenEMR\Common\Logging\SystemLogger;

/**
 * Result envelope for store_document_impl().
 *
 * `ok=true`  → the document landed in OpenEMR. `result` has the JSON the
 *              callers should print/return.
 * `ok=false` → something went wrong. `code` is a short kebab/snake
 *              identifier the callers can log; `message` is the human-
 *              readable cause; `http_status` is the HTTP status the
 *              HTTP-mode caller should return. CLI mode just exits with
 *              code 2.
 *
 * Designed as a value object so the callers can dispatch consistently
 * regardless of transport (CLI exit-code+stderr vs HTTP status+JSON).
 */
final readonly class StoreDocumentResult
{
    /**
     * @param array<string, mixed> $result
     */
    public function __construct(
        public bool $ok,
        public array $result,
        public string $code = '',
        public string $message = '',
        public int $httpStatus = 0,
    ) {
    }

    /**
     * @param array<string, mixed> $payload
     */
    public static function success(array $payload): self
    {
        return new self(ok: true, result: $payload);
    }

    public static function failure(
        string $code,
        string $message,
        int $httpStatus,
    ): self {
        return new self(
            ok: false,
            result: ['error' => $code, 'message' => $message],
            code: $code,
            message: $message,
            httpStatus: $httpStatus,
        );
    }
}

/**
 * Resolve the legacy numeric pid for a FHIR uuid (dashed string form).
 *
 * Returns the pid as int, or null if no patient_data row has that uuid.
 *
 * The `patient_data.uuid` column stores the binary form (16 bytes); the
 * FHIR uuid is the dashed string form. We hex-decode the dashed form on
 * the SQL side so the comparison hits the binary column directly.
 */
function resolve_pid_from_fhir_uuid(string $fhirUuid): ?int
{
    // Validate the FHIR uuid shape up-front so a malformed value doesn't
    // produce a confusing "no_such_patient". Standard UUID is 36 chars
    // with dashes at positions 8, 13, 18, 23.
    if (
        !preg_match(
            '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i',
            $fhirUuid,
        )
    ) {
        return null;
    }

    $row = sqlQuery(
        'SELECT pid FROM patient_data '
            . "WHERE uuid = UNHEX(REPLACE(?, '-', '')) LIMIT 1",
        [$fhirUuid],
    );
    if (empty($row) || !isset($row['pid'])) {
        return null;
    }
    $pid = (int) $row['pid'];

    return $pid > 0 ? $pid : null;
}

/**
 * Store a document into OpenEMR's documents store.
 *
 * Returns a {@see StoreDocumentResult} explaining outcome. Never throws
 * on caller-visible errors (validation, missing patient, etc.) — those
 * are returned as failures so the transport layer can present them
 * uniformly. Throws only on genuinely unexpected internal failures.
 *
 * @param int    $pid       Legacy numeric patient pid (patient_data.pid).
 * @param string $category  Document category name (e.g. "Lab Report").
 *                          Must already exist in the `categories` table.
 * @param string $filename  Display filename; OpenEMR will deduplicate.
 * @param string $mime      MIME type, e.g. "application/pdf".
 * @param string $bytes     Raw document bytes (NOT base64).
 */
function store_document_impl(
    int $pid,
    string $category,
    string $filename,
    string $mime,
    string $bytes,
): StoreDocumentResult {
    if ($pid <= 0) {
        return StoreDocumentResult::failure(
            'missing_pid',
            "Pass pid as positive int (got $pid).",
            400,
        );
    }
    if ($filename === '') {
        return StoreDocumentResult::failure(
            'missing_filename',
            "Pass a non-empty filename.",
            400,
        );
    }
    if ($bytes === '') {
        return StoreDocumentResult::failure(
            'missing_bytes',
            "Pass non-empty document bytes.",
            400,
        );
    }

    // Verify the patient exists. \Document::createDocument silently writes
    // the file even for nonexistent pids, leaving an orphan row, so check
    // up-front and return a clear cause.
    $patientRow = sqlQuery(
        'SELECT pid, fname, lname FROM patient_data WHERE pid = ?',
        [$pid],
    );
    if (empty($patientRow)) {
        return StoreDocumentResult::failure(
            'no_such_patient',
            "patient_data has no row with pid=$pid.",
            404,
        );
    }

    // Resolve the category id by name. The standard categories are
    // seeded by OpenEMR (Lab Report id=2, Patient Information id=1,
    // etc.), but custom installs may not have them — fail loudly rather
    // than silently write to "uncategorized".
    $categoryRow = sqlQuery(
        'SELECT id FROM categories WHERE name = ?',
        [$category],
    );
    if (empty($categoryRow)) {
        return StoreDocumentResult::failure(
            'no_such_category',
            "categories has no row with name=" . var_export($category, true)
                . ". Either pass an existing category or create it via "
                . "Admin -> Forms -> Layouts -> Document Categories.",
            422,
        );
    }
    $categoryId = (int) $categoryRow['id'];

    $logger = new SystemLogger();

    try {
        $doc = new \Document();
        // createDocument signature:
        //   createDocument(pid, categoryId, filename, mimetype, fileBytes, ...)
        // returns '' on success, an error-message string otherwise.
        $err = $doc->createDocument(
            $pid,
            $categoryId,
            $filename,
            $mime,
            $bytes,
        );
    } catch (\Throwable $exc) {
        $logger->error('store_document_impl.create_threw', [
            'pid' => $pid,
            'category' => $category,
            'filename' => $filename,
            'error' => $exc->getMessage(),
        ]);
        return StoreDocumentResult::failure(
            'create_threw',
            "\\Document::createDocument threw: " . $exc->getMessage(),
            500,
        );
    }

    if (!empty($err)) {
        // createDocument returns a non-empty string on failure. Surface
        // OpenEMR's exact message so the caller's log explains the cause
        // immediately without rummaging through the openemr error log.
        return StoreDocumentResult::failure(
            'create_failed',
            "\\Document::createDocument returned non-empty: " . $err,
            500,
        );
    }

    $documentId = (int) $doc->get_id();
    if ($documentId <= 0) {
        return StoreDocumentResult::failure(
            'no_id',
            "\\Document::createDocument succeeded but get_id() is "
                . "non-positive ($documentId). This is a bug in OpenEMR's "
                . "Document class or the categories table is corrupt; "
                . "check categories_seq and the row OpenEMR just wrote.",
            500,
        );
    }

    $logger->info('store_document_impl.ok', [
        'pid' => $pid,
        'document_id' => $documentId,
        'filename' => $filename,
        'size' => strlen($bytes),
    ]);

    return StoreDocumentResult::success([
        'document_id' => $documentId,
        'filename' => $filename,
        'size' => strlen($bytes),
        'category' => $category,
        'category_id' => $categoryId,
        'patient_pid' => $pid,
    ]);
}
