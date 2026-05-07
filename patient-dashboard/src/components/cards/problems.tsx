/**
 * Problem List card — active conditions only.
 *
 * Sorted by onset date, newest first. Falls back to recordedDate when
 * onsetDateTime is null.
 */
import { Activity } from "lucide-react";
import { Card, CardHeader, EmptyState } from "../card";
import { Badge } from "../badge";
import { fetchProblems } from "@/lib/fhir/client";

export async function ProblemsCard({ uuid }: { uuid: string }) {
  const items = await fetchProblems(uuid);
  const sorted = [...items].sort((a, b) => {
    const da = a.onsetDate ? new Date(a.onsetDate).getTime() : 0;
    const db = b.onsetDate ? new Date(b.onsetDate).getTime() : 0;
    return db - da;
  });

  return (
    <Card>
      <CardHeader
        title="Problem List"
        count={sorted.length}
        icon={<Activity size={16} />}
      />
      {sorted.length === 0 ? (
        <EmptyState>No active problems.</EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {sorted.map((p) => (
            <li
              key={p.id}
              className="flex flex-col gap-1 rounded-md bg-zinc-50 px-3 py-2 dark:bg-zinc-800/40"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                  {p.label}
                </span>
                {p.clinicalStatus ? (
                  <Badge status={p.clinicalStatus}>{p.clinicalStatus}</Badge>
                ) : null}
              </div>
              <div className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
                {p.code ? (
                  <code className="font-mono">
                    {p.codeSystem ? `${p.codeSystem} ${p.code}` : p.code}
                  </code>
                ) : null}
                {p.onsetDate ? <span>Onset {formatDate(p.onsetDate)}</span> : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
