/**
 * Auth.js HTTP handlers.
 *
 * Exposes /api/auth/signin, /api/auth/callback/openemr, /api/auth/signout,
 * and the rest of Auth.js's surface. The actual configuration lives in
 * src/auth.ts so it can be imported by middleware and server components
 * without dragging the route handler bindings along.
 */
import { handlers } from "@/auth";

export const { GET, POST } = handlers;
