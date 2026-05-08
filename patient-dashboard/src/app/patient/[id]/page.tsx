/**
 * Patient dashboard — the main view.
 *
 * Layout:
 *   <PatientHeaderBar />     ← persistent identity bar
 *   <main>                   ← responsive grid of cards
 *     <Suspense><AllergiesCard /></Suspense>
 *     <Suspense><ProblemsCard /></Suspense>
 *     <Suspense><MedicationsCard /></Suspense>
 *     <Suspense><PrescriptionsCard /></Suspense>
 *     <Suspense><CareTeamCard /></Suspense>
 *     <Suspense><EncountersCard /></Suspense>  ← +1 of choice
 *   </main>
 *
 * Each card is a Suspense boundary so its FHIR fetch resolves
 * independently. The first paint shows skeletons; cards swap to real
 * content as their shards return. Slow OpenEMR shards (Conditions on a
 * patient with hundreds of rows) don't block fast ones (CareTeam).
 */
import { Suspense } from "react";
import { ArrowLeftFromLine, Link } from "lucide-react";
import { env } from "@/lib/env";
import { fetchPatientHeader } from "@/lib/fhir/client";
import { PatientHeaderBar } from "@/components/patient-header";
import { CardSkeleton } from "@/components/card-skeleton";
import { SignOutButton } from "@/components/sign-out-button";

/*
 * Derive the OpenEMR shell origin from the configured OIDC issuer so
 * the "Back to OpenEMR" link points at the same instance the
 * dashboard is talking to. OPENEMR_ISSUER is shaped like
 * "https://localhost:9300/oauth2/default" — strip the path and use
 * the origin. If parsing fails (misconfig), fall back to the
 * canonical dev URL so the link still does something useful instead
 * of rendering as href="".
 */
function deriveOpenEmrShellUrl(): string {
  try {
    const issuer = new URL(env.OPENEMR_ISSUER);
    return issuer.origin + "/";
  } catch {
    return "https://localhost:9300/";
  }
}
import { AllergiesCard } from "@/components/cards/allergies";
import { ProblemsCard } from "@/components/cards/problems";
import { MedicationsCard } from "@/components/cards/medications";
import { PrescriptionsCard } from "@/components/cards/prescriptions";
import { CareTeamCard } from "@/components/cards/care-team";
import { EncountersCard } from "@/components/cards/encounters";

export default async function PatientPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  // Patient header resolves first because every card below depends on a
  // valid patient; if Patient? returns 404 the whole page should fail
  // fast (handled by error.tsx) rather than surfacing six broken cards.
  const patient = await fetchPatientHeader(id);

  const openemrShellUrl = deriveOpenEmrShellUrl();

  return (
    <>
      <PatientHeaderBar patient={patient} />
      <div className="mx-auto flex w-full max-w-screen-2xl flex-col px-4 py-4">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-4">
            <a
              href="/"
              className="inline-flex items-center gap-1 text-sm text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
            >
              <Link size={14} />
              <span>Back to picker</span>
            </a>
            {/*
             * "Back to OpenEMR" — escape hatch for clinicians who
             * landed on the modern dashboard via a deep link or who
             * closed the OpenEMR tab. Points at the OpenEMR shell
             * origin (derived from OPENEMR_ISSUER above). target="_self"
             * is intentional: this tab is the dashboard's tab, the
             * user's done with it.
             */}
            <a
              href={openemrShellUrl}
              className="inline-flex items-center gap-1 text-sm text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
            >
              <ArrowLeftFromLine size={14} />
              <span>Back to OpenEMR</span>
            </a>
          </div>
          <SignOutButton />
        </div>
        <main
          className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3"
          aria-label="Clinical summary"
        >
          <Suspense fallback={<CardSkeleton title="Allergies" rows={3} />}>
            <AllergiesCard uuid={id} />
          </Suspense>
          <Suspense fallback={<CardSkeleton title="Problem List" rows={4} />}>
            <ProblemsCard uuid={id} />
          </Suspense>
          <Suspense fallback={<CardSkeleton title="Medications" rows={4} />}>
            <MedicationsCard uuid={id} />
          </Suspense>
          <Suspense fallback={<CardSkeleton title="Prescriptions" rows={3} />}>
            <PrescriptionsCard uuid={id} />
          </Suspense>
          <Suspense fallback={<CardSkeleton title="Care Team" rows={2} />}>
            <CareTeamCard uuid={id} />
          </Suspense>
          <Suspense
            fallback={<CardSkeleton title="Encounter History" rows={5} />}
          >
            <EncountersCard uuid={id} />
          </Suspense>
        </main>
      </div>
    </>
  );
}
