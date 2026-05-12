<?php

/**
 * Clinical Co-Pilot task-token refresh endpoint (module-scoped).
 *
 * Used by the Co-Pilot UI when the in-flight task token expires
 * mid-conversation. The chat detects ``token_expired`` (HTTP 401) from
 * the sidecar, navigates the tab here, and we redirect back to the
 * caller with a freshly minted token in the URL fragment.
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
        'clinical_copilot.refresh_token.user_lookup_failed',
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

$patientUuidString = trim((string) ($_GET['patient_uuid'] ?? ''));
$returnTo = trim((string) ($_GET['return_to'] ?? ''));

if ($patientUuidString === '') {
    http_response_code(400);
    echo xlt('Missing patient_uuid.');
    exit;
}

if (!preg_match('/^[0-9a-fA-F-]{8,64}$/', $patientUuidString)) {
    http_response_code(400);
    echo xlt('patient_uuid does not look like a UUID.');
    exit;
}

$patientUuidBin = UuidRegistry::uuidToBytes($patientUuidString);
$row = sqlQuery('SELECT pid FROM patient_data WHERE uuid = ?', [$patientUuidBin]);
if (empty($row['pid'])) {
    http_response_code(404);
    echo xlt('No patient with that uuid.');
    exit;
}
$patientId = 'Patient/' . $patientUuidString;

$copilotBase = rtrim($settings->getString(ModuleSettings::KEY_SIDECAR_URL), '/');
if ($copilotBase === '') {
    http_response_code(503);
    echo xlt('Clinical Co-Pilot URL is not configured. Open the module admin page and set the Sidecar URL.');
    exit;
}
if ($returnTo === '') {
    $returnTo = $copilotBase . '/';
}
if (strpos($returnTo, $copilotBase) !== 0) {
    $logger->warning(
        'clinical_copilot.refresh_token.return_to_rejected',
        ['return_to' => $returnTo, 'copilotBase' => $copilotBase]
    );
    http_response_code(400);
    echo xlt('return_to must point at the configured Clinical Co-Pilot base URL.');
    exit;
}

$signingKey = $settings->getString(ModuleSettings::KEY_JWT_SIGNING_KEY);
$authorizedPurposes = $settings->getList(ModuleSettings::KEY_PURPOSE_ALLOWLIST);
if ($authorizedPurposes === []) {
    $logger->error('clinical_copilot.refresh_token.purpose_allowlist_empty');
    http_response_code(503);
    echo xlt('Clinical Co-Pilot purpose allow list is empty.');
    exit;
}

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

$fragmentParams = [
    'token' => $token,
    'patient' => $patientId,
    'purpose' => 'document_ingest',
];
$modernUrl = trim($settings->getString(ModuleSettings::KEY_MODERN_DASHBOARD_URL));
if ($modernUrl !== '') {
    $fragmentParams['theme'] = 'modern';
}

[$returnUrlClean] = explode('#', $returnTo, 2);
$redirect = $returnUrlClean . '#' . http_build_query($fragmentParams);

header('Location: ' . $redirect, true, 302);
exit;
