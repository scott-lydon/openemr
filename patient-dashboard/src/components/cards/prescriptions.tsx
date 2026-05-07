/**
 * Prescriptions card — the e-prescribed subset of MedicationRequest.
 */
import { ClipboardList } from "lucide-react";
import { Card, CardHeader, EmptyState } from "../card";
import { Badge } from "../badge";
import { fetchPrescriptions } from "@/lib/fhir/client";

export async function PrescriptionsCard({ uuid }: { uuid: string }) {
  const items = await fetchPrescriptions(uuid);
  const sorted = [...items].sort((a, b) => {
    const da = a.authoredOn ? new Date(a.authoredOn).getTime() : 0;
    const db = b.authoredOn ? new Date(b.authoredOn).getTime() : 0;
    return db - da;
  });

  return (
    <Card>
      <CardHeader
        title="Prescriptions"
        count={sorted.length}
        icon={<ClipboardList size={16} />}
      />
      {sorted.length === 0 ? (
        <EmptyState>No active prescriptions.</EmptyState>
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
                {p.status ? <Badge status={p.status}>{p.status}</Badge> : null}
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-600 dark:text-zinc-400">
                {p.dosage ? <span>{p.dosage}</span> : null}
                {p.authoredOn ? (
                  <span>Authored {formatDate(p.authoredOn)}</span>
                ) : null}
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
