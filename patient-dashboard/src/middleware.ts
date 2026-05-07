/**
 * Auth middleware.
 *
 * Every request other than /login, /api/auth/*, /healthz, and Next.js
 * static assets requires an authenticated session. Auth.js redirects
 * unauthenticated users to /login automatically.
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

  if (!req.auth && !isPublic) {
    const url = new URL("/login", req.url);
    url.searchParams.set("callbackUrl", req.nextUrl.pathname + req.nextUrl.search);
    return Response.redirect(url);
  }
});

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\..*).*)"],
};
