/**
 * Liveness / readiness endpoint.
 *
 * Plain JSON. Reports the build sha so a deploy can be verified at a
 * glance. Does NOT touch OpenEMR (intentional — `/healthz` should reflect
 * the dashboard's own readiness, not its dependencies).
 */
import { env } from "@/lib/env";

export const dynamic = "force-static";
export const revalidate = false;

export function GET() {
  return Response.json({
    ok: true,
    service: "openemr-patient-dashboard",
    build: env.NEXT_PUBLIC_BUILD_SHA,
    time: new Date().toISOString(),
  });
}
