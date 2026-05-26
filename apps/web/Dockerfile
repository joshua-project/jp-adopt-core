# syntax=docker/dockerfile:1.7
#
# apps/web — Next.js 15 standalone build.
#
# Three-stage:
#   1. deps  — install workspace deps with frozen lockfile
#   2. build — run `pnpm --filter web build` to produce .next/standalone
#   3. run   — slim Node image with just the standalone bundle
#
# Build context: the repo root (Next reads packages/contracts via the
# pnpm workspace). Build by hand with:
#   docker build -f apps/web/Dockerfile -t jp-adopt-web .
#
# NEXT_PUBLIC_* env vars are baked at build time. The compose file
# passes them as --build-arg; the GitHub deploy workflow reads them
# from repo `vars` and forwards. Dev defaults assume the API on
# http://127.0.0.1:8000 — change with --build-arg NEXT_PUBLIC_API_URL.

# API_PROXY_TARGET is the INTERNAL container-app FQDN of the API. Next.js
# evaluates next.config.ts `rewrites()` at BUILD time and freezes the result
# into the standalone server, so this must be present during `next build`
# (a runtime container env var is NOT read by the baked server). The deploy
# workflow resolves the API's internal FQDN via `az` and passes it here.
ARG API_PROXY_TARGET=
ARG NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
ARG NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID=
ARG NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME=
ARG NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID=
ARG NEXT_PUBLIC_AZURE_AD_B2C_POLICY=B2C_1_signupsignin1
ARG NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES=
ARG NEXT_PUBLIC_AZURE_AD_B2C_KNOWN_AUTHORITIES=

# ─── Stage 1: deps ────────────────────────────────────────────────────────
FROM node:20-alpine AS deps
RUN apk add --no-cache libc6-compat
RUN corepack enable && corepack prepare pnpm@9.0.0 --activate

WORKDIR /build

# Workspace + lockfile + per-package manifests for a cached install
# layer that survives source-only edits.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/package.json
COPY packages/contracts/package.json ./packages/contracts/package.json

# Add `--mount=type=cache,id=pnpm,target=/root/.local/share/pnpm/store`
# when building with BuildKit + buildx for faster reinstalls. Omitted by
# default so the Dockerfile builds on stock Docker.
RUN pnpm install --frozen-lockfile

# ─── Stage 2: build ───────────────────────────────────────────────────────
FROM node:20-alpine AS build
RUN corepack enable && corepack prepare pnpm@9.0.0 --activate

WORKDIR /build

COPY --from=deps /build/node_modules ./node_modules
COPY --from=deps /build/apps/web/node_modules ./apps/web/node_modules
COPY --from=deps /build/packages/contracts/node_modules ./packages/contracts/node_modules

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web ./apps/web
COPY packages/contracts ./packages/contracts

# Re-import build args for this stage (Docker scopes ARG per stage).
ARG API_PROXY_TARGET
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID
ARG NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME
ARG NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID
ARG NEXT_PUBLIC_AZURE_AD_B2C_POLICY
ARG NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES
ARG NEXT_PUBLIC_AZURE_AD_B2C_KNOWN_AUTHORITIES

ENV NEXT_TELEMETRY_DISABLED=1 \
    API_PROXY_TARGET=${API_PROXY_TARGET} \
    NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID=${NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID} \
    NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME=${NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME} \
    NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID=${NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID} \
    NEXT_PUBLIC_AZURE_AD_B2C_POLICY=${NEXT_PUBLIC_AZURE_AD_B2C_POLICY} \
    NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES=${NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES} \
    NEXT_PUBLIC_AZURE_AD_B2C_KNOWN_AUTHORITIES=${NEXT_PUBLIC_AZURE_AD_B2C_KNOWN_AUTHORITIES}

RUN pnpm --filter web build

# ─── Stage 3: run ─────────────────────────────────────────────────────────
FROM node:20-alpine AS runtime
RUN apk add --no-cache tini

RUN addgroup --system --gid 1001 app \
    && adduser --system --uid 1001 --ingroup app app

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

# Standalone output bundles a minimal node_modules + the compiled app.
# apps/web has no public/ directory in this repo — Next handles that
# gracefully; add a `COPY --from=build .../public /app/apps/web/public`
# line if one is introduced later.
COPY --from=build --chown=app:app /build/apps/web/.next/standalone /app/
COPY --from=build --chown=app:app /build/apps/web/.next/static /app/apps/web/.next/static

USER app

EXPOSE 3000
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "apps/web/server.js"]
