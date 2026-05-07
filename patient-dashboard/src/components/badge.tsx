/**
 * Status pill.
 *
 * Variant maps a status string from FHIR (active, resolved, low, high, etc.)
 * into a colour tier. The mapping covers the common cases and falls back
 * to a neutral grey rather than refusing to render an unknown status.
 */
import { cn } from "@/lib/ui/cn";

const TIER: Record<string, string> = {
  // Allergy criticality
  high: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
  low: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  "unable-to-assess":
    "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  // Clinical / encounter status
  active: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  inactive: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  resolved: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  recurrence: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  relapse: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  // Medication status
  completed: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  stopped: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  "on-hold": "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200",
  // Patient status
  true: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
  false: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200",
};

const NEUTRAL =
  "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300";

export function Badge({
  children,
  status,
  className,
}: {
  children: React.ReactNode;
  status?: string | null;
  className?: string;
}) {
  const key = status ? status.toLowerCase() : "";
  const tier = TIER[key] ?? NEUTRAL;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        tier,
        className,
      )}
    >
      {children}
    </span>
  );
}
