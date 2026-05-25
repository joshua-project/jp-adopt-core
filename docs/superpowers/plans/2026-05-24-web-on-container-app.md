# Web on Azure Container App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the Next.js staff UI as its own Azure Container App (standalone Node server) instead of Azure Static Web Apps, with the browser reaching the internal FastAPI backend through a Next.js rewrite proxy — fixing both current `deploy.yml` failures.

**Architecture:** The web app already builds `output: "standalone"` (a Node-server artifact) and already has `apps/web/Dockerfile`. SWA cannot run a standalone server, which is the root cause of the `deploy-web` "unknown exception" and (transitively) the `deploy-api` smoke-check 404. We align to the dominant Joshua Project pattern (jp-prayer-map and jp-adopt-forms both run standalone Next.js on a Node host, not SWA) by running web as a Container App in the **same ACA environment** as the API. The browser hits `https://adoption.joshuaproject.net/api/*`; the web app's Next.js server rewrites `/api/*` to the API's **internal** FQDN. The API ingress stays `external_enabled = false` (unchanged). The SWA is kept provisioned-but-idle as a fast rollback escape hatch.

**Tech Stack:** Next.js 15 (standalone), Azure Container Apps, Azure Container Registry, GitHub Actions (OIDC), Terraform (jp-infrastructure), 1Password.

**Reversibility:** Each part is independently reversible. The `adoption.joshuaproject.net` custom-domain move (Part C) is the only production-visible cutover; it flips back by re-binding the domain to the SWA. The SWA + linked backend are NOT destroyed by this plan (Part D cleanup is deferred and explicit).

---

## Cross-repo note

- **Part A** executes in the **jp-infrastructure** repo (Terraform). It must merge + apply before Part B's deploy can target a real web Container App.
- **Parts B, C-app** execute in **this repo (jp-adopt-core)**.
- **Part C-cutover** is a manual `az`/portal step coordinated by the operator.
- **Part D** is deferred cleanup (separate session, after soak).

## Target routing contract (the single source of truth all tasks must match)

| Caller | URL | Resolves to |
|---|---|---|
| Browser (client fetch) | `https://adoption.joshuaproject.net/api/v1/contacts` | web ACA → Next rewrite → API internal `/v1/contacts` |
| Browser (client fetch) | `https://adoption.joshuaproject.net/api/healthz` | web ACA → Next rewrite → API internal `/healthz` |
| Next rewrite source | `/api/:path*` | `${API_PROXY_TARGET}/:path*` (strips the `/api` prefix) |
| `API_PROXY_TARGET` (**BUILD ARG** — baked, not a runtime env) | `https://jp-adopt-core-api-production.internal.<aca-env-default-domain>` | API internal ingress (port 8000) |

> **Correction (post code-review, 2026-05-24):** Next evaluates `rewrites()` at **build time** and freezes the result into the standalone server — a *runtime* container env var is never read (proven against the built artifact). So `API_PROXY_TARGET` must be present during `next build`. The `build-web` job resolves the API's internal FQDN via `az containerapp show` and passes it as a Docker **build arg**; the Dockerfile sets it as `ENV` in the build stage. The web Container App therefore needs **no** `API_PROXY_TARGET` runtime env (Task A1 updated accordingly).
| `NEXT_PUBLIC_API_URL` (build arg, baked into client bundle) | `/api` | relative same-origin base; `getBaseUrl()` makes it absolute against `window.location.origin` |

The FastAPI app is **unchanged**: it keeps serving `/healthz`, `/readyz`, `/v1/*` with no `/api` prefix. The `/api` prefix lives only between browser↔web; the rewrite strips it before hitting the API.

---

## File Structure

**jp-infrastructure (Part A):**
- Modify: the jp-adopt-core stack that declares the api/worker `azurerm_container_app` resources — add a `web` container app following the existing api pattern (external ingress, port 3000). No `API_PROXY_TARGET` runtime env is needed (the proxy target is baked into the image at build — see Correction above). Custom-domain + cert binding for `adoption.joshuaproject.net` (Part C).

**jp-adopt-core (Parts B, C-app):**
- Modify: `apps/web/next.config.ts` — add `rewrites()` reading `process.env.API_PROXY_TARGET`.
- Modify: `apps/web/src/lib/api-client.ts:30` — `getBaseUrl()` returns an absolute same-origin URL when `NEXT_PUBLIC_API_URL` is relative.
- Modify: `.github/workflows/deploy.yml` — add `build-web` job; replace the SWA `deploy-web` job with an ACA deploy + wait + web smoke; add a final end-to-end `smoke` job; add `aca-web-app-name` to preflight.
- 1Password: add field `aca-web-app-name = jp-adopt-core-web-production` to `op://JP Adopt Platform/Adopt Core - Production`.

---

## Part A — Infrastructure (jp-infrastructure repo)

> Execute in a jp-infrastructure session. Follow the existing `jp-adopt-core-api-production` container-app declaration as the template; mirror its environment, identity, ACR pull config, and ingress block, changing only what each step calls out.

### Task A1: Add the web Container App (placeholder image)

**Files:**
- Modify: the jp-adopt-core app stack `.tf` that declares `jp-adopt-core-api-production` / `jp-adopt-core-worker-production`.

- [ ] **Step 1: Declare `jp-adopt-core-web-production`** in the same `azurerm_container_app_environment` as the API, mirroring the API resource with these differences:
  - `ingress { external_enabled = true; target_port = 3000; transport = "auto" }`
  - Image: the ACR placeholder `jpcontainerregistry.azurecr.io/jp-adopt-web:placeholder` (push in Step 2).
  - `env` block (runtime, NOT secrets):
    - `DEPLOY_SHA = "placeholder"` (overwritten by deploy.yml).
    - Do **NOT** add `API_PROXY_TARGET` here — it is baked into the image at build time by `build-web` (see Correction in the routing-contract section). A runtime env var is not read by the frozen standalone rewrites.
  - Same user-assigned identity (`id-jp-adopt-core-production`) + ACR pull role the API uses.
  - `ignore_changes = [template[0].container[0].image, template[0].container[0].env]` on the relevant lifecycle block, matching the API's narrow ignore pattern (the deploy workflow owns the image tag + DEPLOY_SHA).

- [ ] **Step 2: Push a placeholder web image** so the container app can start before the first real deploy (mirrors the api/worker placeholders):

```bash
az acr login --name jpcontainerregistry
docker pull mcr.microsoft.com/azuredocs/aci-helloworld:latest
docker tag mcr.microsoft.com/azuredocs/aci-helloworld:latest jpcontainerregistry.azurecr.io/jp-adopt-web:placeholder
docker push jpcontainerregistry.azurecr.io/jp-adopt-web:placeholder
```

- [ ] **Step 3: `terraform plan`** for the jp-adopt-core stack.
Expected: one `azurerm_container_app.web` to add, no destroys. Confirm the API and worker apps are unchanged (no diff).

- [ ] **Step 4: Apply** (via the normal push-triggered `--changed` flow, NOT `apply_all=true` — that triggers the deprecated dt-platform-b2c stack).
Expected: web app provisions; `az containerapp show -n jp-adopt-core-web-production -g rg-jp-adopt-core-production --query properties.configuration.ingress.external` → `true`.

- [ ] **Step 5: Verify the placeholder is reachable** on the ACA-generated FQDN:

```bash
WEB_FQDN=$(az containerapp show -n jp-adopt-core-web-production -g rg-jp-adopt-core-production --query properties.configuration.ingress.fqdn -o tsv)
curl -sS -o /dev/null -w "%{http_code}\n" "https://${WEB_FQDN}/"
```
Expected: `200` (helloworld placeholder).

- [ ] **Step 6: Commit** the Terraform change.

> Custom-domain binding for `adoption.joshuaproject.net` is intentionally deferred to Part C so the real image can be validated on the ACA FQDN before the production cutover. Do NOT move the domain in this task.

---

## Part B — jp-adopt-core app + workflow changes (this repo)

### Task B1: Add the Next.js rewrite proxy

**Files:**
- Modify: `apps/web/next.config.ts`

- [ ] **Step 1: Add `rewrites()` to the config.** Replace the file body with:

```ts
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["@jp-adopt/contracts"],
  // Standalone build → .next/standalone/ self-contained Node server.
  // Run as an Azure Container App (NOT Static Web Apps — SWA cannot run
  // a standalone server). See docs/superpowers/plans/2026-05-24-web-on-container-app.md.
  output: "standalone",
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
  },
  // Browser calls same-origin /api/* ; the Node server proxies to the
  // API's INTERNAL container-app FQDN. API_PROXY_TARGET is a runtime env
  // var set by Terraform on the web container (never baked into the
  // client bundle). The /api prefix is stripped here so the FastAPI app
  // keeps serving /healthz, /readyz, /v1/* unprefixed.
  async rewrites() {
    const target = process.env.API_PROXY_TARGET;
    if (!target) return [];
    return [{ source: "/api/:path*", destination: `${target}/:path*` }];
  },
};

export default nextConfig;
```

- [ ] **Step 2: Verify the build still succeeds** (rewrites are valid in standalone):

```bash
cd /Users/joel/.repos/github.com/joshua-project/jp-adopt-core/.dmux/worktrees/issue-176
NEXT_PUBLIC_API_URL=/api pnpm --filter web build
```
Expected: build completes; `apps/web/.next/standalone/apps/web/server.js` exists.

- [ ] **Step 3: Commit**

```bash
git add apps/web/next.config.ts
git commit -m "feat(web): proxy /api/* to internal API via Next rewrite"
```

### Task B2: Make `getBaseUrl()` resolve a relative base against the browser origin

**Files:**
- Modify: `apps/web/src/lib/api-client.ts:30`

**Why:** `api-client.ts:144` does `new URL(`${getBaseUrl()}${path}`)`. `new URL("/api/v1/...")` with a relative string throws. In production `NEXT_PUBLIC_API_URL=/api` (relative, same-origin), so `getBaseUrl()` must return an absolute URL in the browser.

- [ ] **Step 1: Replace the `getBaseUrl` function** (current body is `return process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";`):

```ts
export function getBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  if (configured && configured.length > 0) {
    // A relative value ("/api") means "same origin, proxied by Next".
    // new URL(...) needs an absolute base, so resolve against the
    // current origin in the browser. (Data fetching is client-side.)
    if (configured.startsWith("/") && typeof window !== "undefined") {
      return `${window.location.origin}${configured}`;
    }
    return configured;
  }
  // Dev default: web (`next dev`) talks to the API directly.
  return "http://127.0.0.1:8000";
}
```

- [ ] **Step 2: Confirm no server-side caller breaks.** Verify all `getBaseUrl()` / `${base}` API calls originate in `"use client"` components (so `window` is defined):

```bash
cd /Users/joel/.repos/github.com/joshua-project/jp-adopt-core/.dmux/worktrees/issue-176
grep -rn "getBaseUrl\|NEXT_PUBLIC_API_URL" apps/web/src apps/web/app
```
Expected: usages are in client components (`ContactsB2C.tsx`, `ContactsDevOnly.tsx`, `api-client.ts`). If any server component calls it, add a fallback to `process.env.API_PROXY_TARGET` for the `typeof window === "undefined"` branch and note it here.

- [ ] **Step 3: Lint + typecheck**

```bash
pnpm --filter web lint
```
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add apps/web/src/lib/api-client.ts
git commit -m "fix(web): resolve relative NEXT_PUBLIC_API_URL against browser origin"
```

### Task B3: Add `aca-web-app-name` to 1Password

**Files:** 1Password item `op://JP Adopt Platform/Adopt Core - Production`.

- [ ] **Step 1: Add the field**

```bash
source ~/.config/op/token-joshuaproject.sh
op item edit "Adopt Core - Production" --vault "JP Adopt Platform" \
  "Container Apps.aca-web-app-name=jp-adopt-core-web-production"
```

- [ ] **Step 2: Verify**

```bash
op item get "Adopt Core - Production" --vault "JP Adopt Platform" --fields "aca-web-app-name"
```
Expected: `jp-adopt-core-web-production`.

### Task B4: `build-web` job in deploy.yml

**Files:**
- Modify: `.github/workflows/deploy.yml`

- [ ] **Step 1: Add a `build-web` job** after `build-worker` (mirror `build-api`, but build `apps/web/Dockerfile` and pass the `NEXT_PUBLIC_*` build args). Insert:

```yaml
  build-web:
    name: Build & push web image
    needs: preflight
    if: needs.preflight.outputs.should_deploy_web == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Load deploy secrets from 1Password
        uses: 1password/load-secrets-action@v4
        env:
          OP_SERVICE_ACCOUNT_TOKEN: ${{ secrets.OP_SERVICE_ACCOUNT_TOKEN }}
          AZURE_TENANT_ID: op://JP Adopt Platform/Adopt Core - Production/azure-tenant-id
          AZURE_SUBSCRIPTION_ID: op://JP Adopt Platform/Adopt Core - Production/azure-subscription-id
          AZURE_CLIENT_ID: op://JP Adopt Platform/Adopt Core - Production/azure-client-id
          ACR_LOGIN_SERVER: op://JP Adopt Platform/Adopt Core - Production/acr-login-server
        with:
          export-env: true

      - name: Azure login via OIDC
        uses: azure/login@v2
        with:
          tenant-id: ${{ env.AZURE_TENANT_ID }}
          subscription-id: ${{ env.AZURE_SUBSCRIPTION_ID }}
          client-id: ${{ env.AZURE_CLIENT_ID }}

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to Azure Container Registry
        run: az acr login --name "${{ env.ACR_LOGIN_SERVER }}"

      - name: Build & push web image
        uses: docker/build-push-action@v6
        with:
          context: .
          file: apps/web/Dockerfile
          push: true
          # NEXT_PUBLIC_* are baked at build time. NEXT_PUBLIC_API_URL=/api
          # makes the browser call same-origin /api/*, which the Next
          # server rewrites to the internal API (see next.config.ts).
          build-args: |
            NEXT_PUBLIC_API_URL=/api
            NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID=${{ vars.NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID }}
            NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME=${{ vars.NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME }}
            NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID=${{ vars.NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID }}
            NEXT_PUBLIC_AZURE_AD_B2C_POLICY=${{ vars.NEXT_PUBLIC_AZURE_AD_B2C_POLICY }}
            NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES=${{ vars.NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES }}
          tags: |
            ${{ env.ACR_LOGIN_SERVER }}/jp-adopt-web:${{ github.sha }}
            ${{ env.ACR_LOGIN_SERVER }}/jp-adopt-web:latest
          cache-from: type=registry,ref=${{ env.ACR_LOGIN_SERVER }}/jp-adopt-web:buildcache
          cache-to: type=registry,ref=${{ env.ACR_LOGIN_SERVER }}/jp-adopt-web:buildcache,mode=max
```

- [ ] **Step 2: Validate workflow YAML**

```bash
cd /Users/joel/.repos/github.com/joshua-project/jp-adopt-core/.dmux/worktrees/issue-176
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy.yml')); print('YAML OK')"
```
Expected: `YAML OK`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci(deploy): build & push web container image"
```

### Task B5: Replace the SWA `deploy-web` job with an ACA deploy

**Files:**
- Modify: `.github/workflows/deploy.yml` — replace the entire `deploy-web` job (the `Azure/static-web-apps-deploy@v1` job, currently the last job) with the below. Also add `ACA_WEB_APP_NAME` to the preflight 1Password env + fail-fast list (Step 1).

- [ ] **Step 1: Wire `aca-web-app-name` into preflight.** In the `preflight` job's "Load deploy secrets" env block add:

```yaml
          ACA_WEB_APP_NAME: op://JP Adopt Platform/Adopt Core - Production/aca-web-app-name
```
and add `ACA_WEB_APP_NAME` to the `required=( ... )` array in the "Fail-fast guard" step.

- [ ] **Step 2: Replace the `deploy-web` job** with:

```yaml
  deploy-web:
    name: Deploy web to ACA
    needs: [preflight, build-web]
    if: needs.preflight.outputs.should_deploy_web == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Load deploy secrets from 1Password
        uses: 1password/load-secrets-action@v4
        env:
          OP_SERVICE_ACCOUNT_TOKEN: ${{ secrets.OP_SERVICE_ACCOUNT_TOKEN }}
          AZURE_TENANT_ID: op://JP Adopt Platform/Adopt Core - Production/azure-tenant-id
          AZURE_SUBSCRIPTION_ID: op://JP Adopt Platform/Adopt Core - Production/azure-subscription-id
          AZURE_CLIENT_ID: op://JP Adopt Platform/Adopt Core - Production/azure-client-id
          ACR_LOGIN_SERVER: op://JP Adopt Platform/Adopt Core - Production/acr-login-server
          ACA_RESOURCE_GROUP: op://JP Adopt Platform/Adopt Core - Production/aca-resource-group
          ACA_WEB_APP_NAME: op://JP Adopt Platform/Adopt Core - Production/aca-web-app-name
        with:
          export-env: true

      - name: Azure login via OIDC
        uses: azure/login@v2
        with:
          tenant-id: ${{ env.AZURE_TENANT_ID }}
          subscription-id: ${{ env.AZURE_SUBSCRIPTION_ID }}
          client-id: ${{ env.AZURE_CLIENT_ID }}

      - name: Update web container app
        run: |
          set -euo pipefail
          az containerapp update \
            --name "${{ env.ACA_WEB_APP_NAME }}" \
            --resource-group "${{ env.ACA_RESOURCE_GROUP }}" \
            --image "${{ env.ACR_LOGIN_SERVER }}/jp-adopt-web:${{ github.sha }}" \
            --set-env-vars "DEPLOY_SHA=${{ github.sha }}"

      - name: Wait for new web revision to become ready
        run: |
          set -euo pipefail
          for i in $(seq 1 30); do
            state=$(az containerapp revision list \
              --name "${{ env.ACA_WEB_APP_NAME }}" \
              --resource-group "${{ env.ACA_RESOURCE_GROUP }}" \
              --query "[?properties.active==\`true\`] | [0].properties.healthState" -o tsv 2>/dev/null || echo "")
            if [ "$state" = "Healthy" ]; then
              echo "Web revision healthy after ${i} polls."
              exit 0
            fi
            echo "Web revision state=$state — retrying in 10s ($i/30)"
            sleep 10
          done
          echo "::error::Web revision did not reach Healthy within 300s"
          exit 1
```

- [ ] **Step 3: Validate workflow YAML** (same command as B4 Step 2). Expected `YAML OK`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci(deploy): deploy web to Container App instead of SWA"
```

### Task B6: Move the smoke check to a final end-to-end job

**Files:**
- Modify: `.github/workflows/deploy.yml` — remove the `Smoke check /healthz exposes new SHA` step from the `deploy-api` job (it cannot pass until web proxies `/api/*`), and add a dedicated `smoke` job that runs after both api and web deploy.

**Why:** the API is internal-only; the only public path to it is through the web app's `/api` proxy. So the end-to-end smoke must run after `deploy-web`. Pre-cutover it targets the ACA-generated web FQDN; post-cutover the custom domain works too.

- [ ] **Step 1: Delete the `Smoke check /healthz exposes new SHA` step** from the `deploy-api` job (lines beginning `- name: Smoke check /healthz exposes new SHA` through that step's end). The `deploy-api` job ends after "Wait for new revision to become ready".

- [ ] **Step 2: Add a `smoke` job** at the end of the file:

```yaml
  smoke:
    name: End-to-end smoke (web + API proxy)
    needs: [preflight, deploy-api, deploy-web]
    # Only when both api and web were in scope this run.
    if: needs.preflight.outputs.should_deploy_api == 'true' && needs.preflight.outputs.should_deploy_web == 'true'
    runs-on: ubuntu-latest
    steps:
      - name: Load deploy secrets from 1Password
        uses: 1password/load-secrets-action@v4
        env:
          OP_SERVICE_ACCOUNT_TOKEN: ${{ secrets.OP_SERVICE_ACCOUNT_TOKEN }}
          AZURE_TENANT_ID: op://JP Adopt Platform/Adopt Core - Production/azure-tenant-id
          AZURE_SUBSCRIPTION_ID: op://JP Adopt Platform/Adopt Core - Production/azure-subscription-id
          AZURE_CLIENT_ID: op://JP Adopt Platform/Adopt Core - Production/azure-client-id
          ACA_RESOURCE_GROUP: op://JP Adopt Platform/Adopt Core - Production/aca-resource-group
          ACA_WEB_APP_NAME: op://JP Adopt Platform/Adopt Core - Production/aca-web-app-name
        with:
          export-env: true

      - name: Azure login via OIDC
        uses: azure/login@v2
        with:
          tenant-id: ${{ env.AZURE_TENANT_ID }}
          subscription-id: ${{ env.AZURE_SUBSCRIPTION_ID }}
          client-id: ${{ env.AZURE_CLIENT_ID }}

      - name: Resolve web FQDN
        id: web
        run: |
          set -euo pipefail
          fqdn=$(az containerapp show \
            --name "${{ env.ACA_WEB_APP_NAME }}" \
            --resource-group "${{ env.ACA_RESOURCE_GROUP }}" \
            --query properties.configuration.ingress.fqdn -o tsv)
          echo "fqdn=$fqdn" >> "$GITHUB_OUTPUT"

      - name: Smoke — web root serves the app (not a placeholder)
        run: |
          set -euo pipefail
          code=$(curl -sS -o /dev/null -w "%{http_code}" "https://${{ steps.web.outputs.fqdn }}/")
          echo "GET / → $code"
          [ "$code" = "200" ] || { echo "::error::web root not 200"; exit 1; }

      - name: Smoke — /api/healthz proxies to the API and carries the SHA
        run: |
          set -euo pipefail
          url="https://${{ steps.web.outputs.fqdn }}/api/healthz"
          response=$(curl -fsS "$url")
          echo "Response: $response"
          expected_sha="$(echo "${{ github.sha }}" | cut -c1-10)"
          if ! echo "$response" | grep -q "\"sha\":\"${expected_sha}\""; then
            echo "::error::${url} did not carry expected sha ${expected_sha}"
            exit 1
          fi
```

- [ ] **Step 3: Validate workflow YAML** (B4 Step 2 command). Expected `YAML OK`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci(deploy): end-to-end smoke via web /api proxy after both deploys"
```

### Task B7: PR, deploy, and validate on the ACA FQDN (pre-cutover)

> Requires Part A applied (web Container App exists).

- [ ] **Step 1: Open the PR** for the B-series branch; let CI (`ci.yml`) pass; merge to main.

- [ ] **Step 2: Run the deploy and watch it**

```bash
gh workflow run deploy.yml --repo joshua-project/jp-adopt-core --ref main
sleep 5
RID=$(gh run list --workflow=deploy.yml --repo joshua-project/jp-adopt-core --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RID" --repo joshua-project/jp-adopt-core --exit-status
```
Expected: all jobs green, including `Deploy web to ACA` and `End-to-end smoke`.

- [ ] **Step 3: Manually confirm on the ACA FQDN** (domain not yet cut over):

```bash
WEB_FQDN=$(az containerapp show -n jp-adopt-core-web-production -g rg-jp-adopt-core-production --query properties.configuration.ingress.fqdn -o tsv)
curl -sS -o /dev/null -w "root=%{http_code}\n" "https://${WEB_FQDN}/"
curl -fsS "https://${WEB_FQDN}/api/healthz"; echo
```
Expected: root `200` serving the Next.js app (not helloworld placeholder); `/api/healthz` returns `{"status":"ok","sha":"<short-sha>"}` proving the proxy → internal API path works.

---

## Part C — Production cutover (operator + this repo)

### Task C1: Move `adoption.joshuaproject.net` to the web Container App

**Files:** jp-infrastructure (domain/cert binding) OR manual `az` if doing it out-of-band first to de-risk.

- [ ] **Step 1: Add the custom domain + managed certificate** to `jp-adopt-core-web-production`. A custom domain can be bound to only one resource at a time, so unbind from the SWA as part of this step. Prefer codifying in jp-infrastructure; if validating manually first:

```bash
az containerapp hostname add --hostname adoption.joshuaproject.net \
  -n jp-adopt-core-web-production -g rg-jp-adopt-core-production
# then bind a managed cert per the ACA managed-cert flow
```

- [ ] **Step 2: Verify the public domain serves the app and proxies the API**

```bash
curl -sS -o /dev/null -w "root=%{http_code}\n" "https://adoption.joshuaproject.net/"
curl -fsS "https://adoption.joshuaproject.net/api/healthz"; echo
```
Expected: root `200` Next.js app; `/api/healthz` carries the deploy SHA.

- [ ] **Step 3: Smoke the full stack** against the live domain:

```bash
scripts/smoke-local.sh   # if it accepts a base URL; else run the runbook §8 checks against adoption.joshuaproject.net
```

- [ ] **Step 4: Rollback rehearsal note (do not execute unless needed):** to revert, re-bind `adoption.joshuaproject.net` to the SWA (`ambitious-pebble-07e6a6210.7.azurestaticapps.net`). The SWA is still provisioned (Part D not yet run).

---

## Part D — Deferred cleanup (separate session, after soak)

> Do NOT bundle into this work. File as a follow-up issue. Only after the ACA web path has soaked in production.

- [ ] Remove the SWA (`jp-adopt-core-production`) + its linked backend from jp-infrastructure.
- [ ] Remove `swa-app-name` / `swa-api-token` from the 1P item and any remaining workflow references.
- [ ] Move the out-of-band `AcrPush` grant on deploy SP `6db2efd3-…` into Terraform (tracked separately).
- [ ] **Security (P2, from code review):** the `/api/*` proxy makes FastAPI's `/docs`, `/redoc`, `/openapi.json` publicly reachable (unauthenticated; `/v1/*` stays auth-gated). Disable interactive docs in production (`FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` when `APP_ENV=production`) in `apps/api/src/jp_adopt_api/main.py`, or deny those paths at the proxy. Not a blocker for this migration.
- [ ] Verify the SWA rollback escape hatch actually serves (its linked backend points at the now-internal API) before relying on it as the Part C rollback.

---

## Self-Review

**Spec coverage:**
- Failure #2 (SWA "unknown exception") → resolved by Part A (run standalone on ACA) + Part B5 (deploy to ACA, no SWA). ✓
- Failure #1 (API smoke 404, mislabeled as wait-timeout) → resolved by B1 (rewrite proxy) + B6 (smoke moved after web deploy, hits `/api/healthz` through the proxy). ✓
- Browser→internal API path (Joel's "make sure it works") → B1 rewrite + A1 `API_PROXY_TARGET` + B2 absolute base. ✓
- Empty `NEXT_PUBLIC_API_URL` gap → B4 bakes `NEXT_PUBLIC_API_URL=/api`. ✓
- Reversibility → SWA kept (Part D deferred); domain cutover isolated (C1 Step 4 rollback). ✓

**Placeholder scan:** No TBD/TODO; every code/YAML step shows full content. The one parameterized value is `${<aca_env_default_domain>}` in A1 (intentional — read from the TF env output rather than hardcoding the `mangodesert-…` domain).

**Type/name consistency:** `jp-adopt-core-web-production` (app), `jp-adopt-web` (image repo), `aca-web-app-name` / `ACA_WEB_APP_NAME` (1P field / env), `API_PROXY_TARGET` (runtime env), `NEXT_PUBLIC_API_URL=/api` (build arg) — used identically across A1, B1, B2, B3, B4, B5, B6.

**Open assumption to verify during B2:** all API callers are client-side (`window` defined). If a server component fetches, add the `API_PROXY_TARGET` fallback noted in B2 Step 2.
