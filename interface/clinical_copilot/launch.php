<?php

/**
 * Clinical Co-Pilot launch endpoint.
 *
 * The patient-summary launch button ($newPatient → demographics.php)
 * redirects here with ?pid=…&purpose=…. We:
 *
 *   1. require an authenticated OpenEMR session (the OpenEMR bootstrap
 *      in globals.php does this; unauthenticated requests are bounced to
 *      the login page);
 *   2. ACL-check that this user can read this patient's chart;
 *   3. resolve the patient's FHIR UUID;
 *   4. mint a 5-minute HS256 task token bound to (user, patient, purpose);
 *   5. 302-redirect to the sidecar URL with the token in the URL fragment
 *      (fragments are not sent to the sidecar in the initial GET, so they
 *      never appear in HTTP access logs, but the chat UI's JavaScript can
 *      read window.location.hash to extract the token for fetch() calls).
 *
 * This replaces the previous "direct unauthenticated link to the sidecar"
 * pattern used during demo development.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once __DIR__ . '/../globals.php';

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\ClinicalCoPilot\TaskTokenConfigurationError;
use OpenEMR\ClinicalCoPilot\TaskTokenMinter;

$logger = new SystemLogger();

// 1. Authentication. OpenEMR's globals.php bootstraps the session and
//    stores the authenticated user id under 'authUserID' on the
//    Symfony session bag. We read it via the SessionWrapperFactory
//    (the canonical pattern across the codebase) and resolve the
//    username from the users table to embed it in the JWT subject.
$session = SessionWrapperFactory::getInstance()->getActiveSession();
$authUserID = $session->get('authUserID');
if (!$authUserID) {
    http_response_code(401);
    echo xlt('Not authenticated.');
    exit;
}
$userRow = sqlQuery('SELECT username FROM users WHERE id = ?', [(int) $authUserID]);
$userId = (string) ($userRow['username'] ?? '');
if ($userId === '') {
    $logger->error(
        'clinical_copilot.launch.user_lookup_failed',
        ['authUserID' => $authUserID]
    );
    http_response_code(500);
    echo xlt('Could not resolve username for authenticated user.');
    exit;
}

// 2. ACL: the launch button reuses the same permission as the patient
//    summary page itself ("patients"/"demo" — view the demographics
//    panel). Fail closed if the user lacks it.
if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    echo xlt('You do not have permission to use the Clinical Co-Pilot for this patient.');
    exit;
}

// 3. Inputs. CSRF protection is mandatory because the demographics page
//    builds the launch URL with a token; a missing/invalid token means
//    the link was forged or the session expired.
$pid = isset($_GET['pid']) ? (int) $_GET['pid'] : 0;
$purpose = (string) ($_GET['purpose'] ?? 'diagnostic_cross_check');
$csrfToken = (string) ($_GET['csrf_token'] ?? '');

if ($pid <= 0) {
    http_response_code(400);
    echo xlt('Missing or invalid patient id.');
    exit;
}

if (!CsrfUtils::verifyCsrfToken($csrfToken, $session)) {
    $logger->warning(
        'clinical_copilot.launch.csrf_failed',
        ['pid' => $pid, 'user_id' => $userId]
    );
    http_response_code(403);
    echo xlt('CSRF check failed; reload the patient summary and try again.');
    exit;
}

// The UI fans out one /chat call per purpose from a single launch
// click — diagnostic cross-check, chart-error scan, and follow-up
// questions all run from the same page. Bind the token to the full
// authorised set so a single launch covers every panel; the sidecar
// enforces membership per call and the audit log records the
// per-call exercised purpose.
$allowedPurposes = [
    'diagnostic_cross_check',
    'chart_error_scan',
    'follow_up_question',
];
if (!in_array($purpose, $allowedPurposes, true)) {
    http_response_code(400);
    echo xlt('Unknown purpose:') . ' ' . text($purpose);
    exit;
}
// The ?purpose= query parameter is preserved in the URL fragment so
// the chat UI can highlight the originally-requested panel, but the
// minted token authorises EVERY purpose in $allowedPurposes.
$authorizedPurposes = $allowedPurposes;

// 4. Resolve the FHIR Patient resource id. We prefer the UUID; we never
//    leak the legacy numeric pid into the sidecar URL because the sidecar
//    treats patient_id as the FHIR resource id and would fail to fetch.
$row = sqlQuery('SELECT uuid FROM patient_data WHERE pid = ?', [$pid]);
$uuid = $row['uuid'] ?? null;
if (!$uuid) {
    http_response_code(404);
    echo xlt('No FHIR UUID for this patient. Run the UUID backfill before using the Clinical Co-Pilot.');
    exit;
}
$patientId = 'Patient/' . UuidRegistry::uuidToString($uuid);

// 5. Mint the token. Configuration errors get a precise admin-facing
//    message rather than a generic 500.
$signingKey = (string) ($GLOBALS['clinical_copilot_jwt_signing_key'] ?? '');
$copilotBase = rtrim((string) ($GLOBALS['clinical_copilot_url'] ?? ''), '/');

if ($copilotBase === '') {
    http_response_code(503);
    echo xlt('Clinical Co-Pilot URL is not configured. Set the "Clinical Co-Pilot Sidecar URL" global.');
    exit;
}

try {
    $minter = new TaskTokenMinter($signingKey);
} catch (TaskTokenConfigurationError $exc) {
    $logger->error('clinical_copilot.launch.unconfigured', ['error' => $exc->getMessage()]);
    http_response_code(503);
    echo xlt('Clinical Co-Pilot is not configured:') . ' ' . text($exc->getMessage());
    exit;
}

try {
    $token = $minter->mint(
        userId: $userId,
        patientId: $patientId,
        purposesOfUse: $authorizedPurposes,
    );
} catch (\Throwable $exc) {
    $logger->error('clinical_copilot.launch.mint_failed', ['error' => $exc->getMessage()]);
    http_response_code(500);
    echo xlt('Failed to mint Clinical Co-Pilot task token.');
    exit;
}

$logger->info(
    'clinical_copilot.launch.ok',
    [
        'user_id' => $userId,
        'pid' => $pid,
        'requested_purpose' => $purpose,
        'authorized_purposes' => $authorizedPurposes,
    ]
);

// 6. Redirect with token + patient + purpose in the URL fragment. The
//    fragment is not sent to the sidecar in the initial GET, so the
//    sidecar's HTTP access log never sees the token. The chat UI's JS
//    extracts it from window.location.hash.
$params = http_build_query([
    'token' => $token,
    'patient' => $patientId,
    'purpose' => $purpose,
]);
$redirect = $copilotBase . '/#' . $params;

header('Location: ' . $redirect, true, 302);
exit;
