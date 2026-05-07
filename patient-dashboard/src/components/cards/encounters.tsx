/**
 * Encounter History card — the dashboard's "+1" choice.
 *
 * Why encounters: every other "+1" in the brief (vitals, labs,
 * immunizations, appointments, notes) requires a second FHIR shard the
 * sidecar fan-out hasn't already proven against this OpenEMR version.
 * Encounter is in DEFAULT_RESOURCE_QUERIES — battle-tested.
 */
import { CalendarClock } from "lucide-react";
import { Card, CardHeader, EmptyState } from "../card";
import { Badge } from "../badge";
import { fetchEncounters } from "@/lib/fhir/client";

export async function EncountersCard({ uuid }: { uuid: string }) {
  const items = await fetchEncounters(uuid);
  return (
    <Card>
      <CardHeader
        title="Encounter History"
        count={items.length}
        icon={<CalendarClock size={16} />}
      />
      {items.length === 0 ? (
        <EmptyState>No recent encounters.</EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.slice(0, 10).map((e) => (
            <li
              key={e.id}
              className="flex flex-col gap-1 rounded-md bg-zinc-50 px-3 py-2 dark:bg-zinc-800/40"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                  {e.reason ?? e.classDisplay ?? e.classCode ?? "Encounter"}
                </span>
                {e.status ? <Badge status={e.status}>{e.status}</Badge> : null}
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-600 dark:text-zinc-400">
                {e.start ? <span>{formatDateTime(e.start)}</span> : null}
                {e.classDisplay ? <span>{e.classDisplay}</span> : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
