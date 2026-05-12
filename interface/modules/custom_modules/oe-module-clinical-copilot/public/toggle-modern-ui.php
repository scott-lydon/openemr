<?php

/**
 * Toggle the modern (Next.js) patient dashboard on/off (module-scoped).
 *
 * Flips the module's KEY_MODERN_DASHBOARD_URL between empty (legacy PHP
 * dashboard) and the configured modern dashboard URL. Same authority
 * surface as the OpenEMR admin Globals page, but exposed as a one-click
 * toggle in the patient header so a clinician can switch styles without
 * leaving the chart.
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

use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Modules\ClinicalCoPilot\ModuleSettings;

$logger = new SystemLogger();
$session = SessionWrapperFactory::getInstance()->getActiveSession();
$settings = new ModuleSettings();

if (empty($session->get('authUserID'))) {
    http_response_code(401);
    echo xlt('Authentication required.');
    exit;
}

$token = $_GET['csrf_token'] ?? $_POST['csrf_token'] ?? '';
if (!CsrfUtils::verifyCsrfToken($token, $session)) {
    $logger->warning(
        'clinical_copilot.toggle.csrf_failed',
        ['user' => $session->get('authUser')]
    );
    http_response_code(403);
    echo xlt('CSRF token mismatch.');
    exit;
}

$current = trim($settings->getString(ModuleSettings::KEY_MODERN_DASHBOARD_URL));
$defaultUrl = trim($settings->getString(ModuleSettings::KEY_MODERN_DASHBOARD_DEFAULT));
if ($defaultUrl === '') {
    $defaultUrl = 'http://localhost:8400';
}

$next = $current === '' ? $defaultUrl : '';
$settings->set(ModuleSettings::KEY_MODERN_DASHBOARD_URL, $next);

$logger->info(
    'clinical_copilot.toggle.flipped',
    [
        'user' => $session->get('authUser'),
        'from' => $current,
        'to'   => $next,
    ]
);

$home = '/interface/main/tabs/main.php?site=' . urlencode((string) ($_SESSION['site_id'] ?? 'default'));
header('Location: ' . $home, true, 302);
exit;
