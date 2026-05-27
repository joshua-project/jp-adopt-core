---
title: Next.js `rewrites()` are baked at BUILD time — runtime env vars do not work
date: 2026-05-27
module: apps/web
problem_type: convention
component: tooling
severity: high
applies_when:
  - "Running Next.js with `output: 'standalone'` in a container"
  - "Using `next.config.{js,ts}` `rewrites()` to proxy `/api/*` to an internal backend"
  - "Tempted to read `process.env.<TARGET>` inside the rewrites function and set it as a container runtime env"
related_components:
  - authentication
tags:
  - nextjs
  - standalone
  - rewrites
  - build-time
  - docker
  - container-apps
  - reverse-proxy
---

# Next.js `rewrites()` are baked at BUILD time — runtime env vars do not work

## Context

When `jp-adopt-core` migrated its staff web UI to a Next.js Container
App with a `/api/*` → internal-API rewrite proxy, the obvious-looking
configuration silently failed in production. The intent was to keep the
proxy target portable across environments by reading it from a runtime
env var:

```ts
// apps/web/next.config.ts — WRONG (silently)
async rewrites() {
  const target = process.env.API_PROXY_TARGET?.replace(/\/+$/, "");
  if (!target) return [];
  return [{ source: "/api/:path*", destination: `${target}/:path*` }];
}
```

In the deploy workflow the value was set as a container runtime env:

```yaml
az containerapp update --set-env-vars "API_PROXY_TARGET=https://jp-adopt-core-api-production.internal..."
```

The result: `rewrites()` resolved `process.env.API_PROXY_TARGET` to
`undefined` at runtime, returned `[]`, and `/api/*` calls from the
browser hit a `404` from Next's static handler. The container had the
env var set correctly — Next.js just wasn't reading it.

## Guidance

**`next.config.{js,ts}` is evaluated during `next build`, not at server
boot. The return value of `rewrites()` is serialized into the
`.next/standalone` bundle as a static manifest.** Setting
`API_PROXY_TARGET` as a *runtime* container env has no effect — the
baked manifest was already produced with `process.env.API_PROXY_TARGET
= undefined` at build time.

For a Next.js standalone deploy, the proxy target must be present
**during `next build`**, not at container start:

### Right — pass as a Docker build arg

```dockerfile
# apps/web/Dockerfile
ARG API_PROXY_TARGET=

FROM node:20-alpine AS build
ARG API_PROXY_TARGET                  # re-import per stage
ENV API_PROXY_TARGET=${API_PROXY_TARGET}
RUN pnpm --filter web build           # rewrites() evaluates HERE
```

```yaml
# .github/workflows/deploy.yml — build-web job
- name: Build & push web image
  run: |
    API_PROXY_TARGET=$(az containerapp show \
      --name "$ACA_API_APP_NAME" \
      --resource-group "$ACA_RESOURCE_GROUP" \
      --query "properties.configuration.ingress.fqdn" -o tsv)
    docker build \
      --build-arg "API_PROXY_TARGET=https://${API_PROXY_TARGET}" \
      -f apps/web/Dockerfile \
      -t "$IMAGE_TAG" .
```

The deploy step (`az containerapp update`) then does NOT set
`API_PROXY_TARGET` as a runtime env — that env is ignored by the
already-baked image.

### Trade-offs and accepted constraints

- The image is now environment-specific. A web image built for
  production will not work in staging without a rebuild. This is fine
  if your deploy pipeline already rebuilds per environment.
- If the API container is recreated with a new internal FQDN, the web
  image **must** be rebuilt. A runtime env-var change has no effect.
- Document this loudly: a future operator setting `API_PROXY_TARGET` on
  the running container will see no change and waste hours.

## Why This Matters

This footgun fires silently. The configuration reads naturally (it's
the same pattern that works in 99% of Node apps), the container env
var is present in `az containerapp show`, and there is no startup
warning from Next.js indicating the value was missed. The only
diagnostic that surfaces the truth is mechanical: extract the
`.next/standalone/server.js` from a built image and grep for the
literal proxy target — if it's not there, `rewrites()` saw `undefined`
at build.

This generalizes beyond `API_PROXY_TARGET`. Any value referenced inside
`next.config.{js,ts}` — `rewrites`, `redirects`, `images.domains`,
`headers`, etc. — is build-time, not runtime. The same trapdoor applies
to all of them.

`NEXT_PUBLIC_*` env vars also bake at build time, but most teams already
know that. The `next.config` evaluation point is less widely known and
catches teams who assume "runtime config = `next.config.js` + runtime
env" works the way it does in Express or Fastify.

## When to Apply

- Any new Next.js project that needs a same-origin `/api/*` proxy in a
  container deploy
- Migrating a Next.js app from one platform to another and discovering
  that `next.config` references env vars
- Diagnosing a "404 from `/api/*`" or "the proxy isn't proxying" symptom
  when the container env is set correctly

## Examples

### Wrong (silent failure)

```ts
// next.config.ts
async rewrites() {
  return [{
    source: "/api/:path*",
    destination: `${process.env.API_PROXY_TARGET}/:path*`,
  }];
}
```

```yaml
# deploy step — sets the env at runtime, NOT during build
az containerapp update --set-env-vars "API_PROXY_TARGET=..."
```

### Right (build-time bake)

```ts
// next.config.ts — same code, but the value is set BEFORE `next build`
async rewrites() {
  const target = process.env.API_PROXY_TARGET?.replace(/\/+$/, "");
  if (!target) return [];
  return [{ source: "/api/:path*", destination: `${target}/:path*` }];
}
```

```dockerfile
# Dockerfile — value flows in as a build-arg, then into the build env
ARG API_PROXY_TARGET=
ENV API_PROXY_TARGET=${API_PROXY_TARGET}
RUN pnpm --filter web build
```

```yaml
# deploy step — passes value as build-arg, not runtime env
docker build --build-arg "API_PROXY_TARGET=$resolved_fqdn" .
```

### Verification

After the build, grep the standalone bundle to confirm the literal made
it in:

```bash
docker run --rm <image-tag> grep -r "jp-adopt-core-api-production.internal" /app/apps/web/.next/standalone/
# Expect: a hit inside the routes-manifest.json (or similar). No hit = the bake didn't take.
```

## References

- jp-adopt-core PR #66 — Phase 1 SWA → Container App migration; initial
  P0 finding by adversarial code review
- `docs/superpowers/plans/2026-05-24-web-on-container-app.md` —
  "Correction: build-time bake" section
- `apps/web/Dockerfile` — current implementation
- [Next.js docs — `rewrites()` configuration](https://nextjs.org/docs/app/api-reference/next-config-js/rewrites)
  (does not call out the build-time evaluation explicitly)
