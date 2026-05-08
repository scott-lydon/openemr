/**
 * Auth middleware.
 *
 * Every request other than the public auth surfaces and Next.js static
 * assets requires an authenticated session. Auth.js redirects
 * unauthenticated users to /login automatically.
 *
 * Public paths:
 *   /login        — route handler that calls signIn() to start the
 *                   OAuth2/OIDC dance. Replaces the manual
 *                   "Sign in with OpenEMR" button. Assignment-required
 *                   default authentication entry point.
 *   /api/auth/*   — Auth.js's own routes (callback, csrf, providers).
 *   /healthz      — liveness probe; no auth needed.
 *
 * The matcher excludes static assets so we don't pay the auth round-trip
 * on every CSS file. The `_next/image` and `_next/static` paths are
 * already handled by Next; we whitelist the rest below.
 */
import { auth } from "@/auth";

export default auth((req) => {
  const { pathname } = req.nextUrl;
  const isPublic =
    pathname.startsWith("/login") ||
    pathname.startsWith("/api/auth") ||
    pathname.startsWith("/healthz");

  if (isPublic) return;

  // Either no session, or the JWT carries a refresh-failure flag from a
  // previous turn. In both cases the user can't do anything meaningful;
  // bounce them through OAuth so the next request has a working session.
  // Without the second branch a stale session keeps the user logged in
  // but every FHIR call 401s, surfacing as confusing "no patient found"
  // / "could not load" errors deep in the UI.
  const session = req.auth as
    | { error?: "RefreshAccessTokenError" }
    | null
    | undefined;
  if (!session || session.error === "RefreshAccessTokenError") {
    const url = new URL("/login", req.url);
    url.searchParams.set("callbackUrl", req.nextUrl.pathname + req.nextUrl.search);
    return Response.redirect(url);
  }
});

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\..*).*)"],
};
