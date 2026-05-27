---
title: SWA → ACA "linked backend" silently enables Easy Auth on the Container App
date: 2026-05-27
module: jp-infrastructure/stacks/production/apps/jp-adopt-core
problem_type: integration_issue
component: authentication
severity: high
symptoms:
  - "Every authenticated API request returned 401 Unauthorized after switching the web client from Static Web Apps to a Container App"
  - "`/api/v1/*` calls through the new Next.js proxy were rejected even though the API container's own `/healthz` was reachable internally"
  - "No `Microsoft.App/containerApps/authConfigs` resource was visible in the Terraform plan — Easy Auth appeared to be off"
root_cause: config_error
resolution_type: config_change
related_components:
  - tooling
tags:
  - azure
  - container-apps
  - static-web-apps
  - easy-auth
  - linked-backend
  - silent-side-effect
  - terraform
---

# SWA → ACA "linked backend" silently enables Easy Auth on the Container App

## Context

When `jp-adopt-core` migrated its staff web UI from Azure Static Web Apps
(SWA) to its own Azure Container App with a Next.js `/api/*` rewrite
proxy (Phase 1 of the launch, PR #66), every authenticated request
through the new proxy started returning `401 Unauthorized`. The API
container was healthy, its `/healthz` endpoint reachable from inside the
managed environment, and the JWT was correctly attached on the browser
side — but the response always 401'd.

The Terraform state showed no `authConfigs` resource on the API
container, no `--enable-auth` flag in the deploy workflow, and the
`STRICT_AUTH=true` + dev-bearer disable looked correct. The 401 had no
matching `Microsoft.App` resource to attribute it to.

## Guidance

**An Azure Static Web App's "linked backend" feature silently enables
Easy Auth on the Container App it links to**, even when no
`authConfigs` resource exists in Terraform. The linked backend is
implemented under the hood as an Azure-managed `authConfigs` child of
the Container App; the parent SWA owns it, so it does not appear in
your Container App TF state and does not surface in plan diffs.

If you have a SWA `linked_backend` resource pointed at a Container App
AND you want to call that Container App directly (bypassing the SWA),
the linked backend's Easy Auth will reject the direct calls with 401.

### Diagnostic — confirm Easy Auth is enabled on the Container App

```bash
az containerapp auth show \
  --name <container-app-name> \
  --resource-group <rg> \
  --query "{enabled:properties.platform.enabled, providers:properties.identityProviders}" \
  -o json
```

If `enabled: true` and you did NOT explicitly configure it in
Terraform, look for a SWA linked-backend resource:

```bash
# Find any SWA pointing at this Container App as a linked backend:
az staticwebapp backends show --name <swa-name> --resource-group <rg> -o json
```

### Fix

1. Unlink the backend at the SWA side:

   ```bash
   az staticwebapp backends unlink \
     --name <swa-name> \
     --resource-group <rg> \
     --environment-name default
   ```

2. Disable Easy Auth on the Container App (the unlink leaves the auth
   config in place):

   ```bash
   az containerapp auth update \
     --name <container-app-name> \
     --resource-group <rg> \
     --enabled false
   ```

3. **Codify the removal in Terraform** so a future `terraform apply`
   doesn't recreate it. In our case, the `azapi_resource` for the SWA
   linked backend lived in
   `jp-infrastructure/stacks/production/apps/jp-adopt-core/` — removing
   the resource block + its generated output is the durable fix
   (jp-infrastructure PR #184).

## Why This Matters

Silent side-effects between Azure services are the worst class of
infrastructure surprise. Reading the Container App's resource graph in
the portal or in `terraform state list` gives no indication Easy Auth
is on. A new engineer looking at the same evidence would burn the same
hour we did chasing JWT validation, proxy header forwarding, and CORS
ghosts.

The linked-backend feature is useful for the SWA → ACA pattern Microsoft
documents (SWA does the auth, calls the Container App as a private
backend). It becomes a footgun when you stop using the SWA as the
public front door but leave the linked-backend resource in place — the
Container App is now reachable both via the SWA (where Easy Auth is
helpful) and via the new direct path (where Easy Auth is a 401
generator).

Microsoft's docs cover this in passing under "Bring your own Functions
backend" but do not warn about the trapdoor when the front-end changes.

## When to Apply

- Migrating a Container App from "behind a SWA" to "publicly exposed"
  (or any other direct-call pattern)
- Decommissioning a SWA but keeping its Container App backend in service
- Diagnosing a Container App that returns 401 with no visible auth
  configuration in your own Terraform

## Examples

### Confirming the symptom

```bash
# Before fix — Easy Auth is implicitly on:
az containerapp auth show -n jp-adopt-core-api-production -g rg-jp-adopt-core-production \
  --query "properties.platform.enabled" -o tsv
# → true

# After unlink + auth disable:
az containerapp auth show -n jp-adopt-core-api-production -g rg-jp-adopt-core-production \
  --query "properties.platform.enabled" -o tsv
# → false
```

### Codifying the fix in Terraform

The original linked-backend resource (delete this when migrating away
from SWA-fronted access):

```hcl
# REMOVE this resource — its presence flips Easy Auth on at the Container App.
resource "azapi_resource" "linked_backend" {
  type      = "Microsoft.Web/staticSites/linkedBackends@2022-03-01"
  name      = local.swa_linked_backend_name
  parent_id = azurerm_static_web_app.frontend.id
  body = jsonencode({
    properties = {
      backendResourceId = azurerm_container_app.api.id
      region            = azurerm_container_app.api.location
    }
  })
}
```

After removal, `terraform apply` will:
1. Delete the `Microsoft.Web/staticSites/linkedBackends` resource.
2. Leave the Container App's auth config in place (Terraform doesn't
   own it). You must explicitly disable it via `az containerapp auth
   update --enabled false` once, as a one-time cleanup.

## References

- jp-infrastructure PR #184 — Terraform removal of the linked-backend
  resource
- jp-adopt-core PR #66 — Phase 1 SWA → Container App migration
- [Microsoft docs — Bring your own Functions backend with Static Web
  Apps](https://learn.microsoft.com/en-us/azure/static-web-apps/apis-functions)
  (covers the integration but not the silent Easy Auth side-effect)
