/**
 * Typed errors for the FHIR layer.
 *
 * Every failure mode is its own class. Catch sites can use instanceof to
 * decide between "show the user a friendly empty state" (NotFound) and
 * "this is a bug, surface it to the dev console" (ValidationFailure /
 * UpstreamFailure). Generic `Error` would erase that distinction.
 */

/**
 * Base class so a top-level boundary can catch every FHIR failure with a
 * single `instanceof FhirError` check.
 */
export abstract class FhirError extends Error {
  constructor(
    message: string,
    public readonly endpoint: string,
    options?: { cause?: unknown },
  ) {
    super(message, options);
    this.name = this.constructor.name;
  }
}

/** OpenEMR returned 404. The resource doesn't exist or scope hides it. */
export class FhirNotFound extends FhirError {
  constructor(endpoint: string) {
    super(`FHIR resource not found at ${endpoint}.`, endpoint);
  }
}

/** OpenEMR returned 401/403. Token is missing, expired, or under-scoped. */
export class FhirAuthFailure extends FhirError {
  constructor(endpoint: string, status: number, body: string) {
    super(
      `FHIR ${endpoint} returned HTTP ${status}: token rejected. ` +
        `Body (truncated): ${body.slice(0, 300)}. ` +
        `Verify the access token has the right user/* scopes and that ` +
        `the OAuth client is enabled in OpenEMR's admin UI.`,
      endpoint,
    );
  }
}

/** Any other 4xx/5xx response from OpenEMR. */
export class FhirUpstreamFailure extends FhirError {
  constructor(
    endpoint: string,
    public readonly status: number,
    body: string,
  ) {
    super(
      `FHIR ${endpoint} returned HTTP ${status}. ` +
        `Body (truncated): ${body.slice(0, 500)}.`,
      endpoint,
    );
  }
}

/** Network or transport-level failure (DNS, TLS, connection reset). */
export class FhirTransportFailure extends FhirError {
  constructor(endpoint: string, cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    super(
      `FHIR ${endpoint} could not be reached (${reason}). ` +
        `Check OPENEMR_FHIR_BASE, network connectivity, and TLS verification.`,
      endpoint,
      { cause },
    );
  }
}

/** Response was 200 but did not match the expected Zod schema. */
export class FhirValidationFailure extends FhirError {
  constructor(
    endpoint: string,
    public readonly zodIssues: readonly {
      path: readonly PropertyKey[];
      message: string;
    }[],
  ) {
    const summary = zodIssues
      .slice(0, 5)
      .map(
        (i) =>
          `${i.path.map((p) => String(p)).join(".") || "(root)"}: ${i.message}`,
      )
      .join("; ");
    super(
      `FHIR ${endpoint} returned a body that did not match the expected schema. ` +
        `First issues: ${summary}. ` +
        `This usually means OpenEMR's mapper version differs from what the ` +
        `dashboard's schemas expect, or the response is degraded (e.g. a ` +
        `Bundle with no entry array).`,
      endpoint,
    );
  }
}

/** Caller asked for a session but the user is not authenticated. */
export class FhirNoSession extends Error {
  constructor() {
    super(
      "Cannot call the FHIR client without an authenticated session. " +
        "Wrap the call in a route or component guarded by auth().",
    );
    this.name = "FhirNoSession";
  }
}
