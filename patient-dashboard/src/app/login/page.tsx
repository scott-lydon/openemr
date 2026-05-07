/**
 * Login page.
 *
 * Behavior splits on whether the request landed here with an `?error=`
 * query parameter:
 *
 *   - **No error** (the common path): the page renders a tiny
 *     auto-submitting form that immediately POSTs to the OAuth code
 *     + PKCE start endpoint. The user never sees a button. When the
 *     clinician is already logged in to OpenEMR (same browser session)
 *     and `prompt=login` is absent from the authorize params, OpenEMR
 *     silently issues an authorization code, the dashboard exchanges
 *     it, and the user lands on `callbackUrl` without typing
 *     credentials a second time. This is the assignment-required
 *     OAuth2/OIDC login path; we just remove the manual click.
 *
 *   - **With `?error=`** (refresh failure, denied, misconfig):
 *     render a friendly explanation plus a manual "Try again" button.
 *     We never auto-loop on errors — repeated silent retries would
 *     thrash OpenEMR and hide the underlying problem.
 *
 * Why server-action signIn over a client-side button: keeps the OAuth
 * client_id off the client bundle entirely. The server action calls
 * Auth.js's redirect URL builder.
 *
 * Auto-submit mechanism: a hidden `<form>` with the same server action
 * the manual button would post to, and a small inline `<script>` that
 * calls `requestSubmit()` on it as soon as the page hydrates. Form
 * submission carries Next.js's server-action token automatically. JS-
 * disabled clients fall through to a `<noscript>` fallback button.
 */
import { signIn } from "@/auth";

const AUTO_SIGNIN_FORM_ID = "auto-oidc-signin";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ callbackUrl?: string; error?: string }>;
}) {
  const params = await searchParams;
  const callbackUrl = params.callbackUrl ?? "/";
  const errorMessage = errorText(params.error);

  if (!errorMessage) {
    // Silent OIDC start. The user sees a brief "Signing you in…" frame
    // before OpenEMR's authorize endpoint returns the auth code and
    // Auth.js exchanges + redirects to callbackUrl.
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-4">
        <div className="flex w-full flex-col gap-3 rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Signing you in via OpenEMR…
          </p>
          <form
            id={AUTO_SIGNIN_FORM_ID}
            action={async () => {
              "use server";
              await signIn("openemr", { redirectTo: callbackUrl });
            }}
          >
            <noscript>
              <button
                type="submit"
                className="
                  w-full rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white
                  transition-colors hover:bg-zinc-700
                  dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300
                "
              >
                Continue to OpenEMR
              </button>
            </noscript>
          </form>
          <script
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{
              __html: `
                (function () {
                  var form = document.getElementById(${JSON.stringify(AUTO_SIGNIN_FORM_ID)});
                  if (!form) {
                    console.error('[login] auto-OIDC submit failed: form not found.');
                    return;
                  }
                  if (typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                  } else {
                    form.submit();
                  }
                })();
              `,
            }}
          />
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-6 px-4">
      <div className="flex w-full flex-col gap-3 rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <h1 className="text-2xl font-semibold tracking-tight">
          Patient Dashboard
        </h1>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          We hit a snag completing the OpenEMR OAuth2 / OpenID Connect
          handshake. Try again — if it keeps failing, check the dashboard
          server logs for the underlying error.
        </p>

        <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
          {errorMessage}
        </p>

        <form
          action={async () => {
            "use server";
            await signIn("openemr", { redirectTo: callbackUrl });
          }}
        >
          <button
            type="submit"
            className="
              w-full rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white
              transition-colors hover:bg-zinc-700
              dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300
            "
          >
            Try OpenEMR sign-in again
          </button>
        </form>
      </div>
    </main>
  );
}

function errorText(code?: string): string | null {
  if (!code) return null;
  switch (code) {
    case "RefreshAccessTokenError":
      return "Your session expired. Sign in again.";
    case "AccessDenied":
      return "OpenEMR refused the sign-in. Confirm your account has the right scopes.";
    case "Configuration":
      return "Auth.js configuration error. Check the dashboard's .env.local.";
    default:
      return `Sign-in failed (${code}). Try again, then check the dashboard logs.`;
  }
}
