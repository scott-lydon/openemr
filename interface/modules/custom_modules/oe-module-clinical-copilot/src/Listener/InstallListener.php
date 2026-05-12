<?php

/**
 * Clinical Co-Pilot install listener.
 *
 * Fired by the OpenEMR Module Manager when the user clicks "Install"
 * on the module's row. Responsibilities:
 *
 *   1. Create the module-private settings table (ModuleSettings::TABLE)
 *      if absent.
 *   2. Seed the JWT signing key (so the operator does not see a
 *      "configure the signing key" error on first patient summary
 *      page-load).
 *   3. If the sidecar URL is configured, run the provisioning command
 *      to register the OAuth client.
 *   4. Probe the sidecar's /diagnostic endpoint and surface a precise
 *      error if it is unreachable.
 *
 * Each step records its outcome in the OpenEMR error log. Failures
 * are non-fatal — the module installs even if the sidecar is not yet
 * online, so the operator can configure it after the fact.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 *
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCoPilot\Listener;

use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Modules\ClinicalCoPilot\BootstrapService;
use OpenEMR\Modules\ClinicalCoPilot\ModuleSettings;
use Symfony\Contracts\EventDispatcher\Event;

final class InstallListener
{
    public function __construct(
        private readonly BootstrapService $bootstrap,
        private readonly ?SystemLogger $logger = null,
    ) {
    }

    public function __invoke(Event $event): void
    {
        $logger = $this->logger ?? new SystemLogger();
        $settings = $this->bootstrap->getSettings();

        try {
            $settings->ensureSchema();
            $logger->info('clinical_copilot.install.schema_ready');
        } catch (\Throwable $e) {
            $logger->error(
                'clinical_copilot.install.schema_failed',
                ['error' => $e->getMessage(), 'class' => $e::class]
            );
            return;
        }

        if ($settings->getString(ModuleSettings::KEY_JWT_SIGNING_KEY) === '') {
            $new = $settings->rotateJwtSigningKey();
            $logger->info(
                'clinical_copilot.install.jwt_key_seeded',
                ['preview' => substr($new, 0, 8) . '…' . substr($new, -4)]
            );
        }

        $sidecarUrl = rtrim($settings->getString(ModuleSettings::KEY_SIDECAR_URL), '/');
        if ($sidecarUrl === '') {
            $logger->warning(
                'clinical_copilot.install.sidecar_url_empty: open the admin page '
                . 'and set the Sidecar URL before clinicians try to launch.'
            );
            return;
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
            $logger->error(
                'clinical_copilot.install.sidecar_unreachable',
                [
                    'sidecar_url' => $sidecarUrl,
                    'http_code'   => $code,
                    'curl_error'  => $err,
                    'hint'        => 'Module installed but sidecar is not yet up. '
                        . 'Verify https://<sidecar>/diagnostic returns HTTP 200 '
                        . 'from this host.',
                ]
            );
            return;
        }
        $logger->info('clinical_copilot.install.sidecar_reachable', ['sidecar_url' => $sidecarUrl]);
    }
}
