<?php

/**
 * Bootstrap for the Clinical Co-Pilot module.
 *
 * Loaded by OpenEMR's module loader when the module is enabled. Responsible
 * for:
 *
 *   1. Registering the module's PSR-4 namespace with the OpenEMR class
 *      loader so the controllers, listeners, and CLI commands resolve.
 *   2. Wiring the {@see ClinicalCoPilotSummaryButtonListener} onto the
 *      {@see RenderEvent::EVENT_SECTION_LIST_RENDER_TOP} event so the
 *      patient-summary launch button shows up without a hand-edit of
 *      interface/patient_file/summary/demographics.php.
 *   3. Registering the Symfony console command that provisions the OAuth
 *      client used by the sidecar.
 *   4. Registering the REST route for the internal Snapshot Read API.
 *   5. Registering Install / Uninstall listeners so the user can enable /
 *      disable the module from the Module Manager without leaving stale
 *      state behind.
 *
 * The module deliberately registers NOTHING in library/globals.inc.php.
 * All configuration (sidecar URL, JWT signing key, LLM provider, license
 * key, purpose-of-use allow list, feature flags) lives in the module's
 * own settings table {@see ModuleSettings}, accessible only through the
 * module's admin page at public/admin.php.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 *
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Events\PatientDemographics\RenderEvent as PatientDemographicsRenderEvent;
use OpenEMR\Events\RestApiExtend\RestApiCreateEvent;
use OpenEMR\Modules\ClinicalCoPilot\BootstrapService;
use OpenEMR\Modules\ClinicalCoPilot\Console\ProvisionClinicalCoPilotApiClientCommand;
use OpenEMR\Modules\ClinicalCoPilot\Listener\InstallListener;
use OpenEMR\Modules\ClinicalCoPilot\Listener\PatientSummaryRenderListener;
use OpenEMR\Modules\ClinicalCoPilot\Listener\UninstallListener;
use OpenEMR\Modules\ClinicalCoPilot\Rest\SnapshotRouteRegistrar;
use Symfony\Contracts\EventDispatcher\EventDispatcherInterface;

/**
 * @global \OpenEMR\Core\ModulesClassLoader $classLoader
 * @global EventDispatcherInterface $eventDispatcher
 */

// 1. Register the module's PSR-4 namespace. The IfNotExists variant lets
//    this module be loaded twice (e.g. via composer.json autoload plus
//    runtime registration) without throwing.
$classLoader->registerNamespaceIfNotExists(
    'OpenEMR\\Modules\\ClinicalCoPilot\\',
    __DIR__ . DIRECTORY_SEPARATOR . 'src'
);

// 2. Resolve the event dispatcher. Module bootstrap files are loaded
//    inside an environment where $eventDispatcher is populated by the
//    Kernel; if it is missing we cannot register listeners, which means
//    the launch button will not appear. Log a precise error rather than
//    fail silently — the operator should see why the button is missing
//    from the patient summary even though the module is enabled.
$logger = new SystemLogger();
if (!isset($eventDispatcher) || !$eventDispatcher instanceof EventDispatcherInterface) {
    $logger->error(
        'oe-module-clinical-copilot: $eventDispatcher missing during bootstrap. '
        . 'The patient-summary launch button will NOT be rendered. Verify the '
        . 'module loader (interface/modules/zend_modules/Module.php or '
        . 'src/Core/AbstractModuleConfigListener) passes the kernel dispatcher.'
    );
    return;
}

// 3. Construct the BootstrapService — it owns the listener wiring so the
//    bootstrap file stays under 100 lines and can be unit-tested via
//    BootstrapServiceTest. Service-locator anti-patterns are confined to
//    this file; everything downstream is plain DI.
$bootstrap = new BootstrapService();

// 4. Wire the patient-summary launch button listener. The PatientSummary
//    listener echoes the button HTML; it is gated internally on the
//    sidecar URL setting being non-empty so a half-configured deploy
//    does not surface a broken link.
$eventDispatcher->addListener(
    PatientDemographicsRenderEvent::EVENT_SECTION_LIST_RENDER_TOP,
    new PatientSummaryRenderListener($bootstrap->getSettings()),
);

// 5. Register the REST API extension for the Snapshot Read API. The
//    OpenEMR REST kernel fires RestApiCreateEvent on boot; we attach
//    the Clinical Co-Pilot Snapshot route there.
$eventDispatcher->addListener(
    RestApiCreateEvent::EVENT_HANDLE,
    static function (RestApiCreateEvent $event) use ($bootstrap): RestApiCreateEvent {
        return (new SnapshotRouteRegistrar($bootstrap->getSnapshotController()))->register($event);
    }
);

// 6. Install / Uninstall lifecycle hooks.
$eventDispatcher->addListener(
    'modules.lifecycle.install.clinical-copilot',
    new InstallListener($bootstrap)
);
$eventDispatcher->addListener(
    'modules.lifecycle.uninstall.clinical-copilot',
    new UninstallListener($bootstrap)
);

// 7. Register the provision command on the global Symfony console
//    Application. This is the canonical entry point used by
//    scripts/setup-openemr-client.sh and by the install listener.
$bootstrap->registerConsoleCommand(new ProvisionClinicalCoPilotApiClientCommand());

$logger->debug(
    'oe-module-clinical-copilot bootstrap complete',
    ['listeners' => [
        PatientDemographicsRenderEvent::EVENT_SECTION_LIST_RENDER_TOP,
        RestApiCreateEvent::EVENT_HANDLE,
        'modules.lifecycle.install.clinical-copilot',
        'modules.lifecycle.uninstall.clinical-copilot',
    ]]
);
