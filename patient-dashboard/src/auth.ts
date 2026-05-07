/**
 * Auth.js v5 configuration.
 *
 * The dashboard authenticates clinicians via OpenEMR's OAuth2 + OpenID
 * Connect server. Flow: Authorization Code with PKCE, confidential client
 * (the dashboard runs server-side). Auth.js stores the access token in
 * an encrypted JWT cookie session; FHIR fetches read it back via auth().
 *
 * Token lifecycle:
 *   - Access tokens are short-lived (typically 1 hour in OpenEMR's
 *     default config). When expiry approaches we refresh via the
 *     refresh_token grant. Refreshes that fail invalidate the session
 *     and force re-login.
 *   - The `error: "RefreshAccessTokenError"` flag on the session lets
 *     UI surfaces show a friendly "your session expired" prompt rather
 *     than a generic 500.
 *
 * Scope strategy: SMART-on-FHIR `user/*.read` for every resource the
 * dashboard cards consume. `openid profile fhirUser offline_access` for
 * the OIDC + refresh token primitives. We deliberately avoid
 * `patient/*.read` (which constrains to a single patient context) so the
 * clinician can browse multiple patient charts in one session.
 */
import NextAuth, { type Session } from "next-auth";
import type { JWT } from "next-auth/jwt";
import { env } from "@/lib/env";

// Scopes we request on the initial authorize. `openid` + `fhirUser`
// give us OIDC + the user's FHIR identity; `offline_access` gets us a
// refresh token; the `user/*` scopes cover every FHIR resource the
// dashboard cards consume.
//
// `profile` is intentionally OMITTED. OpenEMR's authorize endpoint
// accepts it, but its refresh handler rejects it with
// `invalid_scope: Check the profile scope`, which makes every refresh
// fail. Skipping it costs us nothing — Auth.js doesn't need profile
// claims for the dashboard.
const AUTHORIZE_SCOPES = [
  "openid",
  "fhirUser",
  "offline_access",
  "user/Patient.read",
  "user/Condition.read",
  "user/MedicationRequest.read",
  "user/AllergyIntolerance.read",
  "user/Observation.read",
  "user/Encounter.read",
  "user/CareTeam.read",
  "user/Practitioner.read",
].join(" ");

// Scopes we send on refresh. Subset of AUTHORIZE_SCOPES — only the API
// surface, not the OIDC primitives. `openid`/`fhirUser`/`offline_access`
// are one-shot ceremony scopes and OpenEMR rejects them on refresh.
const REFRESH_SCOPES = [
  "user/Patient.read",
  "user/Condition.read",
  "user/MedicationRequest.read",
  "user/AllergyIntolerance.read",
  "user/Observation.read",
  "user/Encounter.read",
  "user/CareTeam.read",
  "user/Practitioner.read",
].join(" ");

// Backwards-compat alias for the original constant name.
const SCOPES = AUTHORIZE_SCOPES;

type ExtendedJWT = JWT & {
  accessToken?: string;
  refreshToken?: string;
  /** Unix milliseconds when the access token stops working. */
  expiresAt?: number;
  /** Surface a refresh failure to the UI without throwing on every render. */
  error?: "RefreshAccessTokenError";
};

type ExtendedSession = Session & {
  accessToken?: string;
  error?: "RefreshAccessTokenError";
};

async function refreshAccessToken(token: ExtendedJWT): Promise<ExtendedJWT> {
  if (!token.refreshToken) {
    return { ...token, error: "RefreshAccessTokenError" };
  }
  try {
    // Pass `scope` explicitly on refresh.
    //
    // OpenEMR's authorization server stores the OIDC `nonce` parameter
    // alongside the granted scopes during the auth code exchange, then
    // re-validates that combined set as a scope list on refresh and
    // returns `invalid_scope: Check the \`nonce\` scope`. The recovery
    // is to send the original scope list explicitly on the refresh
    // request, which OpenEMR honours and re-issues against. Same string
    // used in the SCOPES constant above so the granted scope shape stays
    // identical between the initial token and every refresh.
    const response = await fetch(`${env.OPENEMR_ISSUER}/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        refresh_token: token.refreshToken,
        client_id: env.OPENEMR_CLIENT_ID,
        client_secret: env.OPENEMR_CLIENT_SECRET,
        scope: REFRESH_SCOPES,
      }),
    });

    if (!response.ok) {
      const body = await response.text();
      console.error(
        `[auth] OpenEMR refresh failed: HTTP ${response.status}. ` +
          `Body (truncated): ${body.slice(0, 300)}.`,
      );
      return { ...token, error: "RefreshAccessTokenError" };
    }

    const refreshed = (await response.json()) as {
      access_token?: string;
      refresh_token?: string;
      expires_in?: number;
    };

    if (!refreshed.access_token) {
      console.error("[auth] OpenEMR refresh returned no access_token.");
      return { ...token, error: "RefreshAccessTokenError" };
    }

    return {
      ...token,
      accessToken: refreshed.access_token,
      refreshToken: refreshed.refresh_token ?? token.refreshToken,
      expiresAt: Date.now() + (refreshed.expires_in ?? 3600) * 1000,
      error: undefined,
    };
  } catch (cause) {
    console.error("[auth] OpenEMR refresh threw:", cause);
    return { ...token, error: "RefreshAccessTokenError" };
  }
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  trustHost: env.AUTH_TRUST_HOST === "1",
  providers: [
    {
      id: "openemr",
      name: "OpenEMR",
      type: "oidc",
      issuer: env.OPENEMR_ISSUER,
      clientId: env.OPENEMR_CLIENT_ID,
      clientSecret: env.OPENEMR_CLIENT_SECRET,
      authorization: {
        params: {
          scope: SCOPES,
          // Silent SSO: when the clinician is already logged in to
          // OpenEMR (the same browser session), OpenEMR returns the
          // auth code without showing a login form. This is the whole
          // point of OIDC inside the OpenEMR shell — clicking the
          // Dashboard tab should NOT bounce the clinician through a
          // second username/password prompt.
          //
          // (If a multi-clinician workstation needs to force a fresh
          // login, the operator can append &prompt=login to the
          // sign-in URL or sign out of OpenEMR itself.)
        },
      },
      // OpenEMR's discovery doc is fine, but checks on `at_hash` and
      // similar require the JWKS endpoint, which is at `/jwk` not the
      // OIDC default `/.well-known/jwks.json`. Auth.js auto-resolves
      // from the issuer's discovery doc, so we don't override here.
      checks: ["pkce", "state", "nonce"],
    },
  ],
  session: { strategy: "jwt" },
  callbacks: {
    async jwt({ token, account }) {
      // First sign-in — copy the OAuth response onto the JWT.
      if (account) {
        return {
          ...token,
          accessToken: account.access_token,
          refreshToken: account.refresh_token,
          expiresAt:
            account.expires_at !== undefined
              ? account.expires_at * 1000
              : Date.now() + 3600 * 1000,
        } as ExtendedJWT;
      }

      const t = token as ExtendedJWT;

      // Token still valid (with 60s leeway).
      if (t.expiresAt && Date.now() < t.expiresAt - 60_000) {
        return t;
      }

      // Refresh.
      return refreshAccessToken(t);
    },
    async session({ session, token }) {
      const t = token as ExtendedJWT;
      const s = session as ExtendedSession;
      s.accessToken = t.accessToken;
      s.error = t.error;
      return s;
    },
  },
  pages: {
    signIn: "/login",
    error: "/login",
  },
});

declare module "next-auth" {
  interface Session {
    accessToken?: string;
    error?: "RefreshAccessTokenError";
  }
}
