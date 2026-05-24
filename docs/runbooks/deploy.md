# Deploy runbook (U12)

Workflow: `.github/workflows/deploy.yml`. Triggers on push to `main`
(after the CI workflow's required checks pass) or manually via
`workflow_dispatch` (with a `target` choice — `all`, `api`, `worker`,
or `web`).

## Architecture

```
Push to main
  ↓
CI (.github/workflows/ci.yml) — required check, blocks merge if red
  ↓ (merged)
Deploy (.github/workflows/deploy.yml)
  ↓
Preflight → load secrets + fail-fast guard
  ↓
Build-API, Build-worker (parallel) → push to ACR
  ↓
Migrate → alembic upgrade head against production Postgres
  ↓ (only if migrations succeed)
Deploy-API, Deploy-worker (parallel) → roll new ACA revisions
  ↓
Smoke check /healthz exposes the new SHA
  ↓
Deploy-web (Static Web Apps, parallel with API/worker)
```

If migrations fail, the deploy aborts and the previous revisions keep
serving traffic. The web deploy is independent — it pushes a new
static build to Azure Static Web Apps and doesn't depend on the API
container being rolled.

## One-time provisioning

These steps happen once when the project moves from "Joel's laptop" to
"a real Azure subscription". Companion repo `jp-infrastructure` owns
the Terraform; this runbook references the resources it creates.

1. **Azure tenant + subscription.** Joshua Project's existing Azure
   tenant. Subscription scoped to a single resource group
   (`rg-jp-adopt-prod`) so RBAC is contained.

2. **App registration for GitHub OIDC.** Create an Azure AD app
   registration; configure federated credentials trusting the
   `github.com/joshua-project/jp-adopt-core` repo on the `main`
   branch and the `feat/*` PR pattern. Grant the SP `Contributor` on
   `rg-jp-adopt-prod`, `AcrPush` on the ACR, and the relevant
   container-app + static-web-app data-plane roles.

3. **1Password vault setup.** A `JP Adopt Platform` vault with an
   `Adopt Core - Production` item containing the keys the workflow
   expects (created 2026-05-21 in the `joshuaproject` account). Per
   jp-infrastructure PR #157, the secrets split into two categories:

   **Category A — Azure infra identifiers** (operator-seeded; consumed
   by this `deploy.yml`). Field names are **kebab-case**:
   - `azure-tenant-id`, `azure-subscription-id`, `azure-client-id`
   - `acr-login-server` (e.g. `jpadopt.azurecr.io`)
   - `aca-resource-group` (e.g. `rg-jp-adopt-prod`)
   - `aca-api-app-name`, `aca-worker-app-name`
   - `swa-app-name`, `swa-api-token`

   **Category B — Terraform-managed secrets** (consumed by
   jp-infrastructure's `terraform.yml`; NOT used directly here): 10
   `TF_VAR_jp_adopt_core_*` values. See the jp-infrastructure
   bringup runbook §2 for the full list.

   The production Postgres connection string is **not** in 1Password
   anymore. Terraform constructs `db-url-migrator` (DDL role, for
   alembic) and `db-url-runtime` (DML role, for the runtime API
   container) and stores them in the `jp-adopt-core-kv-prod` Key
   Vault. The migrate step in `deploy.yml` pulls `db-url-migrator`
   via `az keyvault secret show` at run time, with the deploy SP
   scoped to `Key Vault Secrets User` on that vault.

4. **OIDC service-account token on the repo.** Add the GitHub repo
   secret `OP_SERVICE_ACCOUNT_TOKEN` with the service-account token
   that has read access to the vault item above. Workflow-wide
   `OP_ACCOUNT: joshuaproject` ensures the token resolves to the
   correct account even when multiple are configured on the runner
   (per the dt-adoption-platform OIDC regression).

5. **ACS resource provisioning.** ACS Email resource +
   `joshuaproject.net` domain verification (SPF + DKIM CNAMEs). The
   plan's Day-1 step. Until the domain is verified, the worker
   defaults to the Azure-supplied `*.azurecomm.net` sender; flip the
   `ACS_SENDER_ADDRESS` env var on the worker container app once
   verification is green.

6. **GitHub repo "variables" (non-secret) used by the web build.**
   - `NEXT_PUBLIC_API_URL`
   - `NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID`
   - `NEXT_PUBLIC_AZURE_AD_B2C_TENANT_NAME`
   - `NEXT_PUBLIC_AZURE_AD_B2C_TENANT_ID`
   - `NEXT_PUBLIC_AZURE_AD_B2C_POLICY`
   - `NEXT_PUBLIC_AZURE_AD_B2C_API_SCOPES`

   These are non-secret (they ship in the SPA's JS bundle) but
   environment-specific, so they live in repo `vars`, not `secrets`.

## Manual deploy

```bash
gh workflow run deploy.yml -f target=api -R joshua-project/jp-adopt-core
```

`target` accepts `all`, `api`, `worker`, or `web`.

## Verification after deploy

The workflow's `Smoke check /healthz exposes new SHA` step does this
automatically (hitting the public SWA URL), but the operator should
re-run after a manual deploy:

```bash
curl -fsS "https://adopt.joshuaproject.net/api/healthz"
# Expected: {"status":"ok","sha":"<10-char commit prefix>"}

curl -fsS "https://adopt.joshuaproject.net/api/readyz"
# Expected: {"status":"ready","sha":"<10-char commit prefix>"}
```

These URLs go through Static Web Apps' linked-backend forwarding
(`/api/*` → API ACA), so they exercise the public entry path that
real users hit. **Don't curl the ACA FQDN directly** — the
infrastructure provisioning (jp-infrastructure PR #157 U5) marks the
API ingress as `external_enabled=false`, so the ACA FQDN is only
reachable from inside the managed environment.

If `/api/readyz` returns 503, Postgres is unreachable from the
container. Check the container's `DATABASE_URL` injection (via ACA
secret-ref to `db-url-runtime` in `jp-adopt-core-kv-prod`) + the
Postgres firewall allow-list.

## Rollback

```bash
# 1. List recent revisions
az containerapp revision list \
  --name jp-adopt-api \
  --resource-group rg-jp-adopt-prod \
  --query "[].{name:name, image:properties.template.containers[0].image, active:properties.active}" \
  -o table

# 2. Pin traffic to the previous revision
az containerapp ingress traffic set \
  --name jp-adopt-api \
  --resource-group rg-jp-adopt-prod \
  --revision-weight <previous-revision-name>=100

# 3. Confirm /healthz returns the old SHA
curl -fsS "https://adopt.joshuaproject.net/api/healthz"
```

Migrations are NOT rolled back automatically. If the bad deploy
applied a migration with breaking behavior, also run:

```bash
cd apps/api
DATABASE_URL=<prod-url> uv run alembic downgrade <target-revision>
```

…using the per-app migrator role, not the runtime app user. Refer to
the per-PR migration body for the safest downgrade target.

## Synthetic monitoring

Set up a synthetic monitor (Azure Monitor / Datadog / Uptime Robot —
pick one) hitting:
- `GET https://adopt.joshuaproject.net/api/healthz` every 60s — pages on 2 consecutive failures
- `GET https://adopt.joshuaproject.net/api/readyz` every 5 min — pages on 1 failure (Postgres outage)

Both endpoints return the deploy SHA in the response body so the
monitor's history can correlate "started failing" with "new revision
rolled".

## Known gaps

- **No staging environment.** The deploy workflow targets production
  directly. For week 1 this is acceptable because the cutover Saturday
  (U13) is the first traffic event. A staging slot lands in v2.
- **ACA + Key Vault references do NOT auto-rotate.** When a referenced
  secret rotates, the container revision needs a restart. See
  `secret-rotation.md` for the procedure.
- **Web deploy doesn't share the API's smoke check.** The Static Web
  Apps deploy is fire-and-forget; if the build artifact references a
  stale `NEXT_PUBLIC_API_URL`, the browser console reveals it but the
  workflow doesn't catch it. Eyeball `/contacts` after every web
  deploy.
- **Terraform changes (jp-infrastructure repo)** apply on a separate
  workflow. This runbook only covers the container/web layer. ACS
  Email DNS, ACA managed environment, Key Vault references, etc.,
  live in Terraform.

## Reference: institutional learnings honored here

- 1Password `OP_ACCOUNT` workflow-wide (dt-adoption-platform OIDC
  regression).
- Element-wise `lifecycle.ignore_changes` per setting — applied in
  Terraform; the deploy.yml does `--set-env-vars` only for `DEPLOY_SHA`
  + the image tag, never the whole map.
- Fail-fast guard before any Azure write (preflight job).
- Per-app DB user discipline — the migrate job pulls
  `db-url-migrator` from Key Vault (DDL-owning role) at run time, not
  from 1Password. The runtime `db-url-runtime` is injected into the
  API container via an ACA secret-ref Terraform sets up; this
  workflow never reads it.
