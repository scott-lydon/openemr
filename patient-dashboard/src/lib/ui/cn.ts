/**
 * Tailwind className helper.
 *
 * `clsx` handles conditional class composition; `tailwind-merge` resolves
 * conflicting utility classes (so a child can override a parent's `p-4`
 * with `p-6` without writing manual !important rules).
 */
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
