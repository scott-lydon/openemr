<?php

declare(strict_types=1);

/**
 * Register the Clinical Co-Pilot internal Snapshot Read API on the
 * OpenEMR REST kernel.
 *
 * The route is INTERNAL — it is intended for the sidecar to call from
 * inside the BAA boundary, not for clinicians. Listeners on
 * RestApiCreateEvent are the canonical way to extend the REST surface
 * from a module without patching ``_rest_routes.inc.php``.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 *
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

namespace OpenEMR\Modules\ClinicalCoPilot\Rest;

use OpenEMR\Events\RestApiExtend\RestApiCreateEvent;

final class SnapshotRouteRegistrar
{
    public function __construct(private readonly SnapshotController $controller)
    {
    }

    public function register(RestApiCreateEvent $event): RestApiCreateEvent
    {
        $routes = $event->getRouteMap();
        $routes['GET /api/clinical-copilot/snapshot/:uuid'] = function (string $uuid) {
            $controller = $this->controller;
            return $controller->getSnapshot($uuid);
        };
        $event->setRouteMap($routes);
        return $event;
    }
}
