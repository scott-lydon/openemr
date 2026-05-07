/**
 * Streaming placeholder.
 *
 * Each clinical card is a Suspense boundary. While its FHIR fetch is in
 * flight, this skeleton shows a pulsing block that matches the rough
 * shape of the card's eventual content. That makes the streaming UX feel
 * intentional rather than broken.
 */
import { Card, CardHeader } from "./card";

export function CardSkeleton({
  title,
  rows = 3,
}: {
  title: string;
  rows?: number;
}) {
  return (
    <Card>
      <CardHeader title={title} />
      <ul className="flex flex-col gap-2">
        {Array.from({ length: rows }).map((_, i) => (
          <li
            key={i}
            className="h-4 animate-pulse rounded bg-zinc-200 dark:bg-zinc-800"
            style={{ width: `${60 + ((i * 13) % 35)}%` }}
            aria-hidden
          />
        ))}
      </ul>
      <span className="sr-only">Loading {title}…</span>
    </Card>
  );
}
