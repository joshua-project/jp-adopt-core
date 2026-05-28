---
title: Two-app-reg pattern for SPA + API in single-tenant Entra
date: 2026-05-28
module: jp-infrastructure/stacks/azure/entra
problem_type: architecture_pattern
component: authentication
severity: medium
applies_when:
  - "Building a single-page-app frontend (PKCE flow) that calls a separate API backend, both protected by Entra"
  - "Single-tenant only (your org's tenant — `sign_in_audience = AzureADMyOrg`)"
  - "API validates the JWT directly (does not rely on Easy Auth or an upstream gateway)"
related_components:
  - tooling
tags:
  - entra
  - azure-ad
  - oauth2
  - pkce
  - spa
  - app-registration
  - terraform
  - single-tenant
---

# Two-app-reg pattern for SPA + API in single-tenant Entra

## Context

A browser-facing single-page app (SPA) that calls a separate API backend
needs Entra to issue access tokens with the **API as the audience**, not
the SPA. The SPA cannot self-authenticate as its own resource — the
token it presents to the API must say "for the API" in the `aud` claim,
or the API's JWT validation rejects it.

This requires two Entra app registrations in a deliberate relationship.
Trying to do it with one app reg (or with the wrong shape on the two
regs) leads to a token whose `aud` is the SPA's client ID and an API
that 401s every call.

The first JP project to implement this pattern was `jp-adopt-core`
(2026-05-26). The pre-existing `dt-platform-sso` stack is a **different
shape** — a single app reg per environment for a WordPress server-side
OIDC login. That single-app pattern works for "server-side web app
that just needs to know who you are"; it does NOT work when the web
side is a separate process from the resource being called.

## Guidance

Use a two-app-reg Terraform module with this shape:

### Resource 1 — API app registration

```hcl
resource "azuread_application" "api" {
  display_name     = "<service>-api"
  sign_in_audience = "AzureADMyOrg"          # single-tenant
  identifier_uris  = ["api://<service>"]      # NOT consumed by v2 tokens; see below

  api {
    requested_access_token_version = 2        # v2 endpoint → aud = appId GUID

    oauth2_permission_scope {
      id    = "<stable UUID, generated ONCE>"  # do NOT regenerate
      value = "api.access"
      type  = "User"
      enabled = true
      admin_consent_display_name = "Access <service> API"
      admin_consent_description  = "..."
      user_consent_display_name  = "Access <service>"
      user_consent_description   = "..."
    }
  }
}

resource "azuread_service_principal" "api" {
  client_id = azuread_application.api.client_id
}
```

Critical:
- `requested_access_token_version = 2`. v2 is the default for new SPAs
  and provides optional claims, group support, etc. Forces v2 issuance
  for tokens requesting this resource.
- **Footgun:** the `aud` claim on issued v2 tokens is the resource's
  **appId GUID**, NOT the identifier URI. Code that validates against
  `api://<service>` will reject every token. See
  `docs/solutions/azure-entra-auth/v2-token-aud-is-appid-not-uri-2026-05-26.md`.
- The scope's `id` field must be a stable UUID. Letting Terraform
  regenerate it invalidates prior admin consent (Entra treats a new
  UUID as a new scope).

### Resource 2 — SPA app registration

```hcl
resource "azuread_application" "spa" {
  display_name     = "<service>-web"
  sign_in_audience = "AzureADMyOrg"

  single_page_application {                   # PKCE — NOT `web {}` block
    redirect_uris = [
      "https://<service>-web-production.<aca-env>.azurecontainerapps.io/auth/callback",
      "https://<service>.<your-domain>.net/auth/callback",
    ]
  }

  required_resource_access {                  # 1) Microsoft Graph for OIDC
    resource_app_id = data.azuread_application_published_app_ids.well_known.result["MicrosoftGraph"]
    resource_access {
      id   = data.azuread_service_principal.msgraph.oauth2_permission_scope_ids["openid"]
      type = "Scope"
    }
    resource_access {
      id   = data.azuread_service_principal.msgraph.oauth2_permission_scope_ids["email"]
      type = "Scope"
    }
    resource_access {
      id   = data.azuread_service_principal.msgraph.oauth2_permission_scope_ids["profile"]
      type = "Scope"
    }
    resource_access {
      id   = data.azuread_service_principal.msgraph.oauth2_permission_scope_ids["User.Read"]
      type = "Scope"
    }
  }

  required_resource_access {                  # 2) the API's scope
    resource_app_id = azuread_application.api.client_id

    resource_access {
      id   = tolist(azuread_application.api.api[0].oauth2_permission_scope)[0].id
      type = "Scope"
    }
  }
}

resource "azuread_service_principal" "spa" {
  client_id                    = azuread_application.spa.client_id
  app_role_assignment_required = true         # gate which users can sign in
}
```

Critical:
- Use the `single_page_application` block, NOT `web {}`. The `web {}`
  block expects a confidential client (server-side, with a secret); the
  SPA must use PKCE.
- **Do NOT set `api { requested_access_token_version = 2 }` on this SPA
  app reg.** That setting belongs on the API. Mis-applying it here
  yields v1 tokens with confusing audiences.
- `required_resource_access` × 2:
  1. Microsoft Graph — `openid`, `email`, `profile`, `User.Read`. These
     are admin-consent-free and let MSAL acquire ID tokens for the
     signed-in user's identity.
  2. The API app reg's `api.access` scope. This is what tells Entra
     "the SPA is allowed to request access tokens for the API."
- `app_role_assignment_required = true` on the SPA service principal
  gates sign-in to users explicitly assigned to the enterprise app —
  preventing every JP-tenant account from automatically getting a
  token. Pair with `Microsoft Graph appRoleAssignments` to grant
  specific users.

### Where to put the scope-id cross-reference

The two-app-reg pattern cross-references the API's scope `id` from
inside the SPA's `required_resource_access`. The expression
`tolist(azuread_application.api.api[0].oauth2_permission_scope)[0].id`
is correct but fragile if multiple scopes are added later (the `[0]`
becomes hash-ordered). A safer shape once you have multiple scopes is
a `locals` lookup keyed by scope `value`:

```hcl
locals {
  api_scope_ids = {
    for s in tolist(azuread_application.api.api[0].oauth2_permission_scope) :
    s.value => s.id
  }
}

# then:
resource_access {
  id   = local.api_scope_ids["api.access"]
  type = "Scope"
}
```

## Why This Matters

A single app reg trying to serve both SPA and API roles **doesn't
work** because the OAuth2 token endpoint refuses to issue access tokens
where the resource is the same as the client (`aud == client_id` is a
nonsense token). The OIDC spec requires that an access token's
audience be a different application than the client requesting it.

The two-app-reg shape is the right answer for any SPA-frontend +
API-backend setup, regardless of tenant model. It cleanly separates:
- **Who the user signs in to** (the SPA — what they see in the consent
  dialog and the redirect URI)
- **What they get an access token for** (the API — what the token's
  `aud` claim names)

Skipping this and trying to validate ID tokens against the SPA's
client ID at the API works in development but fails the moment the API
moves behind any sort of gateway, edge proxy, or different process,
because ID tokens are not meant for service-to-service calls.

## When to Apply

- New JP-internal SPA + API setup using single-tenant Entra (JP staff
  only) — copy `jp-adopt-core-sso` as the starting template.
- Multi-tenant Entra (B2B partner orgs) — same pattern, but
  `sign_in_audience = "AzureADMultipleOrgs"` on both regs, and the API
  validates a partner-tenant allowlist (see
  `apps/api/src/jp_adopt_api/auth_entra.py` and the `partner_tenants`
  table for the pattern).
- **Don't** use this for server-side OIDC web apps (e.g., WordPress +
  OIDC plugin). Those use a single app reg with the `web {}` block and
  a client secret — see `stacks/azure/entra/dt-platform-sso/main.tf`.

## Examples

### Implementations in the JP fleet

| Stack | Shape | Notes |
|---|---|---|
| `jp-infrastructure/stacks/azure/entra/jp-adopt-core-sso/` | Two-app-reg (this pattern) | `jp-adopt-core-api` + `jp-adopt-core-web`. Single-tenant, PKCE, v2 tokens. |
| `jp-infrastructure/stacks/azure/entra/dt-platform-sso/` | Single app reg | WordPress server-side OIDC. `web {}` block. `for_each` over staging + production. **Different shape — do not copy for SPA+API setups.** |

### MSAL client config that pairs with this pattern

```ts
// apps/web/src/lib/msalConfig.ts
export const SIGNIN_SCOPES = ["openid", "profile", "email", "User.Read"];
export const API_ACCESS_SCOPES = ["api://jp-adopt-core/api.access"];
//                                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
//                                  scope string format: "<resource>/<value>"
//                                  the RESOURCE half is the identifier URI;
//                                  the issued token's aud is still the GUID
```

The scope string passed to `acquireTokenSilent({ scopes: [...] })` uses
the identifier URI form (`api://jp-adopt-core/api.access`). Entra
resolves that to the API's appId at token-issuance time and writes the
GUID into `aud`. The SPA code never needs to know the GUID; the API
container does — set `ENTRA_DIRECT_AUDIENCE` to the GUID in deploy.yml.

## References

- `jp-infrastructure/stacks/azure/entra/jp-adopt-core-sso/main.tf` —
  canonical implementation
- `jp-infrastructure/stacks/azure/entra/dt-platform-sso/main.tf` —
  contrast (single-app-reg server-side OIDC pattern)
- `docs/solutions/azure-entra-auth/v2-token-aud-is-appid-not-uri-2026-05-26.md`
  — the related footgun on token audience
- `docs/runbooks/multi-idp-b2c.md` — the multi-IdP architecture
  including B2C and Entra direct
- `docs/superpowers/plans/2026-05-26-entra-direct-staff-auth.md` —
  the Phase 2 launch plan where this pattern landed
- [Microsoft docs — Single-page app
  authentication](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-spa-overview)
