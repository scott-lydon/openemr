/**
 * Care Team card.
 */
import { Users } from "lucide-react";
import { Card, CardHeader, EmptyState } from "../card";
import { Badge } from "../badge";
import { fetchCareTeam } from "@/lib/fhir/client";

export async function CareTeamCard({ uuid }: { uuid: string }) {
  const members = await fetchCareTeam(uuid);
  return (
    <Card>
      <CardHeader
        title="Care Team"
        count={members.length}
        icon={<Users size={16} />}
      />
      {members.length === 0 ? (
        <EmptyState>No care team members on file.</EmptyState>
      ) : (
        <ul className="flex flex-col gap-2">
          {members.map((m) => (
            <li
              key={m.id}
              className="flex flex-col gap-0.5 rounded-md bg-zinc-50 px-3 py-2 dark:bg-zinc-800/40"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                  {m.name}
                </span>
                {m.status ? <Badge status={m.status}>{m.status}</Badge> : null}
              </div>
              {m.roles.length > 0 ? (
                <p className="text-xs text-zinc-600 dark:text-zinc-400">
                  {m.roles.join(", ")}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
