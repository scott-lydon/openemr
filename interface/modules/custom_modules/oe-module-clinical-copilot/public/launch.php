<?php

/**
 * Clinical Co-Pilot launch endpoint (module-scoped).
 *
 * Moved from interface/clinical_copilot/launch.php into the module's
 * public/ directory so a vanilla OpenEMR install can ship the file
 * without a manual copy. The patient-summary listener
 * (PatientSummaryRenderListener) renders a link to this script.
 *
 * Flow:
 *
 *   1. Require an authenticated OpenEMR session (the OpenEMR bootstrap
 *      in globals.php bounces unauthenticated requests to the login
 *      page).
 *   2. ACL-check that this user can read this patient's chart.
 *   3. Resolve the patient's FHIR UUID.
 *   4. Mint a 5-minute HS256 task token bound to (user, patient,
 *      purposes).
 *   5. 302-redirect to the sidecar URL with the token in the URL
 *      fragment.
 *
 * Settings are read from {@see ModuleSettings} (the module's private
 * settings table), NOT from $GLOBALS, so the install can be uninstalled
 * cleanly without leaving stale rows in library/globals.inc.php.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 *
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once __DIR__ . '/../../../../globals.php';

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Modules\ClinicalCoPilot\ModuleSettings;
use OpenEMR\Modules\ClinicalCoPilot\TaskTokenConfigurationError;
use OpenEMR\Modules\ClinicalCoPilot\TaskTokenMinter;

$logger = new SystemLogger();
$settings = new ModuleSettings();

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

if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    echo xlt('You do not have permission to use the Clinical Co-Pilot for this patient.');
    exit;
}

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

$allowedPurposes = $settings->getList(ModuleSettings::KEY_PURPOSE_ALLOWLIST);
if ($allowedPurposes === []) {
    $logger->error('clinical_copilot.launch.purpose_allowlist_empty');
    http_response_code(503);
    echo xlt('Clinical Co-Pilot purpose allow list is empty. Open the module admin page and configure at least one purpose.');
    exit;
}
if (!in_array($purpose, $allowedPurposes, true)) {
    http_response_code(400);
    echo xlt('Unknown purpose:') . ' ' . text($purpose);
    exit;
}
$authorizedPurposes = $allowedPurposes;

$row = sqlQuery('SELECT uuid FROM patient_data WHERE pid = ?', [$pid]);
$uuid = $row['uuid'] ?? null;
if (!$uuid) {
    http_response_code(404);
    echo xlt('No FHIR UUID for this patient. Run the UUID backfill before using the Clinical Co-Pilot.');
    exit;
}
$patientId = 'Patient/' . UuidRegistry::uuidToString($uuid);

$signingKey = $settings->getString(ModuleSettings::KEY_JWT_SIGNING_KEY);
$copilotBase = rtrim($settings->getString(ModuleSettings::KEY_SIDECAR_URL), '/');

if ($copilotBase === '') {
    http_response_code(503);
    echo xlt('Clinical Co-Pilot URL is not configured. Open the module admin page and set the Sidecar URL.');
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

$fragmentParams = [
    'token' => $token,
    'patient' => $patientId,
    'purpose' => $purpose,
];
$modernUrl = trim($settings->getString(ModuleSettings::KEY_MODERN_DASHBOARD_URL));
if ($modernUrl !== '') {
    $fragmentParams['theme'] = 'modern';
}
$params = http_build_query($fragmentParams);
$redirect = $copilotBase . '/#' . $params;

header('Location: ' . $redirect, true, 302);
exit;
