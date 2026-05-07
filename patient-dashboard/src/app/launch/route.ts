/**
 * SMART-on-FHIR EHR launch endpoint.
 *
 * This is the silent in-EMR entry point. When the clinician clicks the
 * Dashboard tab inside OpenEMR, the OpenEMR-side `demographics.php`
 * mints a SMART launch token bound to the active patient context and
 * redirects the iframe here with `?iss=&launch=&pid=`.
 *
 *   1. We initiate an OAuth2/OpenID Connect Authorization Code + PKCE
 *      flow against the issuer, passing the SMART `launch` param and
 *      the `aud` (the issuer's FHIR base URL) and a scope set that
 *      includes `launch` — together those signal an EHR-launch context
 *      to the authorization server.
 *
 *   2. Because (a) the OAuth client has `skip_ehr_launch_authorization_flow=1`,
 *      (b) the global `oauth_ehr_launch_authorization_flow_skip=1` is on,
 *      and (c) the clinician's CORE_SESSION_ID cookie is sent along
 *      (same origin as the authorize endpoint), OpenEMR's
 *      `AuthorizationController::oauthAuthorizationFlow` takes the silent
 *      path: it skips both the OAuth provider login form and the scope
 *      consent screen, and issues an authorization code immediately.
 *
 *   3. Auth.js handles the callback at /api/auth/callback/openemr,
 *      exchanges the code for tokens, and lands the user at
 *      `/patient/by-pid/{pid}`.
 *
 * Direct-link visitors who hit the dashboard root URL never go through
 * here — they get the standard `signIn` flow via /login (which also
 * uses OAuth2/OIDC, just without the SMART launch context). That path
 * remains the assignment-required default login mechanism.
 *
 * Errors here are intentionally specific so the cause is obvious from
 * the URL or one log line. Generic 500s would force a console-rummage
 * to figure out which param is missing.
 *
 * @package   OpenEMR Patient Dashboard
 */
import { NextRequest } from "next/server";
import { signIn } from "@/auth";

const DASHBOARD_LAUNCH_SCOPES = [
  // SMART EHR launch primitives — REQUIRED for the silent skip path.
  // Removing `launch` here would make OpenEMR drop the launch token
  // and fall back to the consent screen.
  "launch",
  // Standard OIDC primitives. `profile` is intentionally OMITTED —
  // see auth.ts for the reason (OpenEMR rejects it on refresh).
  "openid",
  "fhirUser",
  "offline_access",
  // FHIR resource scopes the dashboard cards consume.
  "user/Patient.read",
  "user/Condition.read",
  "user/MedicationRequest.read",
  "user/AllergyIntolerance.read",
  "user/Observation.read",
  "user/Encounter.read",
  "user/CareTeam.read",
  "user/Practitioner.read",
].join(" ");

export async function GET(req: NextRequest): Promise<Response> {
  const { searchParams } = new URL(req.url);
  const launch = searchParams.get("launch");
  const iss = searchParams.get("iss");
  const pid = searchParams.get("pid");

  // Be loud about missing params. SMART EHR launch is a 3-param
  // contract; if one is missing the failure mode is silent on the
  // OAuth side (looks like a normal "consent screen showed up") and
  // very confusing to debug. Surface the omission inline.
  if (!launch) {
    return errorResponse(
      400,
      "missing_launch_param",
      "SMART EHR launch requires a `launch` query parameter. " +
        "OpenEMR's demographics.php should be minting one via SMARTLaunchToken; " +
        "if it isn't, check that patient_dashboard_modern_url is set and " +
        "demographics.php's launch-token branch is reachable.",
    );
  }
  if (!iss) {
    return errorResponse(
      400,
      "missing_iss_param",
      "SMART EHR launch requires an `iss` query parameter (the issuer's " +
        "FHIR base URL). OpenEMR's demographics.php should be passing " +
        "ServerConfig::getFhirUrl() as iss.",
    );
  }

  // After OAuth completes, route to the patient's dashboard. The pid
  // → FHIR uuid mapping is handled by /patient/by-pid/[pid]/page.tsx
  // — that route already exists for the legacy non-launch entry
  // point and we reuse it intact.
  const callbackUrl = pid ? `/patient/by-pid/${encodeURIComponent(pid)}` : "/";

  // signIn()'s third positional argument is `authorizationParams` —
  // these are merged into the OAuth /authorize URL. We override the
  // scope (to include `launch`) and add the EHR-launch primitives:
  //
  //   - `launch=<opaque-token>`: passed back to OpenEMR; it
  //     deserializes the SMARTLaunchToken on the server side to
  //     recover the patient/encounter context.
  //
  //   - `aud=<fhir-base-url>`: the SMART-on-FHIR spec requires the
  //     `aud` to match the FHIR resource server the access token is
  //     intended for. Without it, OpenEMR's IdTokenSMARTResponse logs
  //     a warning and downstream FHIR requests can be rejected.
  //
  // signIn() throws NEXT_REDIRECT on success. The line below the call
  // is unreachable unless signIn is misconfigured.
  try {
    await signIn(
      "openemr",
      { redirectTo: callbackUrl },
      {
        scope: DASHBOARD_LAUNCH_SCOPES,
        launch,
        aud: iss,
      },
    );
  } catch (cause) {
    // NEXT_REDIRECT is how Auth.js signals "I set the cookies and the
    // 302 is in the response." Re-throw so Next.js's framework
    // handler emits the redirect. Anything else is a real error.
    if (
      cause &&
      typeof cause === "object" &&
      "digest" in cause &&
      typeof (cause as { digest?: unknown }).digest === "string" &&
      (cause as { digest: string }).digest.startsWith("NEXT_REDIRECT")
    ) {
      throw cause;
    }
    console.error("[launch] signIn failed:", cause);
    return errorResponse(
      500,
      "signin_failed",
      "Auth.js signIn() threw an unexpected error. Check the dashboard " +
        "server logs for the underlying cause; common culprits: OPENEMR_ISSUER " +
        "doesn't match the URL the browser is on (cookies don't follow), the " +
        "OAuth client lacks the `launch` scope on its allowlist, or the " +
        "global oauth_ehr_launch_authorization_flow_skip is unset.",
    );
  }

  // Unreachable. signIn always throws.
  return errorResponse(
    500,
    "unreachable",
    "Auth.js signIn() returned without redirecting. This means it was " +
      "unable to construct the authorize URL — verify the openemr " +
      "provider config in src/auth.ts.",
  );
}

function errorResponse(
  status: number,
  code: string,
  message: string,
): Response {
  return new Response(
    JSON.stringify({ error: code, message }, null, 2),
    {
      status,
      headers: { "Content-Type": "application/json" },
    },
  );
}
