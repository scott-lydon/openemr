/**
 * Patient page error boundary.
 *
 * Catches any error not handled inside an individual card's Suspense
 * boundary — most commonly a failure to resolve the patient header
 * itself (the first thing the page awaits). Shows a friendly screen
 * with a Reset button.
 */
"use client";

import { AlertTriangle } from "lucide-react";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main className="mx-auto flex max-w-xl flex-col items-center gap-4 px-4 py-16 text-center">
      <AlertTriangle size={32} className="text-red-500" />
      <h1 className="text-xl font-semibold">Could not load this patient</h1>
      <p className="text-sm text-zinc-700 dark:text-zinc-300">{error.message}</p>
      {error.digest ? (
        <p className="text-xs text-zinc-500 dark:text-zinc-500">
          Error ref: <code className="font-mono">{error.digest}</code>
        </p>
      ) : null}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={reset}
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm text-white dark:bg-zinc-100 dark:text-zinc-900"
        >
          Retry
        </button>
        <a
          href="/"
          className="rounded-md border border-zinc-300 px-4 py-2 text-sm dark:border-zinc-700"
        >
          Back to picker
        </a>
      </div>
    </main>
  );
}
