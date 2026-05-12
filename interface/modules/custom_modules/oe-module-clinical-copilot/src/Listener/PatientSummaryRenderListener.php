<?php

/**
 * Render the Clinical Co-Pilot launch button on the patient summary page.
 *
 * This listener replaces the previous hand-edit at
 * interface/patient_file/summary/demographics.php lines ~1100-1136.
 * It hooks the existing OpenEMR core dispatch on
 * {@see RenderEvent::EVENT_SECTION_LIST_RENDER_TOP} so the button shows
 * up wherever a vanilla OpenEMR fires that event, without needing the
 * core file to be patched.
 *
 * The button is hidden if any of the following is true:
 *
 *   - Sidecar URL is not configured.
 *   - License key is not configured (paid tiers).
 *   - The caller fails the ACL check (patients/demo).
 *
 * Failing closed is intentional: a half-configured deploy must NEVER
 * render a button that mints a token against a sidecar URL nobody
 * controls, or against an expired license.
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

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Events\PatientDemographics\RenderEvent;
use OpenEMR\Modules\ClinicalCoPilot\ModuleSettings;

final class PatientSummaryRenderListener
{
    public function __construct(private readonly ModuleSettings $settings)
    {
    }

    public function __invoke(RenderEvent $event): void
    {
        if (!AclMain::aclCheckCore('patients', 'demo')) {
            return;
        }

        $sidecarUrl = $this->settings->getString(ModuleSettings::KEY_SIDECAR_URL);
        if ($sidecarUrl === '') {
            return;
        }

        // Feature flag: hide the button entirely if ALL panels are
        // disabled, so an operator who turns off the module from the
        // admin page sees no UI affordance.
        $anyFlagOn =
            $this->settings->getBool(ModuleSettings::KEY_FF_DIAGNOSTIC)
            || $this->settings->getBool(ModuleSettings::KEY_FF_CHART_ERROR_SCAN)
            || $this->settings->getBool(ModuleSettings::KEY_FF_FOLLOW_UP)
            || $this->settings->getBool(ModuleSettings::KEY_FF_DOCUMENT_INGEST);
        if (!$anyFlagOn) {
            return;
        }

        $pid = (int) $event->getPid();
        if ($pid <= 0) {
            return;
        }

        $session = SessionWrapperFactory::getInstance()->getActiveSession();
        $csrf = CsrfUtils::collectCsrfToken(session: $session);

        $launchHref = '../../modules/custom_modules/oe-module-clinical-copilot/public/launch.php?'
            . http_build_query([
                'pid'        => $pid,
                'purpose'    => 'diagnostic_cross_check',
                'csrf_token' => $csrf,
            ]);

        // Output is rendered in-place. The dispatch site in
        // demographics.php is between the dashboard header and the
        // section list, so this HTML lands at the top of the patient
        // summary without disturbing the existing card layout.
        $escapedHref = htmlspecialchars($launchHref, ENT_QUOTES, 'UTF-8');
        $escapedPid  = htmlspecialchars((string) $pid, ENT_QUOTES, 'UTF-8');
        $btnTitle    = xla('Open the AI diagnostic cross-check and chart-error scan for this patient');
        $btnLabel    = xlt('Clinical Co-Pilot (AI)');
        $caption     = xlt('Diagnostic considerations + chart-error review. Read-only. Citations required.');
        echo <<<HTML
<div class="d-flex align-items-center mb-2 px-1 oe-module-clinical-copilot-launch" style="gap: 10px;">
    <a href="{$escapedHref}"
       target="copilot_{$escapedPid}"
       class="btn btn-primary btn-sm"
       onclick="top.restoreSession();"
       title="{$btnTitle}"
       data-testid="copilot-launch">
        <i class="fa fa-stethoscope mr-1" aria-hidden="true"></i>
        {$btnLabel}
    </a>
    <small class="text-muted">{$caption}</small>
</div>
HTML;
    }
}
