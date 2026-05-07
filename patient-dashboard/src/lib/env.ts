/**
 * Validated environment configuration.
 *
 * Reading process.env directly is a footgun: a typo in a key name returns
 * undefined silently. This module parses the env once on first import and
 * throws a precise, actionable error if anything required is missing or
 * malformed. Every downstream module imports the validated `env` object,
 * never process.env.
 *
 * The validation runs on the server only. Client components that need a
 * value must use a NEXT_PUBLIC_-prefixed variable, which Next inlines at
 * build time.
 */
import { z } from "zod";

const Schema = z.object({
  // ---- OpenEMR OIDC ----
  OPENEMR_ISSUER: z
    .string()
    .url(
      "OPENEMR_ISSUER must be a full URL ending with /oauth2/default " +
        "(e.g. http://localhost:8300/oauth2/default).",
    ),
  OPENEMR_CLIENT_ID: z
    .string()
    .min(
      1,
      "OPENEMR_CLIENT_ID is empty. Run scripts/register-oauth-client.sh " +
        "to provision a client and write the value into .env.local.",
    ),
  OPENEMR_CLIENT_SECRET: z
    .string()
    .min(
      1,
      "OPENEMR_CLIENT_SECRET is empty. Public clients are not supported " +
        "for this dashboard; provision a confidential client.",
    ),

  // ---- FHIR ----
  OPENEMR_FHIR_BASE: z
    .string()
    .url("OPENEMR_FHIR_BASE must be a full URL (no trailing slash needed)."),
  OPENEMR_FHIR_VERIFY_SSL: z
    .enum(["0", "1"], {
      message: "OPENEMR_FHIR_VERIFY_SSL must be exactly '0' or '1'.",
    })
    .default("1"),

  // ---- Auth.js ----
  AUTH_SECRET: z
    .string()
    .min(
      32,
      "AUTH_SECRET must be at least 32 characters. Generate with " +
        "`openssl rand -hex 32`.",
    ),
  AUTH_URL: z.string().url("AUTH_URL must be a full URL.").optional(),
  AUTH_TRUST_HOST: z.enum(["0", "1"]).default("0"),

  // ---- Diagnostic ----
  NEXT_PUBLIC_BUILD_SHA: z.string().default("dev"),
});

function loadEnv() {
  const parsed = Schema.safeParse(process.env);
  if (parsed.success) return parsed.data;
  // Format every issue as a clear, actionable line. Without this, Next.js
  // surfaces a single ZodError dump that's impossible to debug at a glance.
  const issues = parsed.error.issues
    .map((i) => `  • ${i.path.join(".") || "(root)"}: ${i.message}`)
    .join("\n");
  throw new Error(
    `Patient Dashboard environment is invalid:\n${issues}\n\n` +
      `Fix .env.local (or your deployment env) and restart.`,
  );
}

export const env = loadEnv();

export const fhirBase = env.OPENEMR_FHIR_BASE.replace(/\/+$/, "") + "/";
export const fhirVerifyTls = env.OPENEMR_FHIR_VERIFY_SSL === "1";
