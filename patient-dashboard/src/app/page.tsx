/**
 * Landing — patient picker.
 *
 * The brief frames the dashboard around a single patient at a time, but
 * doesn't ship a patient list view. Rather than dump the user onto a
 * blank screen, this page offers two ways to get to a chart:
 *
 *   1. Type a Patient resource UUID directly — for clinicians who already
 *      have one in hand (e.g. from the legacy URL bar).
 *   2. The three demo patients seeded in the local OpenEMR for the
 *      Clinical Co-Pilot evals — Barbara (gout), Suzie (osteoporosis),
 *      and the penicillin-allergy demo. Their UUIDs are resolved at
 *      request time via Patient?identifier= so this page works against
 *      a fresh install where the random UUIDs differ from the seed run.
 */
import Link from "next/link";
import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { fhirBase } from "@/lib/env";
import { SignOutButton } from "@/components/sign-out-button";
import { Card, CardHeader } from "@/components/card";

const DEMO_PIDS = [
  { pid: "87413", label: "Barbara Boston (gout)" },
  { pid: "87414", label: "Suzie Sanchez (osteoporosis)" },
  { pid: "87415", label: "Demo Patient (penicillin allergy)" },
];

async function resolveUuidFromPid(pid: string): Promise<string | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  const url = new URL(`Patient?identifier=${encodeURIComponent(pid)}`, fhirBase);
  try {
    const response = await fetch(url, {
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/fhir+json",
      },
      cache: "no-store",
    });
    if (!response.ok) return null;
    const json = (await response.json()) as {
      entry?: Array<{ resource?: { id?: string } }>;
    };
    return json.entry?.[0]?.resource?.id ?? null;
  } catch {
    return null;
  }
}

async function gotoPatient(formData: FormData) {
  "use server";
  const raw = String(formData.get("identifier") ?? "").trim();
  if (!raw) return;
  // Heuristic: a UUID is hex with dashes. A pid is digits.
  const isUuid = /^[0-9a-f-]{8,}$/i.test(raw);
  const uuid = isUuid ? raw : await resolveUuidFromPid(raw);
  if (!uuid) {
    redirect(`/?error=not-found&q=${encodeURIComponent(raw)}`);
  }
  redirect(`/patient/${uuid}`);
}

export default async function LandingPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; q?: string }>;
}) {
  const params = await searchParams;
  const session = await auth();
  if (!session) {
    // Middleware should already have redirected, but belt-and-braces.
    redirect("/login");
  }

  return (
    <main className="mx-auto flex w-full max-w-2xl flex-col gap-6 px-4 py-12">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Patient Dashboard
          </h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Modern reimplementation of the OpenEMR patient view.
          </p>
        </div>
        <SignOutButton />
      </header>

      {params.error === "not-found" ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          No patient found for{" "}
          <code className="font-mono">{params.q}</code>. Check the identifier
          and try again.
        </p>
      ) : null}

      <Card>
        <CardHeader title="Open a chart by ID" />
        <form action={gotoPatient} className="flex flex-col gap-2 sm:flex-row">
          <input
            name="identifier"
            type="text"
            required
            placeholder="Patient UUID or numeric pid"
            className="
              flex-1 rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm
              placeholder:text-zinc-400 focus:border-zinc-500 focus:outline-none
              dark:border-zinc-700 dark:bg-zinc-900 dark:placeholder:text-zinc-500
            "
          />
          <button
            type="submit"
            className="
              rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white
              transition-colors hover:bg-zinc-700
              dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300
            "
          >
            Open
          </button>
        </form>
      </Card>

      <Card>
        <CardHeader title="Demo patients (Clinical Co-Pilot fixtures)" />
        <ul className="flex flex-col gap-2">
          {DEMO_PIDS.map((p) => (
            <li key={p.pid}>
              <Link
                href={`/patient/by-pid/${p.pid}`}
                className="
                  flex items-center justify-between rounded-md
                  bg-zinc-50 px-3 py-2 text-sm
                  transition-colors hover:bg-zinc-100
                  dark:bg-zinc-800/40 dark:hover:bg-zinc-800
                "
              >
                <span>{p.label}</span>
                <span className="font-mono text-xs text-zinc-500">pid {p.pid}</span>
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </main>
  );
}
