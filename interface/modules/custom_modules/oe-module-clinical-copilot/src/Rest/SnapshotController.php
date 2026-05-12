<?php

declare(strict_types=1);

/**
 * Agent Snapshot Read API for the Clinical Co-Pilot sidecar.
 *
 * Exposes the denormalised patient snapshot in a single call, wrapping
 * the FHIR (Fast Healthcare Interoperability Resources) fan-out plus
 * deterministic reconciliation pass that the sidecar would otherwise
 * issue resource-by-resource. Read-only by contract; future write
 * surfaces live in separate controllers and carry their own
 * Access Control List checks.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 *
 * @author    Scott Lydon <relays.inanity.0n@icloud.com>
 * @copyright Copyright (c) 2026 Scott Lydon
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

namespace OpenEMR\Modules\ClinicalCoPilot\Rest;

use OpenEMR\Common\Logging\SystemLogger;
use OpenEMR\Services\BaseService;

/**
 * Read-only controller for the Clinical Co-Pilot snapshot endpoint.
 *
 * The reconciliation runs in the Python sidecar's
 * ``sidecar/snapshot/reconciler.py``. This controller exists so the
 * sidecar can pull a single JSON payload over a stable internal API
 * when an outbound FHIR fan-out would breach the
 * Business Associate Agreement boundary.
 */
final class SnapshotController extends BaseService
{
    public const TABLE_NAME = 'patient_data';

    public function __construct()
    {
        parent::__construct(self::TABLE_NAME);
    }

    /**
     * Return the denormalised snapshot for one patient.
     *
     * The shape of the JSON returned matches the Pydantic
     * ``PatientSnapshot`` model in
     * ``clinical-copilot/sidecar/snapshot/models.py``. See
     * ``ARCHITECTURE.md`` §2.1 for the canonical example.
     *
     * @param string $patientUuid FHIR resource UUID, not the legacy numeric pid.
     *
     * @return array{
     *   patient_id: string,
     *   snapshot_version: string,
     *   demographics: array<string, mixed>,
     *   active_problems: array<int, array<string, mixed>>,
     *   medications: array<int, array<string, mixed>>,
     *   allergies: array<int, array<string, mixed>>,
     *   recent_vitals: array<int, array<string, mixed>>,
     *   recent_labs: array<int, array<string, mixed>>,
     *   presenting: array<string, mixed>,
     *   quality_flags: array<int, array<string, mixed>>,
     * }
     */
    public function getSnapshot(string $patientUuid): array
    {
        if ($patientUuid === '') {
            throw new \InvalidArgumentException(
                'patientUuid must be a non-empty FHIR Patient UUID; got empty string.'
            );
        }
        (new SystemLogger())->info(
            'Agent snapshot requested',
            ['patient_uuid' => $patientUuid]
        );
        return [
            'patient_id' => $patientUuid,
            'snapshot_version' => (new \DateTimeImmutable('now', new \DateTimeZone('UTC')))
                ->format(\DateTimeInterface::ATOM),
            'demographics' => [],
            'active_problems' => [],
            'medications' => [],
            'allergies' => [],
            'recent_vitals' => [],
            'recent_labs' => [],
            'presenting' => [],
            'quality_flags' => [
                [
                    'code' => 'snapshot_stub',
                    'description' => 'PHP-side snapshot is a stub; the sidecar performs the parallel '
                        . 'FHIR fan-out itself. Replace this when the cold-start path needs to bypass '
                        . 'outbound FHIR for BAA-isolated deploys.',
                    'related_provenance' => [],
                ],
            ],
        ];
    }
}
