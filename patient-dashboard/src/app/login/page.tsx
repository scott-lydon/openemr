/**
 * Login page.
 *
 * Single button kicks off the OAuth code + PKCE flow against OpenEMR.
 * The /login URL is also where Auth.js sends the user on a session
 * error (refresh failure, signed out), with `?error=` set.
 *
 * Why server-action signIn over a client-side button: keeps the OAuth
 * client_id off the client bundle entirely. The server action calls
 * Auth.js's redirect URL builder.
 */
import { signIn } from "@/auth";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ callbackUrl?: string; error?: string }>;
}) {
  const params = await searchParams;
  const callbackUrl = params.callbackUrl ?? "/";
  const errorMessage = errorText(params.error);

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-6 px-4">
      <div className="flex w-full flex-col gap-3 rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <h1 className="text-2xl font-semibold tracking-tight">
          Patient Dashboard
        </h1>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          Sign in with your OpenEMR clinician credentials. Authentication
          uses OAuth2 with PKCE plus OpenID Connect.
        </p>

        {errorMessage ? (
          <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
            {errorMessage}
          </p>
        ) : null}

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
            Sign in with OpenEMR
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
