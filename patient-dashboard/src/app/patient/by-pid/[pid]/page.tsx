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
  // Either no session, or Auth.js failed to refresh the token at some
  // point and the session is in a broken state. In both cases, force a
  // fresh OAuth round-trip so we get a working access token before
  // hitting FHIR. Without this branch the user lands on the "no patient
  // found" path for what is really an auth problem, which is misleading.
  if (!session?.accessToken || session.error === "RefreshAccessTokenError") {
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

  // 401/403 means the token is stale or under-scoped. Send the user
  // through OAuth again rather than telling them the patient doesn't
  // exist.
  if (response.status === 401 || response.status === 403) {
    redirect(`/login?callbackUrl=/patient/by-pid/${pid}`);
  }

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
