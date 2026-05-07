/**
 * Allergies card — async Server Component.
 *
 * Streamed independently so a slow AllergyIntolerance shard doesn't block
 * the rest of the dashboard. Errors are caught by the parent
 * <ErrorBoundary> and rendered with <CardError>.
 */
import { ShieldAlert } from "lucide-react";
import { Card, CardHeader, EmptyState } from "../card";
import { Badge } from "../badge";
import { fetchAllergies } from "@/lib/fhir/client";

export async function AllergiesCard({ uuid }: { uuid: string }) {
  const items = await fetchAllergies(uuid);
  return (
    <Card>
      <CardHeader
        title="Allergies"
        count={items.length}
        icon={<ShieldAlert size={16} />}
      />
      {items.length === 0 ? (
        <EmptyState>No documented allergies.</EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((a) => (
            <li
              key={a.id}
              className="flex flex-col gap-1 rounded-md bg-zinc-50 px-3 py-2 dark:bg-zinc-800/40"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                  {a.substance}
                </span>
                <div className="flex flex-shrink-0 items-center gap-1">
                  {a.criticality ? (
                    <Badge status={a.criticality}>
                      {a.criticality === "high"
                        ? "High risk"
                        : a.criticality === "low"
                          ? "Low risk"
                          : a.criticality}
                    </Badge>
                  ) : null}
                  {a.clinicalStatus ? (
                    <Badge status={a.clinicalStatus}>{a.clinicalStatus}</Badge>
                  ) : null}
                </div>
              </div>
              {a.reactions.length > 0 ? (
                <p className="text-xs text-zinc-600 dark:text-zinc-400">
                  Reactions: {a.reactions.join(", ")}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
