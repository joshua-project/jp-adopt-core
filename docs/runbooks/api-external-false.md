# Flip API ingress to `external: false`

Companion to issue #90. Returns the production architecture to the
originally-documented shape: API has zero public surface; the staff
web app is the only public entrypoint; jp-adopt-forms goes through
the web's `/api/*` proxy.

**Order is load-bearing.** If you flip the API to internal before
forms is migrated, every public form submission stops reaching
adopt-core's contacts table. The runbook below sequences carefully
to prevent that.

This runbook is for a single operator (Joel) with access to both
`jp-adopt-core` and `jp-adopt-forms` repos plus Azure CLI + the
Cloudflare DNS for joshuaproject.net.

---

## Pre-flight (5 min)

Capture the current state for rollback.

```bash
# Confirm current ingress is external:true (the symptom this fixes)
az containerapp show \
  -n jp-adopt-core-api-production \
  -g rg-jp-adopt-core-production \
  --query "properties.configuration.ingress.external" -o tsv
# Expected: true

# Confirm the web proxy already works against the internal API
curl -fsS "https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/api/healthz"
# Expected: {"status":"ok",...}

# Capture current jp-adopt-forms intake URL — for rollback if needed
grep -rn "jp-adopt-core-api\|jp-adopt-core-web" \
  /path/to/jp-adopt-forms/src/lib/ | head -5
```

---

## Step 1 — Migrate jp-adopt-forms to the proxy URL (forms PR, 30 min)

In `jp-adopt-forms`, open `src/lib/core-client.ts` (the file that
POSTs to adopt-core's `/v1/intake/*`).

Find the base URL constant and change:

```ts
// BEFORE
const CORE_API_URL =
  "https://jp-adopt-core-api-production.mangodesert-2647616f.centralus.azurecontainerapps.io";

// AFTER (pre-#82)
const CORE_API_URL =
  "https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/api";

// AFTER (post-#82, once adoption.joshuaproject.net is bound)
const CORE_API_URL = "https://adoption.joshuaproject.net/api";
```

Notes:
- POST paths stay `/v1/intake/adoption` and `/v1/intake/facilitation`
  — they're now under `{base}/v1/intake/*`. The web's `/api/*`
  rewrite strips `/api` and forwards the rest unchanged.
- `INTAKE_API_KEYS` bearer-token auth is unchanged — that's at the
  request body / Authorization-header layer, not the URL layer.

Open the PR in jp-adopt-forms, get CI green, merge, and wait for
the forms deploy to land.

---

## Step 2 — Verify forms→web→API works end-to-end (10 min)

Submit a real adopter form on the production forms site. Confirm:

```bash
# Watch the web container app's logs for the proxy hit
az containerapp logs show \
  -n jp-adopt-core-web-production \
  -g rg-jp-adopt-core-production \
  --tail 50 --follow

# In another shell, after submitting the form, verify the row landed
# in adopt-core. Use a known unique field from the submission
# (display_name, email, submission_id).
curl -fsS \
  -H "Authorization: Bearer <prod-staff-token>" \
  "https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/api/v1/contacts?q=<unique-marker>" \
  | jq '.items[] | {id, display_name, source_system, created_at}'
```

The row should show `source_system='jp-adopt-forms'`. If not — the
forms migration didn't land, the proxy is misrouting, or the
Authorization header isn't carrying. **STOP here and fix.** Do
not proceed to step 3 until this verification passes for at least
one real submission.

---

## Step 3 — Flip the API ingress to internal (30 sec)

Once step 2 is solid:

```bash
az containerapp ingress update \
  --name jp-adopt-core-api-production \
  --resource-group rg-jp-adopt-core-production \
  --type internal
```

The flip is immediate. New TLS handshakes to the API's public FQDN
will start failing within ~30 seconds.

---

## Step 4 — Verify the new posture (5 min)

```bash
# Direct hit to the API's public FQDN should fail (connection
# rejected or "private endpoint" response).
curl -fsSv \
  "https://jp-adopt-core-api-production.mangodesert-2647616f.centralus.azurecontainerapps.io/healthz" \
  2>&1 | head -10
# Expected: TLS handshake fails or 502/403 from the ingress.

# Web proxy still works (it reaches the API's internal FQDN).
curl -fsS \
  "https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/api/healthz"
# Expected: {"status":"ok",...}

# End-to-end smoke through the proxy (the same script we use for
# every deploy).
BEARER='<fresh-token>' \
  API_URL=https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/api \
  SMOKE_EMAIL=joel@joelbcastillo.com \
  scripts/smoke-prod.sh
```

If the API FQDN is still reachable publicly, the ingress flip
didn't take. `az containerapp show ... --query
properties.configuration.ingress.external` should now report
`false`; if it's still `true`, re-run the update.

---

## Step 5 — Land the change in `jp-infrastructure` Terraform (15 min)

The Azure CLI flip in step 3 was an out-of-band change. The next
`terraform apply` in jp-infrastructure would silently revert it
back to `external: true`. To prevent that, update the relevant
resource in `stacks/azure/jp-adopt-core/`:

```hcl
resource "azurerm_container_app" "api" {
  # ...
  ingress {
    external_enabled = false   # was: true
    target_port      = 8000
    transport        = "auto"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }
}
```

`terraform plan` should show no actual change (the cloud state is
already `external: false` from step 3). `apply` to lock it in.

---

## Step 6 — Update the deploy runbook (5 min)

In `docs/runbooks/deploy.md`, the architecture section described
the original `external_enabled=false` design as if it were the
current state during the incident window. Now that we're back to
that posture, either confirm the existing language or add a brief
note that #88 (the temporary external:true) is closed and #90
restored the original shape.

This is a tidying-only commit; do it after step 5 lands.

---

## Rollback

If step 4's smoke or the forms round-trip breaks after the flip:

1. `az containerapp ingress update --name jp-adopt-core-api-production -g rg-jp-adopt-core-production --type external` — restores public ingress.
2. The Authorization header / `INTAKE_API_KEYS` auth stays intact; nothing else needs to change.
3. Then debug what broke at the proxy layer with the new forms client URL.

Steps 1 + 5 are the only changes that touch persistent state; both
are individually revertible.

---

## What this enables

- API has zero public surface — only `/healthz`, `/readyz`, the
  intake routes (key-protected), and the rest of `/v1/*` are
  reachable, all gated by FastAPI's own auth, and only via the
  web's proxy
- Closes #90
- Removes the original incident #88's temporary external:true
  state from production
