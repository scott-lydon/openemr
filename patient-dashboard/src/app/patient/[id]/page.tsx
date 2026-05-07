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
import { Link } from "lucide-react";
import { fetchPatientHeader } from "@/lib/fhir/client";
import { PatientHeaderBar } from "@/components/patient-header";
import { CardSkeleton } from "@/components/card-skeleton";
import { SignOutButton } from "@/components/sign-out-button";
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

  return (
    <>
      <PatientHeaderBar patient={patient} />
      <div className="mx-auto flex w-full max-w-screen-2xl flex-col px-4 py-4">
        <div className="mb-3 flex items-center justify-between">
          <a
            href="/"
            className="inline-flex items-center gap-1 text-sm text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
          >
            <Link size={14} />
            <span>Back to picker</span>
          </a>
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
