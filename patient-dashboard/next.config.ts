import type { NextConfig } from "next";
import path from "node:path";

/**
 * Next.js configuration for the OpenEMR Patient Dashboard.
 *
 * `output: "standalone"` is REQUIRED — the Dockerfile copies `.next/standalone`
 * into the runtime image. If you remove it, the runtime container will fail
 * to start with a confusing error about a missing `server.js`.
 *
 * `outputFileTracingRoot` is set to the patient-dashboard directory because
 * the parent OpenEMR repository has its own package.json (for the legacy
 * gulp build). Without this, Next walks up to that package.json and
 * produces a nested standalone output at .next/standalone/patient-dashboard/
 * that breaks the Dockerfile's `node server.js` invocation.
 */
const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: path.join(__dirname),
  reactStrictMode: true,

  // The dashboard runs against an OpenEMR instance with a self-signed
  // certificate in dev. Server-side fetch() needs to know about that. The
  // FHIR client honours OPENEMR_FHIR_VERIFY_SSL and uses an explicit
  // `dispatcher` rather than disabling TLS globally.
  experimental: {
    // Allow Server Actions from the deployed origin (port 8400) AND the
    // local dev origin so we don't get cross-origin warnings under docker.
    serverActions: {
      allowedOrigins: ["localhost:8400", "patient-dashboard:8400"],
    },
  },
};

export default nextConfig;
