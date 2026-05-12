<?php

/**
 * Clinical Co-Pilot uninstall listener.
 *
 * Fired by the OpenEMR Module Manager when the user clicks "Uninstall"
 * on the module's row. Responsibilities:
 *
 *   1. Disable (do NOT delete) the "Clinical Co-Pilot Sidecar" OAuth
 *      client. Disabling preserves the audit trail; deleting would
 *      lose the row that explains who minted historical tokens.
 *
 *   2. Drop the module-private settings table so a reinstall starts
 *      from defaults. Operators who want to keep settings can comment
 *      out the dropSchema() call here.
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

use OpenEMR\Common\Auth\OpenIDConnect\Repositories\ClientRepository;
use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Modules\ClinicalCoPilot\BootstrapService;
use Symfony\Contracts\EventDispatcher\Event;

final class UninstallListener
{
    private const CLIENT_NAME = 'Clinical Co-Pilot Sidecar';

    public function __construct(
        private readonly BootstrapService $bootstrap,
        private readonly ?SystemLogger $logger = null,
    ) {
    }

    public function __invoke(Event $event): void
    {
        $logger = $this->logger ?? new SystemLogger();

        // 1. Disable OAuth client. Use a defensive try so a failure in
        //    the OAuth disable path does not block uninstall — the
        //    operator can revoke manually from the API Clients admin.
        try {
            $repo = new ClientRepository();
            $disabled = 0;
            foreach ($repo->listClientEntities() as $client) {
                if ($client->getName() === self::CLIENT_NAME) {
                    $repo->saveIsEnabled($client, false);
                    $disabled++;
                }
            }
            $logger->info(
                'clinical_copilot.uninstall.oauth_disabled',
                ['count' => $disabled]
            );
        } catch (\Throwable $e) {
            $logger->error(
                'clinical_copilot.uninstall.oauth_disable_failed',
                [
                    'error' => $e->getMessage(),
                    'hint'  => 'Open Admin → API Clients and disable rows named '
                        . self::CLIENT_NAME . ' manually.',
                ]
            );
        }

        // 2. Drop the settings table. Wrapped in try so a partial
        //    uninstall does not raise an unhandled exception into
        //    the module-manager UI.
        try {
            $this->bootstrap->getSettings()->dropSchema();
            $logger->info('clinical_copilot.uninstall.settings_dropped');
        } catch (\Throwable $e) {
            $logger->error(
                'clinical_copilot.uninstall.settings_drop_failed',
                ['error' => $e->getMessage()]
            );
        }
    }
}
