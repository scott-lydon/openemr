<?php

/**
 * Clinical Co-Pilot module bootstrap service.
 *
 * Owns the wiring graph for the module: settings, controllers,
 * listeners, console commands. Keeps the openemr.bootstrap.php file
 * declarative and testable.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 *
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\ClinicalCoPilot;

use OpenEMR\Modules\ClinicalCoPilot\Rest\SnapshotController;
use Symfony\Component\Console\Application as ConsoleApplication;
use Symfony\Component\Console\Command\Command as ConsoleCommand;

final class BootstrapService
{
    private readonly ModuleSettings $settings;
    private readonly SnapshotController $snapshotController;
    /** @var list<ConsoleCommand> */
    private array $registeredCommands = [];

    public function __construct(?ModuleSettings $settings = null, ?SnapshotController $snapshotController = null)
    {
        $this->settings = $settings ?? new ModuleSettings();
        $this->snapshotController = $snapshotController ?? new SnapshotController();
    }

    public function getSettings(): ModuleSettings
    {
        return $this->settings;
    }

    public function getSnapshotController(): SnapshotController
    {
        return $this->snapshotController;
    }

    /**
     * Register a Symfony console command with the global Application
     * instance when one is available. Modules cannot assume the console
     * is constructed (the bootstrap fires under HTTP too), so absence
     * is silent — the command is recorded so a CLI bootstrap path can
     * pick it up later if needed.
     */
    public function registerConsoleCommand(ConsoleCommand $command): void
    {
        $this->registeredCommands[] = $command;
        if (isset($GLOBALS['OPENEMR_CONSOLE']) && $GLOBALS['OPENEMR_CONSOLE'] instanceof ConsoleApplication) {
            $GLOBALS['OPENEMR_CONSOLE']->add($command);
        }
    }

    /**
     * @return list<ConsoleCommand>
     */
    public function getRegisteredCommands(): array
    {
        return $this->registeredCommands;
    }
}
