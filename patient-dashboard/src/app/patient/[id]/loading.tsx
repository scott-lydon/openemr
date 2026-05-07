/**
 * Top-level loading state for /patient/[id].
 *
 * Renders before the patient header resolves. After the header arrives,
 * each individual card has its own <Suspense> fallback (CardSkeleton)
 * so this top-level loader is only briefly visible.
 */
import { CardSkeleton } from "@/components/card-skeleton";

export default function Loading() {
  return (
    <>
      <div className="sticky top-0 z-20 border-b border-zinc-200 bg-white/90 px-4 py-3 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/90">
        <div className="mx-auto h-6 max-w-screen-2xl animate-pulse rounded bg-zinc-200 dark:bg-zinc-800" />
      </div>
      <div className="mx-auto grid w-full max-w-screen-2xl grid-cols-1 gap-4 px-4 py-4 md:grid-cols-2 xl:grid-cols-3">
        <CardSkeleton title="Allergies" rows={3} />
        <CardSkeleton title="Problem List" rows={4} />
        <CardSkeleton title="Medications" rows={4} />
        <CardSkeleton title="Prescriptions" rows={3} />
        <CardSkeleton title="Care Team" rows={2} />
        <CardSkeleton title="Encounter History" rows={5} />
      </div>
    </>
  );
}
