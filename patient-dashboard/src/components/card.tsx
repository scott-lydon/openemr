/**
 * shadcn-style Card primitive.
 *
 * Tailwind v4 lets us define semantic tokens at the @theme layer and then
 * use them as utilities. The Card uses semantic colours (--card-bg,
 * --card-border) so light/dark mode just works without per-component
 * conditional classes.
 */
import { cn } from "@/lib/ui/cn";
import type { ReactNode } from "react";

type CardProps = {
  className?: string;
  children: ReactNode;
};

export function Card({ className, children }: CardProps) {
  return (
    <div
      className={cn(
        "rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm",
        "dark:border-zinc-800 dark:bg-zinc-900",
        "flex flex-col gap-3",
        className,
      )}
    >
      {children}
    </div>
  );
}

type CardHeaderProps = {
  title: string;
  count?: number | null;
  icon?: ReactNode;
  action?: ReactNode;
};

export function CardHeader({ title, count, icon, action }: CardHeaderProps) {
  return (
    <header className="flex items-center justify-between gap-2">
      <div className="flex items-center gap-2">
        {icon ? (
          <span className="text-zinc-500 dark:text-zinc-400">{icon}</span>
        ) : null}
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-700 dark:text-zinc-300">
          {title}
        </h2>
        {typeof count === "number" ? (
          <span
            className="rounded-full bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
            aria-label={`${count} item${count === 1 ? "" : "s"}`}
          >
            {count}
          </span>
        ) : null}
      </div>
      {action}
    </header>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <p className="text-sm italic text-zinc-500 dark:text-zinc-400">{children}</p>
  );
}
