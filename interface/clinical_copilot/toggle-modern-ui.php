<?php

/**
 * Toggle the modern (Next.js) patient dashboard on/off.
 *
 * Flips the `patient_dashboard_modern_url` OpenEMR global between empty
 * (legacy PHP dashboard) and the configured modern dashboard URL. Same
 * authority surface as the OpenEMR admin Globals page, but exposed as a
 * one-click toggle in the patient header so a clinician can switch
 * styles without leaving the chart.
 *
 * Auth + CSRF: requires an authenticated OpenEMR session and a valid
 * CSRF token. Both are inherited from the OpenEMR bootstrap below.
 *
 * The fallback URL when turning the modern dashboard ON is read from
 * the `patient_dashboard_modern_default_url` global, with a final
 * fallback to `http://localhost:8400` so a fresh install still works
 * out of the box.
 *
 * After flipping, redirect to the parent main_screen (top frame) so
 * every iframe in the OpenEMR shell picks up the new style. Without
 * the top-frame reload, the Dashboard tab iframe keeps showing
 * whichever side it cached.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once __DIR__ . '/../globals.php';

use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Common\Session\SessionWrapperFactory;

$logger = new SystemLogger();
$session = SessionWrapperFactory::getInstance()->getActiveSession();

// Require an authenticated OpenEMR session. globals.php already bounces
// unauthenticated requests to the login page, but be belt-and-braces.
if (empty($session->get('authUserID'))) {
    http_response_code(401);
    echo xlt('Authentication required.');
    exit;
}

// CSRF check — accept either GET or POST so the link form in the
// patient header works without JS.
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

// Note: this toggle only flips a presentation-layer global. It does
// not expose any extra data — both the legacy PHP dashboard and the
// modern Next.js dashboard enforce per-user ACLs at their own entry
// points. So we accept any authenticated session here. If a future
// deploy wants to lock this to admin-only, gate on
// AclMain::aclCheckCore('admin', 'super').

$current = trim((string) ($GLOBALS['patient_dashboard_modern_url'] ?? ''));

// Default URL when turning ON — read from a sibling global, with a
// final fallback for fresh installs.
$defaultUrl = trim((string) ($GLOBALS['patient_dashboard_modern_default_url'] ?? ''));
if ($defaultUrl === '') {
    $defaultUrl = 'http://localhost:8400';
}

$next = $current === '' ? $defaultUrl : '';

sqlStatement(
    "UPDATE globals SET gl_value = ? WHERE gl_name = 'patient_dashboard_modern_url'",
    [$next]
);

$logger->info(
    'clinical_copilot.toggle.flipped',
    [
        'user' => $session->get('authUser'),
        'from' => $current,
        'to'   => $next,
    ]
);

// Redirect to the OpenEMR main screen so every iframe (Dashboard tab,
// patient header, tab strip) reloads against the new global value.
// `_top` is implicit because this script runs as a top-level navigation.
$home = '/interface/main/tabs/main.php?site=' . urlencode((string)($_SESSION['site_id'] ?? 'default'));
header('Location: ' . $home, true, 302);
exit;
