<?php

/**
 * Clinical Co-Pilot module admin settings page.
 *
 * Provides a single-page UI for every setting the module owns:
 *
 *   - Sidecar URL
 *   - JWT signing key (Generate button rotates it server-side)
 *   - License key
 *   - LLM (Large Language Model) provider selector
 *   - LLM API key (encrypted at rest with CryptoGen)
 *   - Purpose-of-use allow list
 *   - Per-use-case feature flags
 *   - Modern dashboard URL + default
 *   - Test Connectivity button
 *
 * Auth: requires the OpenEMR admin/super ACL. Any user who can change
 * core globals can change these.
 *
 * CSRF: every POST is CSRF-protected via CsrfUtils::verifyCsrfToken().
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 *
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require_once __DIR__ . '/../../../../../globals.php';

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\Header;
use OpenEMR\Modules\ClinicalCoPilot\ModuleSettings;

$session = SessionWrapperFactory::getInstance()->getActiveSession();

if (!AclMain::aclCheckCore('admin', 'super')) {
    http_response_code(403);
    echo xlt('Administrator access required to configure the Clinical Co-Pilot.');
    exit;
}

$settings = new ModuleSettings();
$settings->ensureSchema();
$logger = new SystemLogger();
$flash = [];

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (!CsrfUtils::verifyCsrfToken((string) ($_POST['csrf_token'] ?? ''), $session)) {
        http_response_code(403);
        echo xlt('CSRF check failed; reload the admin page and try again.');
        exit;
    }

    $action = (string) ($_POST['action'] ?? 'save');

    try {
        switch ($action) {
            case 'rotate_jwt':
                $new = $settings->rotateJwtSigningKey();
                $flash[] = [
                    'level' => 'success',
                    'text' => sprintf(
                        xl('A new JWT signing key was generated. Mirror this exact value into the sidecar\'s COPILOT_BFF_JWT_SIGNING_KEY environment variable or all /chat requests will return 401: %s'),
                        $new
                    ),
                ];
                break;

            case 'test_connectivity':
                $sidecarUrl = rtrim($settings->getString(ModuleSettings::KEY_SIDECAR_URL), '/');
                if ($sidecarUrl === '') {
                    $flash[] = ['level' => 'warning', 'text' => xl('Sidecar URL is empty. Save a URL before testing.')];
                    break;
                }
                $ch = curl_init($sidecarUrl . '/diagnostic');
                curl_setopt_array($ch, [
                    CURLOPT_RETURNTRANSFER => true,
                    CURLOPT_TIMEOUT_MS     => 5000,
                    CURLOPT_FOLLOWLOCATION => false,
                    CURLOPT_SSL_VERIFYPEER => true,
                    CURLOPT_SSL_VERIFYHOST => 2,
                ]);
                $body = curl_exec($ch);
                $code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
                $err = curl_error($ch);
                curl_close($ch);
                if ($body === false || $code !== 200) {
                    $flash[] = [
                        'level' => 'danger',
                        'text' => sprintf(
                            xl('Sidecar connectivity check failed (HTTP %d) %s. Verify the URL is reachable from this host, that the certificate is valid, and that the /diagnostic endpoint is enabled.'),
                            $code,
                            $err !== '' ? '— ' . $err : ''
                        ),
                    ];
                    break;
                }
                $decoded = json_decode((string) $body, associative: true);
                if (!is_array($decoded) || !isset($decoded['running_git_hash'])) {
                    $flash[] = [
                        'level' => 'danger',
                        'text' => xl('Sidecar returned a 200 but the response is not a valid /diagnostic payload (missing running_git_hash). Verify the sidecar is running this module\'s expected version.'),
                    ];
                    break;
                }
                $flash[] = [
                    'level' => 'success',
                    'text' => sprintf(
                        xl('Sidecar reachable. Version: %s. Auth method: %s. Purpose-check class: %s.'),
                        (string) ($decoded['running_git_hash'] ?? '?'),
                        (string) ($decoded['auth_method'] ?? '?'),
                        (string) ($decoded['purpose_check_class'] ?? '?')
                    ),
                ];
                break;

            case 'save':
            default:
                $writable = [
                    ModuleSettings::KEY_SIDECAR_URL,
                    ModuleSettings::KEY_LICENSE_KEY,
                    ModuleSettings::KEY_LLM_PROVIDER,
                    ModuleSettings::KEY_LLM_API_KEY,
                    ModuleSettings::KEY_PURPOSE_ALLOWLIST,
                    ModuleSettings::KEY_FF_DIAGNOSTIC,
                    ModuleSettings::KEY_FF_CHART_ERROR_SCAN,
                    ModuleSettings::KEY_FF_FOLLOW_UP,
                    ModuleSettings::KEY_FF_DOCUMENT_INGEST,
                    ModuleSettings::KEY_MODERN_DASHBOARD_URL,
                    ModuleSettings::KEY_MODERN_DASHBOARD_DEFAULT,
                ];
                foreach ($writable as $key) {
                    if (!array_key_exists($key, $_POST)) {
                        // Unchecked checkboxes do not appear in $_POST.
                        // Persist them as "0" so the feature flag toggle works.
                        if (in_array($key, [
                            ModuleSettings::KEY_FF_DIAGNOSTIC,
                            ModuleSettings::KEY_FF_CHART_ERROR_SCAN,
                            ModuleSettings::KEY_FF_FOLLOW_UP,
                            ModuleSettings::KEY_FF_DOCUMENT_INGEST,
                        ], true)) {
                            $settings->set($key, '0');
                        }
                        continue;
                    }
                    $raw = (string) $_POST[$key];
                    // Never overwrite the encrypted API key with an empty
                    // submitted value — the form uses placeholder dots so
                    // submitting blank means "no change", not "clear".
                    if ($key === ModuleSettings::KEY_LLM_API_KEY && $raw === '') {
                        continue;
                    }
                    $settings->set($key, $raw);
                }
                $flash[] = ['level' => 'success', 'text' => xl('Settings saved.')];
                break;
        }
    } catch (\Throwable $e) {
        $logger->error('clinical_copilot.admin.save_failed', ['error' => $e->getMessage()]);
        $flash[] = [
            'level' => 'danger',
            'text' => sprintf(
                xl('Failed to apply admin action "%s": %s. Check the OpenEMR error log.'),
                $action,
                $e->getMessage()
            ),
        ];
    }
}

$sidecarUrl       = $settings->getString(ModuleSettings::KEY_SIDECAR_URL);
$jwtKey           = $settings->getString(ModuleSettings::KEY_JWT_SIGNING_KEY);
$licenseKey       = $settings->getString(ModuleSettings::KEY_LICENSE_KEY);
$llmProvider      = $settings->getString(ModuleSettings::KEY_LLM_PROVIDER);
$llmApiKey        = $settings->getString(ModuleSettings::KEY_LLM_API_KEY);
$allowlist        = $settings->getString(ModuleSettings::KEY_PURPOSE_ALLOWLIST);
$ffDiag           = $settings->getBool(ModuleSettings::KEY_FF_DIAGNOSTIC);
$ffChart          = $settings->getBool(ModuleSettings::KEY_FF_CHART_ERROR_SCAN);
$ffFollow         = $settings->getBool(ModuleSettings::KEY_FF_FOLLOW_UP);
$ffDocs           = $settings->getBool(ModuleSettings::KEY_FF_DOCUMENT_INGEST);
$modernUrl        = $settings->getString(ModuleSettings::KEY_MODERN_DASHBOARD_URL);
$modernDefault    = $settings->getString(ModuleSettings::KEY_MODERN_DASHBOARD_DEFAULT);
$csrf             = CsrfUtils::collectCsrfToken(session: $session);

$jwtPreview = $jwtKey === '' ? xl('(not set)') : substr($jwtKey, 0, 8) . '…' . substr($jwtKey, -4);
$licensePreview = $licenseKey === '' ? xl('(not set)') : substr($licenseKey, 0, 8) . '…' . substr($licenseKey, -4);
$apiKeyDisplay = $llmApiKey === '' ? '' : '••••••••';
?><!doctype html>
<html>
<head>
    <title><?php echo xlt('Clinical Co-Pilot Settings'); ?></title>
    <?php Header::setupHeader(); ?>
    <style>
        body { padding: 16px; }
        fieldset { border: 1px solid var(--gray400, #ced4da); padding: 12px 16px; margin-bottom: 16px; border-radius: 4px; }
        legend { font-weight: 600; padding: 0 6px; width: auto; }
        .preview { font-family: monospace; color: #6c757d; }
        .form-row { margin-bottom: 12px; }
        .form-row label { font-weight: 600; }
    </style>
</head>
<body>
<h2><?php echo xlt('Clinical Co-Pilot Settings'); ?></h2>

<?php foreach ($flash as $entry): ?>
    <div class="alert alert-<?php echo attr($entry['level']); ?>"><?php echo text($entry['text']); ?></div>
<?php endforeach; ?>

<form method="post" action="" autocomplete="off">
    <input type="hidden" name="csrf_token" value="<?php echo attr($csrf); ?>">

    <fieldset>
        <legend><?php echo xlt('Sidecar'); ?></legend>
        <div class="form-row">
            <label for="sidecar_url"><?php echo xlt('Sidecar URL'); ?></label>
            <input id="sidecar_url" class="form-control" type="url" name="<?php echo attr(ModuleSettings::KEY_SIDECAR_URL); ?>" value="<?php echo attr($sidecarUrl); ?>" placeholder="https://copilot.example.com">
            <small class="form-text text-muted"><?php echo xlt('Base URL of the Clinical Co-Pilot sidecar (no trailing slash). Empty hides the launch button.'); ?></small>
        </div>
        <div class="form-row">
            <label><?php echo xlt('JWT Signing Key'); ?></label>
            <div class="d-flex align-items-center" style="gap: 12px;">
                <span class="preview"><?php echo text($jwtPreview); ?></span>
                <button type="submit" name="action" value="rotate_jwt" class="btn btn-secondary btn-sm"><?php echo xlt('Generate New Key'); ?></button>
            </div>
            <small class="form-text text-muted"><?php echo xlt('Rotating the key invalidates every in-flight task token and requires the sidecar\'s COPILOT_BFF_JWT_SIGNING_KEY env var to be updated to the new value.'); ?></small>
        </div>
        <div class="form-row">
            <button type="submit" name="action" value="test_connectivity" class="btn btn-info btn-sm"><?php echo xlt('Test Connectivity'); ?></button>
        </div>
    </fieldset>

    <fieldset>
        <legend><?php echo xlt('License'); ?></legend>
        <div class="form-row">
            <label for="license_key"><?php echo xlt('License Key'); ?></label>
            <input id="license_key" class="form-control" type="text" name="<?php echo attr(ModuleSettings::KEY_LICENSE_KEY); ?>" value="<?php echo attr($licenseKey); ?>">
            <small class="form-text text-muted"><?php echo text(sprintf(xl('Current: %s. Issued via Stripe Checkout after subscription. Without a key the sidecar refuses /chat with HTTP 402.'), $licensePreview)); ?></small>
        </div>
    </fieldset>

    <fieldset>
        <legend><?php echo xlt('LLM Provider'); ?></legend>
        <div class="form-row">
            <label for="llm_provider"><?php echo xlt('Provider'); ?></label>
            <select id="llm_provider" class="form-control" name="<?php echo attr(ModuleSettings::KEY_LLM_PROVIDER); ?>">
                <?php foreach (['openai', 'azure-openai', 'anthropic', 'mock'] as $opt): ?>
                    <option value="<?php echo attr($opt); ?>" <?php echo $llmProvider === $opt ? 'selected' : ''; ?>><?php echo text($opt); ?></option>
                <?php endforeach; ?>
            </select>
            <small class="form-text text-muted"><?php echo xlt('Sets the COPILOT_LLM_PROVIDER env var the sidecar honours at next restart.'); ?></small>
        </div>
        <div class="form-row">
            <label for="llm_api_key"><?php echo xlt('LLM API Key'); ?></label>
            <input id="llm_api_key" class="form-control" type="password" name="<?php echo attr(ModuleSettings::KEY_LLM_API_KEY); ?>" value="<?php echo attr($apiKeyDisplay); ?>" autocomplete="new-password">
            <small class="form-text text-muted"><?php echo xlt('Stored encrypted via CryptoGen. Submitting blank keeps the existing value.'); ?></small>
        </div>
    </fieldset>

    <fieldset>
        <legend><?php echo xlt('Authorization'); ?></legend>
        <div class="form-row">
            <label for="purpose_allowlist"><?php echo xlt('Purpose-of-Use Allow List'); ?></label>
            <input id="purpose_allowlist" class="form-control" type="text" name="<?php echo attr(ModuleSettings::KEY_PURPOSE_ALLOWLIST); ?>" value="<?php echo attr($allowlist); ?>">
            <small class="form-text text-muted"><?php echo xlt('Comma-separated list. Defaults: diagnostic_cross_check,chart_error_scan,follow_up_question,document_ingest.'); ?></small>
        </div>
    </fieldset>

    <fieldset>
        <legend><?php echo xlt('Feature Flags'); ?></legend>
        <div class="form-check">
            <input class="form-check-input" type="checkbox" id="ff_diag" name="<?php echo attr(ModuleSettings::KEY_FF_DIAGNOSTIC); ?>" value="1" <?php echo $ffDiag ? 'checked' : ''; ?>>
            <label class="form-check-label" for="ff_diag"><?php echo xlt('Diagnostic cross-check'); ?></label>
        </div>
        <div class="form-check">
            <input class="form-check-input" type="checkbox" id="ff_chart" name="<?php echo attr(ModuleSettings::KEY_FF_CHART_ERROR_SCAN); ?>" value="1" <?php echo $ffChart ? 'checked' : ''; ?>>
            <label class="form-check-label" for="ff_chart"><?php echo xlt('Chart error scan'); ?></label>
        </div>
        <div class="form-check">
            <input class="form-check-input" type="checkbox" id="ff_follow" name="<?php echo attr(ModuleSettings::KEY_FF_FOLLOW_UP); ?>" value="1" <?php echo $ffFollow ? 'checked' : ''; ?>>
            <label class="form-check-label" for="ff_follow"><?php echo xlt('Follow-up question'); ?></label>
        </div>
        <div class="form-check">
            <input class="form-check-input" type="checkbox" id="ff_docs" name="<?php echo attr(ModuleSettings::KEY_FF_DOCUMENT_INGEST); ?>" value="1" <?php echo $ffDocs ? 'checked' : ''; ?>>
            <label class="form-check-label" for="ff_docs"><?php echo xlt('Document ingest'); ?></label>
        </div>
    </fieldset>

    <fieldset>
        <legend><?php echo xlt('Modern Dashboard (optional)'); ?></legend>
        <div class="form-row">
            <label for="modern_dashboard_url"><?php echo xlt('Modern Dashboard URL (currently active)'); ?></label>
            <input id="modern_dashboard_url" class="form-control" type="url" name="<?php echo attr(ModuleSettings::KEY_MODERN_DASHBOARD_URL); ?>" value="<?php echo attr($modernUrl); ?>">
        </div>
        <div class="form-row">
            <label for="modern_dashboard_default"><?php echo xlt('Modern Dashboard URL (default when toggled on)'); ?></label>
            <input id="modern_dashboard_default" class="form-control" type="url" name="<?php echo attr(ModuleSettings::KEY_MODERN_DASHBOARD_DEFAULT); ?>" value="<?php echo attr($modernDefault); ?>">
        </div>
    </fieldset>

    <button type="submit" name="action" value="save" class="btn btn-primary"><?php echo xlt('Save'); ?></button>
</form>
</body>
</html>
