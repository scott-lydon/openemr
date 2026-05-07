/**
 * Per-card error display.
 *
 * If a single shard's FHIR fetch fails, we want the rest of the dashboard
 * to keep rendering. The dashboard page wraps each card in <Suspense> +
 * an error boundary that delegates to this component. Showing the
 * error.message inline is safe because the FhirError classes scrub
 * sensitive fields before they reach the user.
 */
import { AlertTriangle } from "lucide-react";
import { Card, CardHeader } from "./card";

export function CardError({
  title,
  message,
}: {
  title: string;
  message: string;
}) {
  return (
    <Card className="border-red-200 dark:border-red-900">
      <CardHeader
        title={title}
        icon={<AlertTriangle size={16} className="text-red-500" />}
      />
      <p className="text-sm text-red-700 dark:text-red-300">{message}</p>
    </Card>
  );
}
