/**
 * pid → uuid resolver redirect.
 *
 * Convenience route for jumping to a patient by their legacy numeric
 * pid (e.g. from the Co-Pilot fixtures). Resolves via FHIR
 * `Patient?identifier=` and 302s to the canonical UUID URL.
 */
import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { fhirBase } from "@/lib/env";

export default async function ByPidPage({
  params,
}: {
  params: Promise<{ pid: string }>;
}) {
  const { pid } = await params;
  const session = await auth();
  if (!session?.accessToken) {
    redirect(`/login?callbackUrl=/patient/by-pid/${pid}`);
  }

  const url = new URL(
    `Patient?identifier=${encodeURIComponent(pid)}`,
    fhirBase,
  );
  const response = await fetch(url, {
    headers: {
      Authorization: `Bearer ${session.accessToken}`,
      Accept: "application/fhir+json",
    },
    cache: "no-store",
  });

  if (!response.ok) {
    redirect(`/?error=not-found&q=${encodeURIComponent(pid)}`);
  }

  const body = (await response.json()) as {
    entry?: Array<{ resource?: { id?: string } }>;
  };
  const uuid = body.entry?.[0]?.resource?.id;
  if (!uuid) {
    redirect(`/?error=not-found&q=${encodeURIComponent(pid)}`);
  }

  redirect(`/patient/${uuid}`);
}
