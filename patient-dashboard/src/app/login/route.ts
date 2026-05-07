/**
 * /login route handler.
 *
 * Replaces the prior `/login/page.tsx` button. Auth.js v5's
 * `signIn()` sets state/PKCE cookies, which Next.js only permits
 * inside Server Actions and Route Handlers — calling it from a
 * server component throws "Cookies can only be modified inside
 * Server Actions or Route Handlers." That's why this is a route
 * handler, not a page.
 *
 * Behavior:
 *
 *   - GET /login (no error)         → signIn("openemr") immediately.
 *                                     The clinician sees one redirect
 *                                     hop, never a button.
 *
 *   - GET /login?error=…            → render a plain HTML error page
 *                                     with the message and a manual
 *                                     "Try again" link. We never
 *                                     auto-loop on errors, otherwise
 *                                     a misconfigured provider would
 *                                     thrash OpenEMR with redirects
 *                                     and bury the underlying cause.
 *
 *   - GET /login?retry=1            → manual retry from the error
 *                                     page; calls signIn() again.
 *
 * `signIn()` throws NEXT_REDIRECT — Next.js's framework catches it
 * and emits the 302. Anything else from the call is a real error.
 */
import { NextRequest } from "next/server";
import { signIn } from "@/auth";

export async function GET(req: NextRequest): Promise<Response> {
  const { searchParams } = new URL(req.url);
  const callbackUrl = searchParams.get("callbackUrl") ?? "/";
  const error = searchParams.get("error");
  const retry = searchParams.get("retry");

  // Error landing — only render manual UI if the user came back with
  // an error AND didn't ask to retry. The retry path bypasses the
  // error UI and loops back into signIn() so the clinician gets one
  // click to recover.
  if (error && retry !== "1") {
    return errorHtmlResponse(error, callbackUrl);
  }

  try {
    await signIn("openemr", { redirectTo: callbackUrl });
  } catch (cause) {
    // NEXT_REDIRECT is how Auth.js signals "I set the cookies and the
    // 302 is in the response." Re-throw so Next.js's handler emits
    // the redirect. Any other error is a real problem.
    if (
      cause &&
      typeof cause === "object" &&
      "digest" in cause &&
      typeof (cause as { digest?: unknown }).digest === "string" &&
      (cause as { digest: string }).digest.startsWith("NEXT_REDIRECT")
    ) {
      throw cause;
    }
    console.error("[login] signIn failed:", cause);
    return errorHtmlResponse("signin_threw", callbackUrl);
  }

  // Unreachable — signIn always throws.
  return new Response(
    "Auth.js signIn() returned without redirecting. Check the openemr " +
      "provider config in src/auth.ts: missing OPENEMR_ISSUER, missing " +
      "OPENEMR_CLIENT_ID/SECRET, or unreachable issuer URL.",
    { status: 500, headers: { "Content-Type": "text/plain" } },
  );
}

function errorText(code: string): string {
  switch (code) {
    case "RefreshAccessTokenError":
      return "Your session expired. Sign in again.";
    case "AccessDenied":
      return "OpenEMR refused the sign-in. Confirm your account has the right scopes.";
    case "Configuration":
      return "Auth.js configuration error. Check the dashboard's .env.local " +
        "(OPENEMR_ISSUER, OPENEMR_CLIENT_ID, OPENEMR_CLIENT_SECRET).";
    case "signin_threw":
      return "Auth.js signIn() threw. Check the dashboard server log " +
        "for the underlying cause; common culprits: OPENEMR_ISSUER " +
        "doesn't match the URL the browser is on, or OpenEMR's authorize " +
        "endpoint is unreachable.";
    default:
      return `Sign-in failed (${code}). Try again, then check the dashboard logs.`;
  }
}

function errorHtmlResponse(code: string, callbackUrl: string): Response {
  const message = errorText(code);
  const retryHref = `/login?retry=1&callbackUrl=${encodeURIComponent(callbackUrl)}`;
  // Plain HTML — no React, no JS — so we don't reintroduce the
  // server-component script-execution issue we were getting from
  // page.tsx's auto-submit form.
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Sign-in failed — Patient Dashboard</title>
  <style>
    body { margin: 0; min-height: 100vh; background: #09090b; color: #fafafa;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, system-ui, sans-serif;
      display: flex; align-items: center; justify-content: center; padding: 1rem; }
    .card { max-width: 28rem; width: 100%; padding: 1.5rem; border-radius: 1rem;
      background: #18181b; border: 1px solid #27272a; }
    h1 { font-size: 1.5rem; font-weight: 600; margin: 0 0 0.5rem; }
    .subtitle { color: #a1a1aa; font-size: 0.875rem; margin: 0 0 1rem; }
    .error { padding: 0.5rem 0.75rem; border-radius: 0.375rem; font-size: 0.875rem;
      background: #450a0a; border: 1px solid #7f1d1d; color: #fecaca; margin-bottom: 1rem; }
    a.retry { display: block; text-align: center; padding: 0.5rem 1rem;
      border-radius: 0.375rem; background: #fafafa; color: #18181b;
      text-decoration: none; font-size: 0.875rem; font-weight: 500; }
    a.retry:hover { background: #d4d4d8; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Patient Dashboard</h1>
    <p class="subtitle">We hit a snag completing the OpenEMR OAuth2 / OpenID Connect
      handshake. Try again — if it keeps failing, check the dashboard
      server logs for the underlying error.</p>
    <div class="error">${escapeHtml(message)}</div>
    <a class="retry" href="${escapeHtml(retryHref)}">Try OpenEMR sign-in again</a>
  </div>
</body>
</html>`;
  return new Response(html, {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
