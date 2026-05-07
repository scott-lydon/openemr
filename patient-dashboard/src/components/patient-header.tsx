/**
 * Persistent identity bar.
 *
 * Mirrors the legacy OpenEMR header's data fields:
 *   • Name
 *   • Date of Birth (and computed age)
 *   • Sex
 *   • MRN
 *   • Active status
 *
 * Layout is intentionally horizontal at desktop widths so the header takes
 * one row, freeing every pixel below for the clinical cards. On narrow
 * viewports the fields stack into a 2-column grid so MRN never wraps off
 * screen.
 */
import { Badge } from "./badge";
import type { PatientHeader as PatientHeaderData } from "@/lib/fhir/types";

function ageFromBirthDate(iso: string | null): number | null {
  if (!iso) return null;
  const dob = new Date(iso);
  if (Number.isNaN(dob.getTime())) return null;
  const now = new Date();
  let age = now.getFullYear() - dob.getFullYear();
  const m = now.getMonth() - dob.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < dob.getDate())) age -= 1;
  return age;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function Field({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
        {label}
      </span>
      <span className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
        {value}
      </span>
    </div>
  );
}

export function PatientHeaderBar({ patient }: { patient: PatientHeaderData }) {
  const age = ageFromBirthDate(patient.birthDate);
  return (
    <header
      className="
        sticky top-0 z-20
        border-b border-zinc-200 bg-white/90 backdrop-blur
        dark:border-zinc-800 dark:bg-zinc-950/90
      "
      aria-label="Patient identity"
    >
      <div className="mx-auto flex max-w-screen-2xl flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-baseline gap-3">
          <h1 className="text-xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
            {patient.fullName}
          </h1>
          {patient.active === false ? (
            <Badge status="false">Inactive</Badge>
          ) : (
            <Badge status="true">Active</Badge>
          )}
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:flex sm:items-center sm:gap-6">
          <Field
            label="Date of Birth"
            value={
              age !== null
                ? `${formatDate(patient.birthDate)} (${age}y)`
                : formatDate(patient.birthDate)
            }
          />
          <Field
            label="Sex"
            value={patient.sex ? capitalise(patient.sex) : "—"}
          />
          <Field label="MRN" value={patient.mrn ?? "—"} />
          <Field
            label="Patient ID"
            value={
              <code className="font-mono text-xs">{shortUuid(patient.uuid)}</code>
            }
          />
        </div>
      </div>
    </header>
  );
}

function capitalise(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function shortUuid(u: string): string {
  // OpenEMR FHIR resource UUIDs are long; show the first 8 chars in the
  // header and the full value via the title attribute on hover.
  return u.length > 12 ? `${u.slice(0, 8)}…` : u;
}
