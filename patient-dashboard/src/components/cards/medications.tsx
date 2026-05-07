/**
 * Medications card — every MedicationRequest the chart carries.
 *
 * The brief calls Medications and Prescriptions out separately. The
 * distinction OpenEMR uses is `MedicationRequest.intent`:
 *   - "order" / "original-order" — actually e-prescribed (Prescriptions)
 *   - "plan" / "proposal" / etc. — problem-list-style "patient is on this"
 *
 * This card shows all medications the chart knows about, with a small
 * label tagging which subset each falls into.
 */
import { Pill } from "lucide-react";
import { Card, CardHeader, EmptyState } from "../card";
import { Badge } from "../badge";
import { fetchMedications } from "@/lib/fhir/client";

export async function MedicationsCard({ uuid }: { uuid: string }) {
  const items = await fetchMedications(uuid);
  const sorted = [...items].sort((a, b) => {
    const da = a.authoredOn ? new Date(a.authoredOn).getTime() : 0;
    const db = b.authoredOn ? new Date(b.authoredOn).getTime() : 0;
    return db - da;
  });

  return (
    <Card>
      <CardHeader
        title="Medications"
        count={sorted.length}
        icon={<Pill size={16} />}
      />
      {sorted.length === 0 ? (
        <EmptyState>No medications recorded.</EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {sorted.map((m) => (
            <li
              key={m.id}
              className="flex flex-col gap-1 rounded-md bg-zinc-50 px-3 py-2 dark:bg-zinc-800/40"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                  {m.label}
                </span>
                <div className="flex flex-shrink-0 items-center gap-1">
                  {m.status ? <Badge status={m.status}>{m.status}</Badge> : null}
                  {m.intent === "order" || m.intent === "original-order" ? (
                    <Badge>e-prescribed</Badge>
                  ) : null}
                </div>
              </div>
              {m.dosage ? (
                <p className="text-xs text-zinc-600 dark:text-zinc-400">
                  {m.dosage}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
