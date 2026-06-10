# DNS rebind: `adoption.joshuaproject.net` → web Container App

Companion to issue #82. Step-by-step cutover from the legacy Static
Web App to the `jp-adopt-core-web-production` Container App.

This runbook is for a single operator (Joel) with admin access to:
- `jp-infrastructure` Terraform
- Cloudflare DNS for `joshuaproject.net`
- Azure Entra (the `jp-adopt-core-web` SPA app registration)

Expected window: **30 minutes execution, up to 30 minutes wait for
managed-cert provisioning, plus DNS TTL propagation.**

---

## Pre-flight (5 min)

Capture the current state so the runbook is reversible.

```bash
# Existing CNAME target (the SWA), for the rollback step.
dig +short CNAME adoption.joshuaproject.net
# Expected: ambitious-pebble-07e6a6210.7.azurestaticapps.net.

# ACA FQDN we're migrating to — confirm it's healthy now.
curl -fsS "https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/api/healthz"
# Expected: 200 {"status":"ok",...}

# Capture the current Entra redirect URIs (so the post-cutover
# verification can confirm `https://adoption.joshuaproject.net/auth/callback`
# is still listed, not accidentally removed).
az ad app show --id 3a6d7ff8-fb64-48df-9302-6a236a194db5 \
  --query "spa.redirectUris" -o tsv
```

The Entra SPA app reg (`3a6d7ff8-fb64-48df-9302-6a236a194db5`) should
already include `https://adoption.joshuaproject.net/auth/callback` per
the entra-direct-staff-auth plan — verify before cutover.

---

## Step 1 — Add `asuid.adoption` TXT record (Cloudflare, 2 min)

Azure validates the custom domain binding via a TXT record proving DNS
control. Get the validation token:

```bash
ACA_DOMAIN_VERIFICATION_ID=$(az containerapp show \
  -n jp-adopt-core-web-production \
  -g rg-jp-adopt-core-production \
  --query "properties.customDomainVerificationId" -o tsv)
echo "$ACA_DOMAIN_VERIFICATION_ID"
```

In Cloudflare DNS for `joshuaproject.net`, add:

| Type | Name | Content | Proxy | TTL |
|---|---|---|---|---|
| TXT | `asuid.adoption` | `<the verification id from above>` | DNS only | Auto |

**Wait ~60 seconds** then verify:

```bash
dig +short TXT asuid.adoption.joshuaproject.net
# Expected: "<verification id>"
```

---

## Step 2 — Repoint the `adoption` CNAME (Cloudflare, 1 min)

In Cloudflare DNS, edit the existing `adoption` CNAME:

| Field | Before | After |
|---|---|---|
| Type | CNAME | CNAME |
| Name | `adoption` | `adoption` |
| Content | `ambitious-pebble-07e6a6210.7.azurestaticapps.net` | `jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io` |
| Proxy | DNS only | **DNS only** (do not enable orange-cloud — Azure manages TLS, and proxying breaks the cert exchange) |
| TTL | Auto | Auto |

**Wait ~60 seconds** then verify:

```bash
dig +short CNAME adoption.joshuaproject.net
# Expected: jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io.
```

The site is now unreachable at the custom domain — visiting it returns
an Azure default page — until step 3 binds it.

---

## Step 3 — Bind the custom domain in `jp-infrastructure` (Terraform, 10 min + ~20 min cert wait)

In `jp-infrastructure`, add to the `jp-adopt-core` stack (next to the
existing `azurerm_container_app` resource):

```hcl
# Custom hostname binding for the staff app.
resource "azurerm_container_app_custom_domain" "adoption_joshuaproject_net" {
  name                     = "adoption.joshuaproject.net"
  container_app_id         = azurerm_container_app.web.id
  certificate_binding_type = "SniEnabled"

  # Managed certificate — Azure provisions a Let's Encrypt cert on
  # bind. This takes ~10-20 minutes; the resource stays in
  # `Pending` until the cert lands and DNS validation succeeds (so
  # step 1's TXT must be live before this apply).
  lifecycle {
    ignore_changes = [certificate_id]
  }
}

resource "azurerm_container_app_environment_custom_domain" "managed_cert_adoption" {
  # Only needed when using a managed cert; consult Azure docs for the
  # exact resource name in the version of azurerm_provider you're on.
  # See: https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app_custom_domain
}
```

If the provider version pinned in `jp-infrastructure` doesn't support
`certificate_binding_type = "SniEnabled"` without a pre-provisioned
cert, fall back to the `azapi` provider:

```hcl
resource "azapi_resource" "adoption_custom_domain" {
  type      = "Microsoft.App/containerApps/customDomains@2024-03-01"
  parent_id = azurerm_container_app.web.id
  name      = "adoption.joshuaproject.net"
  body = jsonencode({
    properties = {
      bindingType = "SniEnabled"
      # No certificateId → Azure provisions a managed cert.
    }
  })
}
```

Then:

```bash
cd stacks/azure/jp-adopt-core
terraform plan -out=tfplan
terraform apply tfplan
```

Watch the apply — it'll sit in `Pending` for 10-20 minutes while the
managed cert provisions. **Don't cancel.**

---

## Step 4 — Verify the cert + domain binding (5 min)

```bash
# Confirm the custom domain is bound and the cert is Healthy.
az containerapp hostname list \
  -n jp-adopt-core-web-production \
  -g rg-jp-adopt-core-production \
  --query "[?name=='adoption.joshuaproject.net']"

# Hit the custom domain end-to-end.
curl -fsSv "https://adoption.joshuaproject.net/api/healthz"
# Expected: TLS handshake against a `joshuaproject.net` cert,
# then 200 {"status":"ok",...}
```

If the cert is still `Provisioning` after 30 minutes, check that
`asuid.adoption.joshuaproject.net` TXT is resolvable from anywhere
(`dig` from a non-cached resolver) and that the `adoption` CNAME
points at the ACA FQDN, not the SWA.

---

## Step 5 — Smoke-test the full app (5 min)

```bash
# Health + readiness via the proxy.
curl -fsS "https://adoption.joshuaproject.net/api/healthz"
curl -fsS "https://adoption.joshuaproject.net/api/readyz"

# Run the prod smoke against the new hostname.
BEARER='<paste-fresh-token>' \
  API_URL=https://adoption.joshuaproject.net/api \
  SMOKE_EMAIL=joel@joelbcastillo.com \
  scripts/smoke-prod.sh
```

Also sign in via the staff app in a browser at
`https://adoption.joshuaproject.net/signin` and confirm:
- Entra redirect lands at `/auth/callback` cleanly (no `redirect_uri_mismatch`)
- `/matches`, `/campaigns`, `/admin/orgs` all render

If the sign-in throws `AADB2C90118` or `AADSTS50011`, the SPA app reg
is missing the redirect URI for the custom domain — see "Rollback /
follow-up" below.

---

## Step 6 — Update docs (5 min)

After the smoke clears, this PR's sibling change updates the runbooks
that reference the long ACA FQDN to point at the canonical hostname:

- `docs/runbooks/amy-walkthrough.md`
- `docs/runbooks/prod-smoke-walkthrough.md`
- `scripts/smoke-prod.sh` (the `API_URL` default)

(That cleanup is small but post-cutover — don't rush it in before the
hostname is actually live, or the docs will lie.)

---

## Rollback

If anything in steps 3-5 fails irreversibly:

1. In Cloudflare, repoint the `adoption` CNAME back to the SWA host
   captured in pre-flight.
2. Delete the `asuid.adoption` TXT record (not required, but tidy).
3. In Terraform, remove the `azurerm_container_app_custom_domain`
   resource and re-apply.
4. SWA serves traffic again (it was never decommissioned in this
   runbook).

DNS TTL is short on Cloudflare; rollback is live within minutes.

---

## What this enables

- Cleaner URL for Amy and other staff (no more pasting the long ACA
  FQDN)
- Closes #82
- Unblocks #90 (flipping the API to `external: false`) — the web's
  `/api/*` proxy is the only public entrypoint after the rebind,
  which is the architectural goal

---

## Post-soak SWA decommission

The SWA `jp-adopt-core-production` in `rg-jp-adopt-core-production`
keeps existing after the rebind. It serves zero production traffic but
costs ~$10/month and accumulates as infrastructure drift. Decommission
it after the rebind has soaked.

### When it's safe

All of:
- Rebind has been live for **at least 14 days** without any rollback
  trigger.
- Production smoke against `adoption.joshuaproject.net` passes daily.
- API ingress flip (#90, `api-external-false.md`) has also completed
  — otherwise you'd still want the SWA available for a recovery path.
- No external systems still reference the SWA FQDN
  (`ambitious-pebble-07e6a6210.7.azurestaticapps.net`). Grep monitoring
  configs, customer integrations, partner docs.

### What happens in `jp-infrastructure`

jp-infrastructure#203 removes:
- The `azurerm_static_web_app` resource for `jp-adopt-core-production`
- The `azurerm_static_web_app_custom_domain` for the canonical
  hostname (already moved to ACA in the rebind)
- Any linked-backend resource pinning the SWA to the API container

That PR is owned by the infrastructure repo; this section exists so
the core-side cleanup tracks alongside it.

### What changes in `jp-adopt-core` after #203 merges

The SWA references in this repo are no longer load-bearing. Land a
small cleanup PR removing:

| File | Change |
|---|---|
| `.github/workflows/deploy.yml` | Drop `SWA_APP_NAME` and `SWA_API_TOKEN` from the preflight 1P load block (lines ~124-125). The comment "retained as a rollback escape hatch" no longer applies — SWA is gone. |
| `docs/runbooks/deploy.md` | Remove `swa-app-name` / `swa-api-token` from the required-secrets list; remove the "idle SWA still exists" paragraph from the rollback section. |
| `docs/runbooks/dns-rebind.md` (this file) | Update the **Rollback** section above: cutting back to the SWA is no longer an option. The new rollback path is restoring from Postgres backup (`postgres-backup-restore.md`) + re-deploying a prior known-good API revision via `gh workflow run deploy.yml -f target=api`. |
| `docs/runbooks/quick-start.md` | Drop the "(or wherever the SWA points)" parenthetical in the sign-in instructions. |
| `1Password vault: Adopt Core - Production` | Delete the `swa-app-name` and `swa-api-token` fields. Recovery from a deleted 1P field is harder than from a deleted Azure SWA — do this **last**, after the deploy.yml PR merges and a deploy is green. |

### Rollback once SWA is gone

The SWA-based rollback path described earlier in this runbook no
longer exists after the decommission. Real-incident recovery for a
botched ACA web revision becomes:

1. `az containerapp revision list --name jp-adopt-core-web-production
   -g rg-jp-adopt-core-production` — find the prior known-good revision.
2. `az containerapp ingress traffic set --name
   jp-adopt-core-web-production -g rg-jp-adopt-core-production
   --revision-weight <prior-rev>=100 <bad-rev>=0` — shift traffic back.
3. Investigate, fix forward, redeploy.

DNS does not need to change in this rollback — both revisions live
behind the same custom domain.

### Verification after #203

- `az staticwebapp list --query "[?name=='jp-adopt-core-production']"`
  returns `[]`.
- `dig +short adoption.joshuaproject.net` still resolves to the ACA
  FQDN (unchanged from rebind).
- Deploy workflow runs green without the SWA env vars.
- `gh secret list` (in the GitHub repo) and the 1P vault contain no
  remaining SWA references.
