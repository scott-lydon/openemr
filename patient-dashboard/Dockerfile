# syntax=docker/dockerfile:1.7
#
# Multi-stage Dockerfile for the OpenEMR Patient Dashboard.
#
# Stage 1 (deps)    — install npm dependencies with full devDependencies so
#                     the Next.js compiler can run.
# Stage 2 (builder) — copy source and run `next build`. Output is the
#                     standalone server in .next/standalone plus static
#                     assets in .next/static.
# Stage 3 (runtime) — Alpine Node with ONLY the standalone output. Runs as
#                     non-root. ~120 MB final image.
#
# Build:
#   docker build -t openemr-patient-dashboard:dev .
# Run (joined to OpenEMR docker network):
#   docker compose up -d patient-dashboard
#
# next.config.ts must contain `output: "standalone"` for stage 2 to produce
# the standalone bundle. The build will fail with a clear "no standalone
# directory" error if that flag is missing.

# ---------- Stage 1: deps ----------
FROM node:22-alpine AS deps
WORKDIR /app

# Copy only the manifests so the dep layer caches across source changes.
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

# ---------- Stage 2: builder ----------
FROM node:22-alpine AS builder
WORKDIR /app

# Reuse the dep layer from stage 1.
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Build-time env: bake the git sha into the bundle so /healthz reports it.
ARG BUILD_SHA=unknown
ENV NEXT_PUBLIC_BUILD_SHA=$BUILD_SHA

# Disable Next.js telemetry inside the build container.
ENV NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# Verify the standalone output exists. If it doesn't, next.config.ts is
# missing `output: "standalone"` and the runtime stage will fail to start
# with a confusing error. Fail here instead with an actionable message.
RUN test -d .next/standalone || ( \
    echo "ERROR: .next/standalone not produced by `next build`." >&2 ; \
    echo "Set `output: \"standalone\"` in next.config.ts and rebuild." >&2 ; \
    exit 1 )

# ---------- Stage 3: runtime ----------
FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=8400
ENV HOSTNAME=0.0.0.0

# Run as the prebuilt non-root `node` user.
USER node

# Copy ONLY what the standalone bundle needs.
COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static

EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8400/healthz || exit 1

CMD ["node", "server.js"]
