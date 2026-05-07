/**
 * Sign-out button.
 *
 * Server action via Auth.js's `signOut()`. Posts to the Auth.js endpoint
 * which clears the session cookie and redirects.
 */
import { signOut } from "@/auth";
import { LogOut } from "lucide-react";

export function SignOutButton() {
  return (
    <form
      action={async () => {
        "use server";
        await signOut({ redirectTo: "/login" });
      }}
    >
      <button
        type="submit"
        className="
          inline-flex items-center gap-1 rounded-md
          bg-zinc-100 px-3 py-1.5 text-xs font-medium text-zinc-700
          transition-colors hover:bg-zinc-200
          dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700
        "
      >
        <LogOut size={14} />
        Sign out
      </button>
    </form>
  );
}
