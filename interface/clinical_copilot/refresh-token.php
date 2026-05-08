<?php

/**
 * Clinical Co-Pilot task-token refresh endpoint.
 *
 * Used by the Co-Pilot UI when the in-flight task token expires
 * mid-conversation. The chat (chat_w2.html) and Co-Pilot home
 * (chat.html) detect 401 ``token_expired`` from the sidecar, navigate
 * the tab here, and we redirect them back to the same surface with a
 * freshly minted token in the URL fragment.
 *
 * Why a separate endpoint instead of reusing launch.php:
 *
 *   - launch.php requires a CSRF token. The CSRF token only lives in
 *     OpenEMR's session and is not reachable from the Co-Pilot tab
 *     (different origin, sessionStorage, etc.). The original click
 *     from demographics.php has the token because the link is rendered
 *     server-side; a refresh-from-the-browser does not.
 *
 *   - Skipping CSRF is defensible for this specific endpoint because:
 *
 *       1. It's a top-level GET that mints a token bound to the
 *          *currently authenticated* OpenEMR user (we read it from
 *          the session). A cross-site forgery wouldn't be able to
 *          read the resulting fragment-encoded token (browsers don't
 *          send fragments to servers, and the JS that reads the
 *          fragment is same-origin to the dashboard).
 *
 *       2. The minted token is itself short-lived (5 min) and bound
 *          to a specific user/patient/purpose set, so even if a CSRF
 *          attacker COULD trick a clinician into minting one, they
 *          can't read it.
 *
 *       3. The returned token is delivered only via the URL fragment
 *          (the ``token=...`` portion of ``#``), which never reaches
 *          server logs and never crosses origins.
 *
 *   - The endpoint requires:
 *       - an authenticated OpenEMR session (CORE_SESSION_ID cookie
 *         sent on top-level navigation),
 *       - the ``patient_uuid`` query param (FHIR-style; the Co-Pilot
 *         UI has this in PATIENT_ID; the chat doesn't have ``pid``),
 *       - the same ACL grant as launch.php (``patients/demo``).
 *
 * Inputs:
 *   ?patient_uuid=<dashed-fhir-uuid>     required
 *   ?return_to=<absolute-url-on-copilot-base>  required, must be
 *                                              http://localhost:8801
 *                                              (or whatever
 *                                              `clinical_copilot_url`
 *                                              global is set to).
 *                                              We anchor on the
 *                                              configured base to
 *                                              avoid an open redirect.
 *
 * On success:
 *   302 -> ``<return_to>#token=<jwt>&patient=Patient/<uuid>&purpose=document_ingest[&theme=modern]``
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
use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\ClinicalCoPilot\TaskTokenConfigurationError;
use OpenEMR\ClinicalCoPilot\TaskTokenMinter;

$logger = new SystemLogger();

// 1. Authentication. globals.php already bounces unauthenticated
//    requests to the login page, but we double-check authUserID so
//    the rest of the script never operates on an empty username.
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
        'clinical_copilot.refresh_token.user_lookup_failed',
        ['authUserID' => $authUserID]
    );
    http_response_code(500);
    echo xlt('Could not resolve username for authenticated user.');
    exit;
}

// 2. ACL: same as launch.php. A clinician who can no longer read this
//    chart shouldn't be able to refresh a token that lets them.
if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    echo xlt('You do not have permission to use the Clinical Co-Pilot for this patient.');
    exit;
}

// 3. Inputs.
$patientUuidString = trim((string) ($_GET['patient_uuid'] ?? ''));
$returnTo = trim((string) ($_GET['return_to'] ?? ''));

if ($patientUuidString === '') {
    http_response_code(400);
    echo xlt('Missing patient_uuid.');
    exit;
}

// Loose UUID-ish shape check — defends against stray path traversal /
// header injection more than against an attacker-controlled UUID,
// which is fine because the sqlQuery below uses parameter binding.
if (!preg_match('/^[0-9a-fA-F-]{8,64}$/', $patientUuidString)) {
    http_response_code(400);
    echo xlt('patient_uuid does not look like a UUID.');
    exit;
}

// 4. Resolve the patient. Reject any uuid that doesn't correspond to
//    a real patient_data row — otherwise the minted token would
//    authorise access to a nonexistent record.
$patientUuidBin = UuidRegistry::uuidToBytes($patientUuidString);
$row = sqlQuery('SELECT pid FROM patient_data WHERE uuid = ?', [$patientUuidBin]);
if (empty($row['pid'])) {
    http_response_code(404);
    echo xlt('No patient with that uuid.');
    exit;
}
$patientId = 'Patient/' . $patientUuidString;

// 5. return_to validation. Anchor on the configured Co-Pilot base
//    URL so this endpoint can't be coerced into an open-redirect
//    primitive (e.g. ?return_to=https://attacker.example/).
$copilotBase = rtrim((string) ($GLOBALS['clinical_copilot_url'] ?? ''), '/');
if ($copilotBase === '') {
    http_response_code(503);
    echo xlt('Clinical Co-Pilot URL is not configured. Set the "Clinical Co-Pilot Sidecar URL" global.');
    exit;
}
if ($returnTo === '') {
    // Default to the Co-Pilot home. Both chat.html and chat_w2.html
    // can recover from there.
    $returnTo = $copilotBase . '/';
}
// Strict same-origin check on the return URL: scheme + host + port
// must equal the configured copilotBase. We don't allow path-only
// return_to values (no relative URLs) — the resolved $returnTo must
// be absolute and match the base prefix exactly.
if (strpos($returnTo, $copilotBase) !== 0) {
    $logger->warning(
        'clinical_copilot.refresh_token.return_to_rejected',
        ['return_to' => $returnTo, 'copilotBase' => $copilotBase]
    );
    http_response_code(400);
    echo xlt('return_to must point at the configured Clinical Co-Pilot base URL.');
    exit;
}

// 6. Mint the token. Same authorised purposes as launch.php — the
//    token is fungible across diagnostic / chart-error / follow-up /
//    document_ingest panels.
$signingKey = (string) ($GLOBALS['clinical_copilot_jwt_signing_key'] ?? '');
$authorizedPurposes = [
    'diagnostic_cross_check',
    'chart_error_scan',
    'follow_up_question',
    'document_ingest',
];

try {
    $minter = new TaskTokenMinter($signingKey);
} catch (TaskTokenConfigurationError $exc) {
    $logger->error('clinical_copilot.refresh_token.unconfigured', ['error' => $exc->getMessage()]);
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
    $logger->error('clinical_copilot.refresh_token.mint_failed', ['error' => $exc->getMessage()]);
    http_response_code(500);
    echo xlt('Failed to mint Clinical Co-Pilot task token.');
    exit;
}

$logger->info(
    'clinical_copilot.refresh_token.ok',
    [
        'user_id' => $userId,
        'pid' => (int) $row['pid'],
        'return_to' => $returnTo,
    ]
);

// 7. Redirect with the new token in the URL fragment. Same shape as
//    launch.php so the chat / Co-Pilot UIs can re-use their existing
//    fragment-parsing code without a second code path.
$fragmentParams = [
    'token' => $token,
    'patient' => $patientId,
    'purpose' => 'document_ingest',
];
if (trim((string) ($GLOBALS['patient_dashboard_modern_url'] ?? '')) !== '') {
    $fragmentParams['theme'] = 'modern';
}

// Append the fragment to the return URL. If return_to already had a
// fragment we discard it — the freshly minted token is the only
// fragment state that matters now.
[$returnUrlClean] = explode('#', $returnTo, 2);
$redirect = $returnUrlClean . '#' . http_build_query($fragmentParams);

header('Location: ' . $redirect, true, 302);
exit;
