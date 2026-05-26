---
title: "feat: Entra direct sign-in for staff (Phase 2 / jp-adopt-core#60)"
type: feat
status: active
created: 2026-05-26
plan_depth: Standard
origin: https://github.com/joshua-project/jp-adopt-core/issues/60
---

# Entra Direct Sign-In for JP Staff — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement jp-adopt-core#60 as the **launch auth** for adopt-core's staff web — staff sign in with their `@joshuaproject.net` Microsoft account, get a JWT the API already validates, and reach the dashboard.

**Architecture:** Single-tenant Entra direct via MSAL v5 PKCE. The API already dispatches Entra-issued JWTs (`apps/api/src/jp_adopt_api/auth.py:_ENTRA_ISSUER_RE` → `auth_entra.py:decode_entra_direct_token`, validating against a `partner_tenants` allowlist with `aud = api://jp-adopt-core`). Two new Entra app registrations land in jp-infrastructure: an **API app reg** (exposes the `api.access` scope under `api://jp-adopt-core`) and a **SPA app reg** (PKCE, redirect URIs for both the ACA FQDN and the future custom domain). The web app gets a new Entra MSAL config that replaces the dead B2C scaffolding, a `/signin` page, a `/auth/callback` handler, a client-side auth gate, and a tweak to `api-client.ts` that puts the MSAL-acquired access token on every API request. A new Alembic migration seeds the JP tenant in `partner_tenants`. The dev-token textbox stays dead-code-eliminated by the existing `NODE_ENV === "production"` gate.

**Tech Stack:** Next.js 15 App Router (standalone), MSAL v5 (`@azure/msal-browser` + `@azure/msal-react` already in `apps/web/package.json`), Azure Entra ID (single-tenant, JP tenant `761e2c5f-34bd-4872-b86c-3a9f3b29d63a`), Terraform + Terramate (`stacks/azure/entra/`), Alembic (idempotent data migration), GitHub Actions OIDC.

**Supersedes:** the implicit "magic-link UI at launch" framing in [docs/runbooks/multi-idp-b2c.md](../../runbooks/multi-idp-b2c.md). Magic-link API endpoints stay live as a non-UI fallback for cases where Entra isn't an option (e.g., facilitators on personal email), but adopt-core's web UI uses Entra direct only.

**Reversibility:** Each part is reversible. The infra app regs can be deleted (no downstream-state coupling). The web changes can be reverted; until then, the dev-token textbox still exists in non-production builds. The Alembic seed is a single idempotent insert and can be undone with a downgrade. The deploy.yml build-arg change is local to the `build-web` job. No data is destroyed.

---

## Cross-repo note

- **Part A** executes in **jp-infrastructure** (Terraform/Terramate). Must apply before Part B's deploy can use the resulting client IDs.
- **Parts B, C, D** execute in this repo (jp-adopt-core).
- **Part E** is end-to-end verification on the live ACA FQDN.
- **Part F** is explicitly deferred follow-up work.

## Auth contract (the single source of truth all tasks must match)

| Layer | Value |
|---|---|
| JP Entra tenant ID | `761e2c5f-34bd-4872-b86c-3a9f3b29d63a` |
| API app reg — Application ID URI | `api://jp-adopt-core` |
| API app reg — exposed scope | `api.access` (full id: `api://jp-adopt-core/api.access`) |
| API expected `aud` (incoming JWT) | `api://jp-adopt-core` (matches `settings.entra_direct_audience` default) |
| API expected `iss` | `https://login.microsoftonline.com/761e2c5f-34bd-4872-b86c-3a9f3b29d63a/v2.0` |
| API expected `tid` | `761e2c5f-34bd-4872-b86c-3a9f3b29d63a` (looked up in `partner_tenants`) |
| SPA app reg — `single_page_application.redirect_uris` | both `https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/auth/callback` AND `https://adoption.joshuaproject.net/auth/callback` |
| SPA app reg — **user assignment required** | `true` — only explicitly-assigned users (not every JP-tenant account) can consent and obtain tokens. Belt-and-suspenders with the role check below. |
| SPA — MSAL sign-in scopes (`loginRedirect`) | `["openid", "profile", "email", "User.Read"]` |
| SPA — MSAL token-acquire scopes (`acquireTokenSilent` for API calls) | `["api://jp-adopt-core/api.access"]` |
| SPA — MSAL token-refresh fallback | `acquireTokenRedirect` (NOT `acquireTokenPopup` — the existing `api-client.ts` uses popup; this plan replaces it because popup is blocked for non-user-gesture refreshes in modern browsers). |
| SPA — `knownAuthorities` | `["login.microsoftonline.com"]` |
| SPA — authority | `https://login.microsoftonline.com/761e2c5f-34bd-4872-b86c-3a9f3b29d63a` |
| `MsalClientProvider` redirectUri override | **must be removed** — the existing component unconditionally sets `redirectUri` to `window.location.origin` (bare), which would mismatch the registered `/auth/callback` URIs and trigger `AADSTS50011`. The new Entra config sets the full callback URI; the provider must honor it. |
| `partner_tenants` row | `(microsoft_tenant_id='761e2c5f-...', partner_id='joshua-project', partner_name='Joshua Project')`, idempotent insert (uses Python `uuid4()` bound param — NOT `gen_random_uuid()` — to avoid pgcrypto/PG-version dependencies). |
| `user_roles` schema | rename `user_b2c_subject_id` → `user_subject_id` (the column was named for B2C; Entra OIDs need a generic name). Existing data preserved by `op.alter_column`. |
| Authz model (defense-in-depth) | Two-and-a-half gates: (1) `partner_tenants` tenant allowlist (existing); (2) SPA service principal `app_role_assignment_required = true` so unassigned JP-tenant users are blocked at Entra (effectiveness for SPA-PKCE flows verified by U18's negative test — if it doesn't hold, that's documented and gate 3 carries the load); (3) row-level `user_roles` entry keyed on `user_subject_id = <Entra OID>`, enforced via `require_role(...)` on every data-bearing route. **No router uses bare `CurrentUser` after U22.** Routers that use `CurrentUserWithRoles` + inline role checks (matches.py, workflow.py) are functionally equivalent and verified by the audit table in U22. |
| Build args (baked into web image at `build-web`) | `NEXT_PUBLIC_AZURE_AD_TENANT_ID=761e2c5f-...` + `NEXT_PUBLIC_AZURE_AD_CLIENT_ID=${{ vars.NEXT_PUBLIC_AZURE_AD_CLIENT_ID }}` (SPA app reg client ID, public — not a secret) |
| Dev-token UI gate | `isDevTokenUiEnabled()` already returns `false` when `NODE_ENV === "production"` (build-time, dead-code-eliminated). No new gate needed; **verify and preserve**. |
| FastAPI `/docs` exposure | **disabled in production** (`docs_url=None, redoc_url=None, openapi_url=None` when `APP_ENV=="production"`). Was Part F; promoted to Part C (U23) because the new public proxy makes the API surface enumerable without auth. |

---

## High-level technical design

The full sign-in roundtrip, framed as directional guidance for review (not a copy-paste implementation):

```text
Browser                       Next server (web ACA)           Entra ID                  API (internal ACA)
   |                                  |                          |                             |
1. GET /                              | (no MSAL account)         |                             |
   |---layout RequireAuth wrapper---->|                          |                             |
   |   render <Redirect to=/signin/>  |                          |                             |
   |<---------------------------------|                          |                             |
2. GET /signin                        |                          |                             |
   |   <button onClick=loginRedirect>|                          |                             |
   |   user clicks ----------------->                          |                             |
3. instance.loginRedirect({ scopes: ["openid","profile","email","User.Read"] })
   |   browser → Entra ----------------------------------------> |                             |
   |   user signs in with @joshuaproject.net                     |                             |
   |   Entra redirects back ←----------------------------------- |                             |
4. GET /auth/callback?code=...        |                          |                             |
   |   client mount → handleRedirectPromise()                    |                             |
   |   MSAL exchanges code for tokens (PKCE) → cache             |                             |
   |   router.push("/")                                          |                             |
5. GET / (active MSAL account present)|                          |                             |
   |   RequireAuth renders <HomeDashboard />                     |                             |
6. fetch("/api/v1/contacts", ...)     |                          |                             |
   |   api-client.request() →                                    |                             |
   |     acquireTokenSilent({ scopes: ["api://jp-adopt-core/api.access"] })                    |
   |     Authorization: Bearer <accessToken>                     |                             |
   |   Next rewrites /api/* → internal API ------------------------------------------------->  |
   |                                                            |   API auth.py dispatches:   |
   |                                                            |    iss matches Entra regex  |
   |                                                            |    → decode_entra_direct    |
   |                                                            |    tid lookup partner_tenants
   |                                                            |    aud == api://jp-adopt-core
   |                                                            |    → 200 OK                 |
   |<-----------------------------------------------------------------------------------------|
```

This communicates the intended approach; the implementing agent should treat it as context, not code to reproduce.

---

## File structure

**jp-infrastructure (Part A):**
- Create: `stacks/azure/entra/jp-adopt-core-sso/stack.tm.hcl` (Terramate stack scaffold)
- Create: `stacks/azure/entra/jp-adopt-core-sso/generate.tm.hcl` (the Terramate generator — the two app regs + role assignments + outputs)
- Create: `stacks/azure/entra/jp-adopt-core-sso/outputs.tf` (hand-written outputs file consumed by `terraform output`)
- Generated (by `terramate generate`): `stacks/azure/entra/jp-adopt-core-sso/_terramate_generated_main.tf`, `_terramate_generated_provider.tf`

**jp-adopt-core (Parts B, C, D):**

Web (Part B):
- Create: `apps/web/src/lib/msalConfig.ts` — Entra single-tenant MSAL v5 config builder (flat path; no `entra/` subdirectory — only one file, no nesting earned)
- Delete: `apps/web/src/lib/b2c/msalConfig.ts` — dead B2C scaffolding; replaced
- Modify: `apps/web/src/components/MsalClientProvider.tsx` — gate on `isEntraClientConfigured()`; import from `../lib/msalConfig`; **stop overriding `redirectUri` to the bare origin** (let the new Entra config's `${origin}/auth/callback` be honored — otherwise Entra rejects with `AADSTS50011`)
- Create: `apps/web/app/signin/page.tsx` — public sign-in page (no auth gate); inlines the "Sign in with Microsoft" client component (no separate `SignInButton.tsx` — one consumer, one file)
- Create: `apps/web/app/auth/callback/page.tsx` — MSAL redirect-return handler (inlined as a `"use client"` page, no separate `AuthCallback.tsx`)
- Create: `apps/web/src/components/RequireAuth.tsx` — client-side auth gate
- Modify: `apps/web/app/layout.tsx` — wrap children in `RequireAuth` (excluding /signin and /auth/callback via the gate's internal pathname check)
- Modify: `apps/web/src/lib/api-client.ts` — `request()` acquires an access token via `acquireTokenSilent` (scope `api://jp-adopt-core/api.access`) and adds `Authorization: Bearer <accessToken>`; **replace `acquireTokenPopup` with `acquireTokenRedirect`** as the `InteractionRequiredAuthError` fallback
- Modify: `apps/web/src/components/ContactsB2C.tsx` — rename to `Contacts.tsx`; drop its local `resolveAccessToken` + B2C imports; call `apiFetch` from `api-client.ts`
- Delete: `apps/web/src/components/ContactsDevOnly.tsx` — no longer needed once `RequireAuth` gates the layout
- Modify: `apps/web/app/contacts/page.tsx` — drop the `isB2cClientConfigured()` branch; render `<Contacts />` directly
- Modify: `apps/web/Dockerfile` — add `ARG NEXT_PUBLIC_AZURE_AD_TENANT_ID` and `ARG NEXT_PUBLIC_AZURE_AD_CLIENT_ID` (both stages); drop the B2C `ARG`s; matching `ENV` in build stage
- Modify: `.github/workflows/deploy.yml` — `build-web` job: add the two new `NEXT_PUBLIC_AZURE_AD_*` build-args; drop the B2C ones

API + DB (Part C):
- Create: `apps/api/alembic/versions/2026_05_26_NNNN_seed_partner_tenants_joshua_project.py` — idempotent insert into `partner_tenants` (Python `uuid4()` bind param)
- Create: `apps/api/alembic/versions/2026_05_26_NNNN_rename_user_b2c_subject_id.py` — single revision; `op.alter_column` on BOTH `user_roles` AND `facilitator_org_membership`
- Create: `apps/api/alembic/versions/2026_05_26_NNNN_seed_staff_user_roles.py` — idempotent insert of staff Entra OIDs with their roles (operator populates the OID/role list before merge using portal or `az ad user show`)
- Modify: `apps/api/src/jp_adopt_api/models.py` — rename BOTH `UserRole.user_b2c_subject_id` AND `FacilitatorOrgMembership.user_b2c_subject_id` fields → `user_subject_id`; update both `PrimaryKeyConstraint`s
- Modify: `apps/api/src/jp_adopt_api/deps.py` — update the `load_user_roles` join to use the renamed column
- Modify: `apps/api/src/jp_adopt_api/domain/digest.py` — both references (line ~149 `UserRole.user_b2c_subject_id`, line ~190 `FacilitatorOrgMembership.user_b2c_subject_id`) renamed to `user_subject_id`
- Modify: `apps/api/src/jp_adopt_api/routers/admin.py` — Pydantic field names on `FacilitatorMembershipCreateRequest` + `FacilitatorMembershipRead` renamed; DELETE path parameter renamed; all query-site references renamed (public API surface change — regen contracts)
- Modify: `apps/api/src/jp_adopt_api/routers/workflow.py` — `FacilitatorOrgMembership.user_b2c_subject_id` references renamed
- Modify: `apps/api/src/jp_adopt_api/routers/matches.py` — `FacilitatorOrgMembership.user_b2c_subject_id` references renamed
- Modify: `apps/api/src/jp_adopt_api/routers/contacts.py` — the 4 endpoints (list, status_counts, get, patch) replace `_user: CurrentUser` with `require_role(...)`. Other routers already protected — see U22 audit table.
- Modify: `apps/api/src/jp_adopt_api/main.py` — `docs_url`, `redoc_url`, `openapi_url` set to `None` when `settings.is_production` (NOT `app_env == "production"` — the canonical helper accepts both `"production"` and `"prod"`)

Docs:
- Update: `docs/runbooks/deploy.md`, `docs/runbooks/multi-idp-b2c.md` — reflect Entra-direct-as-launch-auth, the seeded JP tenant, the column rename, and the staff role-seed runbook

---

## Part A — jp-infrastructure (Terraform/Terramate)

> Execute in a jp-infrastructure session/branch. Follows the Terramate pattern in `stacks/azure/entra/link-hub/` (single-tenant app reg) and `stacks/azure/entra/dt-platform-sso/` (single-tenant + `requested_access_token_version = 2`). Differences are called out per task.

### U1. Stack scaffold

**Files:** Create `stacks/azure/entra/jp-adopt-core-sso/stack.tm.hcl`.

**Goal:** Register the new Terramate stack so `terramate generate` discovers it and so it gets its own state blob.

**Patterns to follow:** `stacks/azure/entra/dt-platform-sso/stack.tm.hcl` for the stack id/name/tags shape. Pick a fresh stack id (per the existing `a1b2c3d4-NNNN-...` convention used in this repo).

**Approach:** One-file scaffold. `id = "a1b2c3d4-NNNN-4000-8000-NNNNNNNNNNNN"`, `name = "production-azure-entra-jp-adopt-core-sso"`, tags `["entra", "production"]`. Same provider stack as the other entra stacks.

**Test scenarios:** none — pure scaffold.

**Verification:**
- `terramate list` includes the new stack path.
- `terramate generate` reports the new stack with the files it created (no errors).

### U2. API app registration (exposes `api.access` scope)

**Files:** Create `stacks/azure/entra/jp-adopt-core-sso/generate.tm.hcl` (add the API app reg block).

**Goal:** Create an Entra single-tenant app registration whose Application ID URI is `api://jp-adopt-core` and which exposes a delegated scope `api.access`. This is the resource the API validates `aud` against; no SPA can request the right token without it.

**Dependencies:** U1.

**Patterns to follow:** `stacks/azure/entra/link-hub/main.tf` (single-tenant `AzureADMyOrg`, `requested_access_token_version = 2`). Diverge in: do not add a `web {}` or `implicit_grant {}` block; this app reg is *only* an API surface, not a sign-in target.

**Approach:**
- `azuread_application "jp_adopt_core_api"` with `display_name = "jp-adopt-core-api"`, `sign_in_audience = "AzureADMyOrg"`, `identifier_uris = ["api://jp-adopt-core"]`.
- `api { requested_access_token_version = 2; oauth2_permission_scope { value = "api.access"; admin_consent_display_name = "Access jp-adopt-core API"; ... type = "User"; enabled = true } }`.
- No `required_resource_access` (this is the resource, not a consumer).
- `azuread_service_principal` for the app reg so the SPA can grant consent against it.

**Technical design (directional, not implementation):**

```hcl
# Sketch — actual Terramate HCL will use tm_hcl_expression() for cross-resource refs.
resource "azuread_application" "jp_adopt_core_api" {
  display_name     = "jp-adopt-core-api"
  sign_in_audience = "AzureADMyOrg"
  identifier_uris  = ["api://jp-adopt-core"]

  api {
    requested_access_token_version = 2
    oauth2_permission_scope {
      id                         = "<stable uuid>"  # set once; never regenerate
      value                      = "api.access"
      admin_consent_display_name = "Access jp-adopt-core API"
      admin_consent_description  = "Allow the application to call jp-adopt-core API as the signed-in user."
      user_consent_display_name  = "Access jp-adopt-core"
      user_consent_description   = "Allow the application to call jp-adopt-core API on your behalf."
      type                       = "User"
      enabled                    = true
    }
  }
}

resource "azuread_service_principal" "jp_adopt_core_api" {
  client_id = azuread_application.jp_adopt_core_api.client_id
}
```

The `oauth2_permission_scope.id` is a UUID that must be stable across applies (a re-roll forces consent re-grant). Generate it once and pin it.

**Test scenarios:**
- After apply: `az ad app list --filter "identifierUris/any(c:c eq 'api://jp-adopt-core')" --query "[].appId" -o tsv` returns one GUID.
- The exposed scope is visible: `az ad app show --id <appId> --query "api.oauth2PermissionScopes[?value=='api.access'].id" -o tsv` returns the pinned UUID.

**Verification:** `terraform plan` shows `+ azuread_application.jp_adopt_core_api` and `+ azuread_service_principal.jp_adopt_core_api`. Apply succeeds. The audience the API validates (`api://jp-adopt-core`) is now claimable.

### U3. SPA app registration (PKCE, two redirect URIs)

**Files:** Same `generate.tm.hcl` (append the SPA app reg block).

**Goal:** Create the browser-facing app registration. PKCE flow (no client secret). Single-tenant. Both ACA FQDN and custom-domain redirect URIs registered up front so the Part C domain cutover requires zero Entra change.

**Dependencies:** U2 (so the SPA can `required_resource_access` the API app's scope).

**Patterns to follow:** the Microsoft Entra docs `single_page_application` block in the `azuread_application` provider. None of the existing JP app regs use this block — adopt-core is the first.

**Approach:**
- `azuread_application "jp_adopt_core_spa"` with `display_name = "jp-adopt-core-web"`, `sign_in_audience = "AzureADMyOrg"`.
- `single_page_application { redirect_uris = [ aca_fqdn_callback, custom_domain_callback ] }`.
- `required_resource_access` × 2:
  1. Microsoft Graph (`00000003-0000-0000-c000-000000000000`) — delegated scopes `openid`, `email`, `profile`, `User.Read` (well-known UUIDs; lift from any existing JP app reg).
  2. The API app reg — `Scope`-type access to the `api.access` permission. **Cross-reference the scope ID from U2** so the SPA stays in lock-step with U2's pinned UUID. Use a filtered expression (NOT a positional `[0]` index on the `oauth2_permission_scope` set, since set ordering is hash-based and unstable if future scopes are added): `[for s in azuread_application.jp_adopt_core_api.api[0].oauth2_permission_scope : s.id if s.value == "api.access"][0]` (or `one(...)` if you prefer). Never inline a UUID literal here.
- `azuread_service_principal "jp_adopt_core_spa"` for the SPA app reg, with `app_role_assignment_required = true` so only operator-assigned users (not every JP-tenant account) can sign in. The operator assigns staff via `azuread_app_role_assignment` blocks or the Azure portal.

> **Important — do NOT set `api { requested_access_token_version = 2 }` on this SPA app reg.** That setting belongs on the API app reg (U2). Mis-applying it here produces v1 tokens with a GUID `aud` instead of `api://jp-adopt-core`, and every API call 401s.

**Technical design (directional):**

```hcl
resource "azuread_application" "jp_adopt_core_spa" {
  display_name     = "jp-adopt-core-web"
  sign_in_audience = "AzureADMyOrg"

  single_page_application {
    redirect_uris = [
      "https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/auth/callback",
      "https://adoption.joshuaproject.net/auth/callback",
    ]
  }

  required_resource_access {
    resource_app_id = "00000003-0000-0000-c000-000000000000"  # Microsoft Graph
    # openid, email, profile, User.Read — well-known UUIDs (mirror dt-platform-sso)
    resource_access { id = "<openid uuid>"; type = "Scope" }
    resource_access { id = "<email uuid>"; type = "Scope" }
    resource_access { id = "<profile uuid>"; type = "Scope" }
    resource_access { id = "<User.Read uuid>"; type = "Scope" }
  }

  required_resource_access {
    resource_app_id = azuread_application.jp_adopt_core_api.client_id
    resource_access {
      id   = "<api.access uuid from U2>"
      type = "Scope"
    }
  }
}
```

**Test scenarios:**
- After apply: `az ad app show --id <spa-appId> --query "spa.redirectUris" -o json` returns the two URIs.
- `az ad app show --id <spa-appId> --query "requiredResourceAccess[?resourceAppId=='$<api-appId>'].resourceAccess[0].id" -o tsv` returns the U2 scope UUID.

**Verification:** Plan diff shows the SPA app reg + a SP. Apply succeeds. Client ID becomes available as `azuread_application.jp_adopt_core_spa.client_id` (or via `terraform output -raw spa_client_id` once U4 lands).

### U4. Outputs

**Files:** Create `stacks/azure/entra/jp-adopt-core-sso/outputs.tf`.

**Goal:** Expose both client IDs and the tenant ID so downstream consumers (the operator who populates the GH variable + the README/runbook) can read them via `terraform output`.

**Dependencies:** U2, U3.

**Approach:** Three outputs: `api_client_id`, `spa_client_id`, `tenant_id`.

**Test scenarios:** none.

**Verification:** `terraform output -raw spa_client_id` returns the GUID after apply; `terraform output -raw api_client_id` returns the API app reg's GUID.

### U5. Apply + capture client IDs

**Files:** none (operator step).

**Goal:** Apply the new stack and capture both client IDs for use in Part B/D.

**Dependencies:** U1–U4.

**Approach:** Push the Terramate change → PR → CI runs `terraform plan` (Entra stack) → merge → `terraform apply` lands on main. Then:
- `terraform output -raw spa_client_id` → this is the **public** client ID baked into the web image (`vars.NEXT_PUBLIC_AZURE_AD_CLIENT_ID` in jp-adopt-core).
- `terraform output -raw api_client_id` → reference only; the API's `ENTRA_DIRECT_AUDIENCE` does not need this (the default `api://jp-adopt-core` is the right audience and is already what the API expects).

**Verification:** Both outputs return GUIDs; both app regs visible in the Azure portal under the JP tenant; the SPA app reg's "Authentication" blade shows the two redirect URIs under "Single-page application".

---

## Part B — jp-adopt-core web (MSAL + sign-in flow + auth gate)

### U6. Entra MSAL config (replaces B2C scaffolding)

**Files:**
- Create: `apps/web/src/lib/msalConfig.ts`
- Delete: `apps/web/src/lib/b2c/msalConfig.ts`

**Goal:** Provide a single source of truth for the Entra single-tenant MSAL v5 configuration. Mirror the v5 patterns already in the codebase (`MsalClientProvider` already migrated v3→v5).

**Dependencies:** U5 produces the SPA client ID.

**Patterns to follow:** the existing `apps/web/src/lib/b2c/msalConfig.ts` for the v5 idioms (initialize-await is enforced by `MsalClientProvider`; this file only builds the `Configuration`):
- `isEntraClientConfigured()` checks `NEXT_PUBLIC_AZURE_AD_TENANT_ID` + `NEXT_PUBLIC_AZURE_AD_CLIENT_ID` non-empty.
- `buildEntraConfiguration()` returns `Configuration | null`. Authority `https://login.microsoftonline.com/${tenantId}`, `knownAuthorities: ["login.microsoftonline.com"]`, `cacheLocation: "sessionStorage"`, `redirectUri` set to `${window.location.origin}/auth/callback` and `postLogoutRedirectUri` set to `${window.location.origin}/signin` (NOT the bare origin as in the B2C pattern — Entra rejects redirect URIs that don't exactly match the registered SPA callback path). Carry the `getSessionCorrelationId` helper over verbatim (per-tab UUID in sessionStorage; useful for cross-system debugging).
- Export the API scope as a constant: `export const API_ACCESS_SCOPES = ["api://jp-adopt-core/api.access"];` so callers don't hard-code it.
- Export sign-in scopes: `export const SIGNIN_SCOPES = ["openid", "profile", "email", "User.Read"];`.
- `isDevTokenUiEnabled()` (lift verbatim from the b2c file — it already gates on `NODE_ENV === "production"`; the existing behavior is what we want).

**Approach:** The Entra authority shape is **structurally different** from B2C (no `/<policy>/` segment, different `knownAuthorities`). Don't try to extend the B2C builder — drop a new file at `apps/web/src/lib/msalConfig.ts` (flat path, no `entra/` subdirectory — one file, no nesting earned), delete the b2c/ one.

**Technical design (directional):**

```ts
// apps/web/src/lib/msalConfig.ts — sketch
export const API_ACCESS_SCOPES = ["api://jp-adopt-core/api.access"];
export const SIGNIN_SCOPES = ["openid", "profile", "email", "User.Read"];

export function isEntraClientConfigured(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_AZURE_AD_TENANT_ID?.trim() &&
      process.env.NEXT_PUBLIC_AZURE_AD_CLIENT_ID?.trim(),
  );
}

export function buildEntraConfiguration(): Configuration | null {
  if (!isEntraClientConfigured()) return null;
  const tenantId = process.env.NEXT_PUBLIC_AZURE_AD_TENANT_ID!.trim();
  const clientId = process.env.NEXT_PUBLIC_AZURE_AD_CLIENT_ID!.trim();
  return {
    auth: {
      clientId,
      authority: `https://login.microsoftonline.com/${tenantId}`,
      knownAuthorities: ["login.microsoftonline.com"],
      redirectUri:
        typeof window !== "undefined" ? `${window.location.origin}/auth/callback` : undefined,
      postLogoutRedirectUri:
        typeof window !== "undefined" ? `${window.location.origin}/signin` : undefined,
    },
    cache: { cacheLocation: "sessionStorage" },
    system: {
      loggerOptions: {
        correlationId: getSessionCorrelationId(),
        logLevel: LogLevel.Warning,
        piiLoggingEnabled: false,
        loggerCallback: /* same shape as b2c */,
      },
    },
  };
}

export function isDevTokenUiEnabled(): boolean { /* lifted verbatim from the b2c file; already gates on NODE_ENV==="production" */ }
```

**Test scenarios:**
- Unit: `isEntraClientConfigured()` returns `false` when either env var is empty; `true` when both are set.
- Unit: `buildEntraConfiguration()` returns `null` when not configured; otherwise returns a valid `Configuration` with the right authority.
- Unit: `isDevTokenUiEnabled()` returns `false` when `NODE_ENV === "production"` (verifying the existing gate is preserved).

**Verification:** TypeScript compiles. `pnpm --filter web build` succeeds with the new env vars set. The B2C file is removed; nothing in `apps/web/src` imports from `../lib/b2c/`.

### U7. MsalClientProvider — switch to Entra

**Files:** Modify `apps/web/src/components/MsalClientProvider.tsx`.

**Goal:** Point the existing v5 provider at the new Entra config.

**Dependencies:** U6.

**Patterns to follow:** the file is already structured around `isB2cClientConfigured()` + `buildMsalConfiguration()` — same shape, just renamed imports and updated guard.

**Approach:**
1. Import changes: `../lib/b2c/msalConfig` → `../lib/msalConfig`; `isB2cClientConfigured` → `isEntraClientConfigured`; `buildMsalConfiguration` → `buildEntraConfiguration`. (Verify the exact existing export name in `b2c/msalConfig.ts` before doing the rename.)
2. **Remove the `redirectUri` override.** The current provider (`apps/web/src/components/MsalClientProvider.tsx` lines ~43–48) unconditionally sets `redirectUri: window.location.origin` (bare) and `postLogoutRedirectUri: window.location.origin` on the `PublicClientApplication` config. The new Entra config builder already sets these to `${origin}/auth/callback` and `${origin}/signin` respectively; the override must be deleted so Entra sees the registered `/auth/callback` URI and doesn't reject sign-in with `AADSTS50011`.
3. All other v5 patterns stay (`pca.initialize().then(...)` gate, `EventType.LOGIN_SUCCESS` callback, active-account propagation, `getSessionCorrelationId`).
4. Update the error message in `setInitError` to say "Entra" not "B2C".

**Test scenarios:**
- Unit/integration: rendering `<MsalClientProvider>` with both env vars set initializes MSAL and resolves `ready=true`.
- Unit/integration: rendering with neither env var set still resolves `ready=true` (the guard short-circuits, allowing dev without Entra).

**Verification:** `grep -rn "b2c" apps/web/src/components apps/web/src/lib` returns no matches (other than archived/comment references if any). The component compiles.

### U8. /signin page

**Files:** Create `apps/web/app/signin/page.tsx` (inlines the sign-in client logic — no separate `SignInButton.tsx`; only one consumer).

**Goal:** A public page (NOT behind `RequireAuth`) with a single "Sign in with Microsoft" button that initiates the MSAL `loginRedirect` flow.

**Dependencies:** U7.

**Patterns to follow:** existing `"use client"` components (e.g., `apps/web/src/components/ContactsB2C.tsx`) for the client-component shape. The page itself can stay a server component that renders the client component (mirrors `app/page.tsx` → `HomeDashboard`).

**Approach:**
- `app/signin/page.tsx` is a `"use client"` page containing the sign-in button inline (no separate `SignInButton.tsx` — one consumer, no extraction earned).
- The page uses `useMsal()` from `@azure/msal-react`; the button calls `instance.loginRedirect({ scopes: SIGNIN_SCOPES })`. On a fresh active account (after redirect-return), MSAL will fire `LOGIN_SUCCESS` and `MsalClientProvider` will set the active account.
- If MSAL is not initialized yet (`!ready` from provider), the button is disabled with a small "Loading…" label.
- If `!isEntraClientConfigured()`, show an inline error "Entra sign-in is not configured for this build" rather than a broken button.

**Test scenarios:**
- Unit: rendering `<SignInButton />` outside an MsalProvider raises (`useMsal` throws) — this is expected; document and rely on layout placement.
- Integration: clicking the button calls `instance.loginRedirect` once with the right scopes (`SIGNIN_SCOPES`).
- Integration: when MSAL is not yet initialized, the button is disabled.
- Integration: when Entra is not configured, the page renders an inline error and no clickable button.

**Verification:** Navigating to `/signin` in `next dev` (with Entra env vars set) renders a single "Sign in with Microsoft" button.

### U9. /auth/callback handler

**Files:** Create `apps/web/app/auth/callback/page.tsx` (inlined `"use client"` page — no separate `AuthCallback.tsx`).

**Goal:** Handle the MSAL redirect-return. PKCE code-exchange is done by MSAL itself via `instance.handleRedirectPromise()`; this page exists to call it, surface errors, and redirect to `/` (or the original target) on success.

**Dependencies:** U7.

**Patterns to follow:** the MSAL v5 redirect handler reference. The repo's `MsalClientProvider` does not call `handleRedirectPromise()` itself — that's deliberate; the explicit callback page makes the lifecycle visible. (The provider does set the active account via the `LOGIN_SUCCESS` event callback once MSAL resolves the code exchange, so the page just needs to await `handleRedirectPromise` and route.)

**Approach:**
- Inline `"use client"` page (no separate `AuthCallback.tsx` — one consumer).
- On mount, `useEffect` calls `instance.handleRedirectPromise({ navigateToLoginRequestUrl: false })`. The explicit `navigateToLoginRequestUrl: false` keeps MSAL from auto-navigating back to the original request URL (MSAL v5 moved this from a config-level setting to a per-call option); we always `router.push("/")` ourselves so the navigation contract is visible.
- On success, `router.push("/")`. Deep-linking back to the user's pre-auth target is out of scope here — `/` is fine for v1 (the dashboard is a discovery surface, not a deep-link target).
- On error, render the MSAL error in a small inline panel with a "Back to sign-in" link. Add `role="status"` + `aria-live="polite"` so a screen reader announces the in-flight-to-redirect transition.
- **Do NOT fire any analytics/error-reporting event from `useEffect` on this page before `handleRedirectPromise()` resolves and clears the URL hash.** The auth code lives in the URL fragment (`#code=...&state=...`) for the ~500ms PKCE-exchange window; any `posthog.capture`, `sentry.captureException`, or `gtag` call that reads `window.location.href` during mount could log the code. Keep this page deliberately instrumentation-free.
- No layout-level auth gate fires for this page (`RequireAuth` exempts `/auth/callback` and `/signin` by pathname).

**Test scenarios:**
- Integration: mounting `<AuthCallback />` calls `instance.handleRedirectPromise()` exactly once.
- Integration: on a successful redirect with a hash-encoded auth code, the component triggers `router.push("/")`.
- Integration: on `BrowserAuthError` (e.g., user cancelled), the component renders the error and a "Back to sign-in" link.
- Integration: hard refresh on `/auth/callback` with no hash (no redirect in progress) → renders a benign "Returning to home…" and pushes to `/`.

**Verification:** Sign-in roundtrip end-to-end in `next dev`: click sign-in, land on Entra, sign in, get redirected to `/auth/callback`, end up on `/` with an active MSAL account.

### U10. RequireAuth wrapper + layout integration

**Files:**
- Create: `apps/web/src/components/RequireAuth.tsx`
- Modify: `apps/web/app/layout.tsx`

**Goal:** Every non-public page renders `HomeDashboard` (or whatever) only after MSAL is ready AND there is an active account. Unauthenticated users get redirected to `/signin`. The dashboard never flickers.

**Dependencies:** U7, U8, U9.

**Patterns to follow:** the existing `MsalClientProvider`'s `ready` gate is the model — return early with a loading shell until MSAL is initialized.

**Approach:**
- `RequireAuth` is `"use client"`. Reads `useMsal()` + `useIsAuthenticated()` (the latter from `@azure/msal-react`). Reads `usePathname()` from `next/navigation`.
- Exempt paths: `/signin`, `/auth/callback`. For those, render children unchanged.
- For all other paths:
  - If MSAL is not yet initialized (no `instance` or `inProgress !== InteractionStatus.None`), render a centered loading shell (no flicker — never render the dashboard).
  - If MSAL is ready and there's an active account, render children.
  - If MSAL is ready and there is no active account, `router.replace("/signin")` (replace, not push, so back-button doesn't return to the gated page) and render the loading shell while the redirect lands.
- `layout.tsx` wraps `{children}` inside `MsalClientProvider` (existing) AND `RequireAuth` (new):
  ```tsx
  <MsalClientProvider>
    <RequireAuth>{children}</RequireAuth>
  </MsalClientProvider>
  ```

**Technical design — the no-flicker contract (directional):**

The gate exists in three states: `loading` (MSAL initializing), `unauthenticated` (initialized, no account), `authenticated` (initialized, account). The gate **never** renders `{children}` in the first two states for protected paths. The loading shell can be a centered `<div>` with the app title and a small spinner — anything stable and not branded with dashboard content.

**Test scenarios:**
- Integration: `/signin` and `/auth/callback` render their content immediately regardless of auth state (exempt paths).
- Integration: `/` while MSAL is initializing renders the loading shell, NOT `HomeDashboard`.
- Integration: `/` once initialized + no account → `router.replace("/signin")` is called; loading shell continues to render until the navigation lands.
- Integration: `/` once initialized + active account → `HomeDashboard` renders.
- Integration: clicking sign-out (deferred — see Part F) → redirected to `/signin` immediately.

**Verification:** Unauthenticated visit to `/` lands on `/signin` without ever showing the dashboard. Authenticated visit renders the dashboard with no white flash.

### U11. api-client.ts — attach MSAL access token

**Files:** Modify `apps/web/src/lib/api-client.ts`.

**Goal:** Every API call from the web app carries a valid Entra access token on `Authorization: Bearer ...`. The API already validates this — the web has been missing the carrier.

**Dependencies:** U6, U7.

**Patterns to follow:** the existing `request()` function in `api-client.ts` already accepts an `ApiClientContext { instance, accounts, devToken? }` and uses `resolveAccessToken(ctx)` to obtain a bearer. That helper currently returns either the dev-local token (when allowed) or attempts MSAL acquisition. The shape is right; the call needs to:
1. Use `API_ACCESS_SCOPES` from the new `lib/msalConfig` (not whatever B2C scope was there).
2. Prefer `acquireTokenSilent` against the active account; fall back to `acquireTokenRedirect` on `InteractionRequiredAuthError` (this is the standard MSAL v5 pattern — silent first, redirect on expiry).
3. Drop the dev-token branch ONLY when `isDevTokenUiEnabled()` is `false` (so dev-local still works in `next dev`).

**Approach:**
1. Update the MSAL imports + the `scopes` constant to `API_ACCESS_SCOPES` from `../lib/msalConfig`.
2. **REPLACE `acquireTokenPopup` with `acquireTokenRedirect`** as the `InteractionRequiredAuthError` fallback. The existing code (`api-client.ts:87`) uses popup; that is wrong for a redirect-flow SPA — modern browsers block popups not triggered by a user gesture, so a mid-session silent-refresh failure either silently dies or opens a popup with no context. `acquireTokenRedirect` navigates to Entra, the user re-authenticates, and lands back at `/auth/callback` → `/`. Document the UX cost in the risk table (lost form state on token expiry).
3. Keep the dev-token fallback (gated by `isDevTokenUiEnabled()`) for `pnpm dev` local work — dead-code-eliminated in production builds.
4. Keep `getBaseUrl()` unchanged (already correct from the web→ACA migration).

**Test scenarios:**
- Unit: `resolveAccessToken({ instance, accounts: [acct], devToken: undefined })` calls `instance.acquireTokenSilent({ scopes: API_ACCESS_SCOPES, account: acct })`.
- Unit: on `InteractionRequiredAuthError` from silent acquisition, falls back to `instance.acquireTokenRedirect(...)`.
- Unit: when `isDevTokenUiEnabled()` is true AND a `devToken` is set in `localStorage`, returns the dev token without calling MSAL.
- Unit: when `isDevTokenUiEnabled()` is false (production), `devToken` is ignored.
- Integration: a real `apiFetch()` call hitting a mock `/v1/contacts` carries `Authorization: Bearer <token-fixture>`.

**Verification:** With a real sign-in roundtrip completed, `fetch("/api/v1/contacts")` in the browser console returns 200; the network request shows `Authorization: Bearer ey...`.

### U12. Dev-token textbox — verify the existing gate

**Files:** Audit `apps/web/src/components/MsalClientProvider.tsx` + any other place the dev-token textbox is rendered (`isDevTokenUiEnabled` callers).

**Goal:** Confirm the existing `NODE_ENV === "production"` gate dead-code-eliminates the dev-token UI from production bundles, and that no new code path I'm adding re-introduces it.

**Dependencies:** U6, U7, U10, U11.

**Patterns to follow:** Next.js build constants — `process.env.NODE_ENV` is a compile-time replacement in client bundles, so `if (process.env.NODE_ENV === "production") return false` literally compiles out the branch.

**Approach:** No new code; just a verification step. After Part B is implemented, run a production-mode build and grep the output bundle for `dev-local` / "dev token" string literals; both should be absent. If present, find the leak and gate it.

**Test scenarios:**
- Build verification: `NODE_ENV=production NEXT_PUBLIC_AZURE_AD_TENANT_ID=... NEXT_PUBLIC_AZURE_AD_CLIENT_ID=... pnpm --filter web build` followed by `grep -r "dev-local" apps/web/.next/static/chunks/` returns zero matches.
- Build verification: same grep for the dev-token textbox copy ("dev token", "Bearer dev-local") returns zero matches in the production bundle.

**Verification:** Visiting `/signin` on the live ACA FQDN shows ONLY the "Sign in with Microsoft" button — no dev-token textbox.

### U24. Migrate B2C consumers (delete dev-only fallback, drop b2c imports) — Part B

**Files:**
- Delete: `apps/web/src/components/ContactsDevOnly.tsx`
- Modify: `apps/web/src/components/ContactsB2C.tsx` — rename to `Contacts.tsx`; drop the local `resolveAccessToken` + `getApiScopeList` + `isDevTokenUiEnabled` imports from `lib/b2c/msalConfig`; call `apiFetch` from `../lib/api-client` instead
- Modify: `apps/web/app/contacts/page.tsx` — drop the `isB2cClientConfigured()` branch; import `Contacts` from `../../src/components/Contacts`; render `<Contacts />` directly

**Goal:** Without this unit, the TypeScript build fails the moment U6 deletes `lib/b2c/msalConfig.ts` — three files import from it (`contacts/page.tsx`, `ContactsB2C.tsx`, `ContactsDevOnly.tsx`). Worse: the `isB2cClientConfigured()` branch in `contacts/page.tsx` evaluates to `false` post-migration (B2C env vars are gone), so the page would silently render `<ContactsDevOnly>` in production even after a successful Entra sign-in. The unit consolidates the contacts view onto the canonical `apiFetch` path and removes the dev-only fallback (which `RequireAuth` already supersedes).

**Dependencies:** U6, U7, U10, U11 (Entra config + provider + auth gate + api-client all in place first).

**Patterns to follow:** the simplification matches the rest of the repo — `app/<name>/page.tsx` renders a `Contacts` component, which uses the central `apiFetch` for all API calls (no per-component token logic).

**Approach:**
- Delete `ContactsDevOnly.tsx` outright. Its purpose was a dev-token textbox UI when B2C wasn't configured; `RequireAuth` now ensures all users have a real Entra session, so the fallback is dead code.
- Rename `ContactsB2C.tsx` → `Contacts.tsx`. Inside, replace the local `resolveAccessToken` block (which uses `getApiScopeList` from B2C) with a call to `apiFetch({ instance, accounts }, "/v1/contacts?limit=50")` — the centralized helper already handles the access-token acquisition via `acquireTokenSilent` with the new `API_ACCESS_SCOPES`.
- `contacts/page.tsx` drops the `isB2cClientConfigured()` branch and the `ContactsDevOnly` import; renders `<Contacts />`.

**Test scenarios:**
- Build: `pnpm --filter web build` succeeds after U6's deletion (no dangling `lib/b2c/` imports).
- Integration: `/contacts` renders the Contacts list (real data) after a successful Entra sign-in.
- Integration: there is no dev-token textbox anywhere in production, even if a developer pastes one into localStorage.

**Verification:** `grep -rn "lib/b2c" apps/web` returns zero matches. `/contacts` on the production URL loads real contact data for a staff user.

---

## Part C — jp-adopt-core API (data migrations + authz + docs disable)

### U13. Seed `partner_tenants` with the JP tenant

**Files:** Create `apps/api/alembic/versions/2026_05_26_NNNN_seed_partner_tenants_joshua_project.py`.

**Goal:** Insert the JP tenant row in `partner_tenants` so the API's `decode_entra_direct_token` doesn't return `403 tenant_not_provisioned` for `@joshuaproject.net` users. Idempotent: re-running is a no-op.

**Dependencies:** none (the `partner_tenants` table exists from a prior migration; this is a pure data insert).

**Patterns to follow:** the existing data-seeding migrations in `apps/api/alembic/versions/` (e.g., the foundation migration `20260515_0003_foundation_amy_return.py` lines around `INSERT INTO roles ...` which uses `op.execute` with a stable UUID for idempotency). Also `docs/solutions/conventions/alembic-migration-edit-after-apply-2026-05-20.md` — once applied, do **not** edit; new revision instead.

**Approach:**
- `revises = <head>` (current head migration; pick at write time).
- `def upgrade()`: generate the UUID **in Python** via `uuid.uuid4()` and pass as a bound param — do NOT use Postgres `gen_random_uuid()`. Rationale: `pgcrypto` is not enabled anywhere in this repo's migrations (verified by grep), and although `gen_random_uuid()` is core in PG14+, relying on the server version is needless coupling. The existing foundation-migration roles seed (`20260515_0003`) hardcodes UUIDs the same way — match that pattern. Shape: `op.execute(sa.text("INSERT INTO partner_tenants (id, microsoft_tenant_id, partner_id, partner_name) VALUES (:id, :tid, :pid, :pname) ON CONFLICT (microsoft_tenant_id) DO NOTHING").bindparams(id=str(uuid.uuid4()), tid='761e2c5f-...', pid='joshua-project', pname='Joshua Project'))`.
- `def downgrade()`: `op.execute("DELETE FROM partner_tenants WHERE microsoft_tenant_id = '761e2c5f-34bd-4872-b86c-3a9f3b29d63a'")`.

**Test scenarios:**
- Local: run `alembic upgrade head` against a fresh DB; query `SELECT count(*) FROM partner_tenants WHERE microsoft_tenant_id = '761e2c5f-34bd-4872-b86c-3a9f3b29d63a'` → 1.
- Local: re-run `alembic upgrade head` (no-op on already-upgraded); count remains 1 (idempotency).
- Local: `alembic downgrade -1` removes the row.

**Verification:**
- After CI's migrate step in the deploy run: `az containerapp exec --name jp-adopt-core-api-production -g rg-jp-adopt-core-production -- psql ... -c "SELECT * FROM partner_tenants"` (or via a Postgres client) shows the row.
- A live sign-in with a JP staff account no longer returns `403 tenant_not_provisioned`.

> **Note on revocation latency** (from the multi-idp-b2c runbook): deleting a `partner_tenants` row does **not** revoke existing token caches; the API's per-tid `PyJWKClient` keeps validating until the API process restarts. Not a concern for U13 (we're adding, not removing), but worth knowing for operator runbooks.

### U20. Rename `user_b2c_subject_id` → `user_subject_id` across BOTH tables + all callsites

**Files:**
- Create: `apps/api/alembic/versions/2026_05_26_NNNN_rename_user_b2c_subject_id.py` — single revision; two `op.alter_column` calls (one per table).
- Modify: `apps/api/src/jp_adopt_api/models.py` — rename **both** `UserRole.user_b2c_subject_id` AND `FacilitatorOrgMembership.user_b2c_subject_id` fields + the matching `PrimaryKeyConstraint`s.
- Modify: `apps/api/src/jp_adopt_api/deps.py` — `load_user_roles` join uses the new column name.
- Modify: `apps/api/src/jp_adopt_api/domain/digest.py` — line ~149 (`UserRole.user_b2c_subject_id` reference) and line ~190 (`FacilitatorOrgMembership.user_b2c_subject_id` reference) updated.
- Modify: `apps/api/src/jp_adopt_api/routers/admin.py` — request/response Pydantic field `user_b2c_subject_id` → `user_subject_id` on `FacilitatorMembershipCreateRequest` and `FacilitatorMembershipRead`; path parameter on `DELETE /v1/admin/facilitator-memberships/{user_b2c_subject_id}/{facilitator_org_id}` → `/{user_subject_id}/{facilitator_org_id}`; all query-site references (`FacilitatorOrgMembership.user_b2c_subject_id == ...`) updated.
- Modify: `apps/api/src/jp_adopt_api/routers/workflow.py` — line ~101 (`FacilitatorOrgMembership.user_b2c_subject_id == user_sub`) updated.
- Modify: `apps/api/src/jp_adopt_api/routers/matches.py` — line ~211 (same pattern) updated.

**Goal:** Decouple identity from B2C-era naming everywhere it appears in the codebase. Without the second-table rename, the admin API's public schema still says `user_b2c_subject_id`, and the facilitator org-scope guards in `workflow.py`/`matches.py` compare against a column whose value will increasingly hold Entra OIDs (semantic drift between column name and contents). Without this, every Entra-authenticated user gets an empty role set on the `user_roles` side (the original blocker), AND facilitator OIDs are written into a misleadingly-named column.

**Dependencies:** none (the tables exist; this renames columns + every ORM/route reference).

**Patterns to follow:** Alembic `op.alter_column` with `new_column_name`. The existing PK constraint names should be inspected before the migration writes (and renamed via `op.execute("ALTER TABLE ... RENAME CONSTRAINT ...")` if needed).

**Approach (single Alembic revision):**
- Migration `upgrade()`:
  ```python
  op.alter_column('user_roles', 'user_b2c_subject_id', new_column_name='user_subject_id')
  op.alter_column('facilitator_org_membership', 'user_b2c_subject_id', new_column_name='user_subject_id')
  ```
  Both are in-place; existing rows are preserved.
- Migration `downgrade()`: reverse both alters.
- `models.py`: rename the field on `UserRole` AND `FacilitatorOrgMembership`; update both `PrimaryKeyConstraint`s.
- `deps.py`: `load_user_roles`'s `where(UserRole.user_b2c_subject_id == user_sub)` → `where(UserRole.user_subject_id == user_sub)`.
- `domain/digest.py`: BOTH references (line ~149 `select(Role.name, UserRole.user_b2c_subject_id)`; line ~190 `FacilitatorOrgMembership.user_b2c_subject_id`) updated.
- `routers/admin.py`: Pydantic field names on `FacilitatorMembershipCreateRequest` / `FacilitatorMembershipRead` updated (`user_b2c_subject_id` → `user_subject_id`); path parameter on `DELETE` route updated; every `FacilitatorOrgMembership.user_b2c_subject_id` reference (lines 61, 69, 127, 147, 151, 162 in current `admin.py`) updated; **regenerate the OpenAPI contracts artifact** (`pnpm contracts:generate`) since this is a public API surface change.
- `routers/workflow.py`, `routers/matches.py`: every `FacilitatorOrgMembership.user_b2c_subject_id` reference updated.

> **Atomicity:** The migration + every ORM rename + every router/domain rename must ship in **one** PR / one deployment. A partial deploy (migration applied but the API container still references the old attribute) raises `AttributeError` or `ProgrammingError` on every protected request — 500s instead of 403s, with no graceful path. CI's `migrate` job runs before `deploy-api` in deploy.yml, so atomicity at the PR boundary is the gate.

**Test scenarios:**
- Local: `alembic upgrade head` succeeds; `\d user_roles` and `\d facilitator_org_membership` both show `user_subject_id`.
- Local: `alembic downgrade -1` restores both column names.
- Unit: `load_user_roles` with a matching subject returns the expected role set post-rename.
- Integration: existing facilitator workflow + matches tests pass (the org-scope guards still gate correctly under the renamed column).
- Build: `pnpm contracts:generate` succeeds; the OpenAPI artifact shows `user_subject_id` everywhere `user_b2c_subject_id` previously appeared.
- Regression: any test that explicitly posts `user_b2c_subject_id` to admin endpoints must be updated.

**Verification:** `grep -rn "user_b2c_subject_id" apps/api` returns zero matches after the unit lands. CI's contracts check passes.

### U21. Seed staff Entra OIDs in `user_roles`

**Files:** Create `apps/api/alembic/versions/2026_05_26_NNNN_seed_staff_user_roles.py`.

**Goal:** Without this, U20's rename is functional but no Entra-authenticated user actually has roles, so every `require_role`-gated endpoint 403s. Seed at least one `staff_admin` so the system is operable post-launch.

**Dependencies:** U20 (column must be renamed first).

**Approach:**
- The operator pre-populates a small dict mapping JP staff `@joshuaproject.net` Entra OIDs → role names (e.g., `{joel_oid: "staff_admin", ...}`). For initial launch, a single `staff_admin` row is sufficient (operator can grant more via direct SQL or the Part F admin UI).
- The OID for a given user is obtained **before** writing the migration via one of: the Azure portal (`https://portal.azure.com → Microsoft Entra → Users → <user> → Overview → Object ID`), or `az ad user show --id <upn>@joshuaproject.net --query id -o tsv`. *Do NOT* attempt to obtain it via "sign in and read AuthUser.sub from a debug endpoint" — that path is circular (it requires U13 + this migration to be applied first, which is what we're trying to write).
- Migration shape: `op.execute(sa.text("INSERT INTO user_roles (user_subject_id, role_id) SELECT :oid, id FROM roles WHERE name = :role ON CONFLICT DO NOTHING").bindparams(oid=..., role=...))`. Idempotent.
- Foundation migration roles (`20260515_0003`) already created the role rows with deterministic UUIDs — this seed just joins to them by name.

**Test scenarios:**
- Local: `alembic upgrade head` inserts the operator-listed rows; re-running is a no-op (`ON CONFLICT DO NOTHING`).
- Unit: `load_user_roles(<seeded OID>)` returns the seeded role set.

**Verification:** Sign-in as a seeded staff user → `load_user_roles` returns non-empty → `require_role`-gated endpoints return 200.

### U22. Promote contacts router endpoints to `require_role`

**Files:** Modify `apps/api/src/jp_adopt_api/routers/contacts.py` (the 4 specific endpoints below; other routers already use `require_role` or equivalent inline checks — confirmed by audit).

**Goal:** Tenant allowlist (`partner_tenants`) admits any JP-tenant Entra account. Without a role check at the route layer, a `@joshuaproject.net` account with no role assignment can read/write contacts. This unit closes that gap on the four `contacts.py` endpoints that currently use bare `CurrentUser`.

**Dependencies:** U20, U21.

**Patterns to follow:** other routers in this repo (admin, drips, manual_contacts) use `require_role(...)`; `matches.py` and `workflow.py` use `CurrentUserWithRoles` + inline role checks (functionally equivalent — they raise 403 if the user lacks the right role). Mirror the `require_role` pattern in `contacts.py` to keep the contract uniform.

**Audit result (recorded so the implementer doesn't have to rederive):**

| Router | Pattern | Action |
|---|---|---|
| `routers/contacts.py` lines 53, 142, 197, 210 | bare `_user: CurrentUser` | **Replace with `require_role(...)`** (this unit) |
| `routers/matches.py` | `_user: CurrentUserWithRoles` + inline `if not roles & _QUEUE_ROLES: raise 403` | Already protected — no change |
| `routers/workflow.py` | `CurrentUserWithRoles` + inline check | Already protected — no change |
| `routers/admin.py` | `require_role(*_STAFF_ROLES)` | Already protected — no change |
| `routers/drips.py` | `require_role(...)` | Already protected — no change |
| `routers/manual_contacts.py` | `require_role(*_STAFF_ROLES)` | Already protected — no change |
| `routers/intake.py` | API-key auth (separate path, not `CurrentUser`) | Out of scope here |
| `routers/auth_magic_link.py` | unauthenticated by design (issues tokens) | Out of scope |
| `routers/health.py` | unauthenticated (probes) | Out of scope |

**Approach:**
- For each of the 4 `contacts.py` endpoints (list, status counts, get, patch): replace `_user: CurrentUser` with `_user: Annotated[AuthUser, Depends(require_role("staff_admin", ...))]` (or the canonical `require_role` shape — check `admin.py`'s `_STAFF_ROLES` constant).
- Pick role lists: writes (`patch`) require `staff_admin`; reads (`list`, `status_counts`, `get`) accept the broader `_STAFF_ROLES` set used by `admin.py` / `manual_contacts.py`.
- Update OpenAPI metadata where applicable.

**Test scenarios:**
- Integration: a request with a valid Entra JWT but **no** `user_roles` row → 403 `role_required` on all 4 contacts endpoints.
- Integration: a request with a valid Entra JWT AND a `staff_admin` role → 200.
- Integration: regression — `Bearer dev-local` in non-prod still works for the existing local-dev tests.
- Negative audit: `grep -rE "_user: CurrentUser\b" apps/api/src/jp_adopt_api/routers/` returns zero matches after this unit (the `\b` excludes `CurrentUserWithRoles`).

**Verification:** OpenAPI artifact regenerated; `pnpm contracts:generate` shows the new role requirements on the 4 contacts endpoints. CI's contracts check passes.

### U23. Disable FastAPI `/docs` and `/openapi.json` in production

**Files:** Modify `apps/api/src/jp_adopt_api/main.py`.

**Goal:** The new public proxy makes the API surface enumerable to any visitor; Swagger UI + the full OpenAPI schema were previously gated by the SWA linked-backend's auth (since removed). Close the information-disclosure path before launch.

**Dependencies:** none (independent change).

**Patterns to follow:** the existing `FastAPI(...)` initialization in `main.py` already accepts `docs_url`, `redoc_url`, `openapi_url`. Set them to `None` when `settings.app_env == "production"` (or equivalent — verify the canonical env-detection function in `config.py`).

**Approach:**
- Use the canonical helper: `is_prod = settings.is_production` (defined in `config.py`; accepts both `"production"` and `"prod"`). Do **not** write `settings.app_env == "production"` — the existing `_cors_params()` in `main.py` already uses `settings.is_production`; match that pattern.
- Pass `docs_url=None if is_prod else "/docs"`, `redoc_url=None if is_prod else "/redoc"`, `openapi_url=None if is_prod else "/openapi.json"` to `FastAPI(...)`.

**Test scenarios:**
- Local (`APP_ENV=local`): `curl http://localhost:8000/docs` returns 200 (Swagger UI).
- Production (`APP_ENV=production`): `curl https://<host>/api/docs` returns 404.
- Production: `curl https://<host>/api/openapi.json` returns 404.

**Verification:** Live curl on the ACA FQDN after deploy: `/api/docs`, `/api/redoc`, `/api/openapi.json` all return 404. `/api/healthz` continues to return 200.

---

## Part D — deploy.yml + Dockerfile (build-time env wiring)

### U14. Dockerfile — add Entra ARGs, drop B2C ARGs

**Files:** Modify `apps/web/Dockerfile`.

**Goal:** The web image must bake `NEXT_PUBLIC_AZURE_AD_TENANT_ID` and `NEXT_PUBLIC_AZURE_AD_CLIENT_ID`. Drop the obsolete `NEXT_PUBLIC_AZURE_AD_B2C_*` ARGs.

**Dependencies:** U6 (the new config reads these env vars).

**Patterns to follow:** the existing Dockerfile pattern for `API_PROXY_TARGET` (already correctly bakes at build): ARG declared at the top, re-imported in the build stage, set as ENV before `pnpm --filter web build`.

**Approach:**
- Top-level ARGs section: add `ARG NEXT_PUBLIC_AZURE_AD_TENANT_ID=` + `ARG NEXT_PUBLIC_AZURE_AD_CLIENT_ID=`. Remove `ARG NEXT_PUBLIC_AZURE_AD_B2C_CLIENT_ID`, `_TENANT_NAME`, `_TENANT_ID`, `_POLICY`, `_API_SCOPES`, `_KNOWN_AUTHORITIES`.
- Build stage: re-import the two new ARGs; set as ENV; remove the six B2C ENV lines.

**Test scenarios:** none direct; covered by E2E in Part E.

**Verification:** `docker build --build-arg NEXT_PUBLIC_AZURE_AD_TENANT_ID=761e2c5f-... --build-arg NEXT_PUBLIC_AZURE_AD_CLIENT_ID=<spa-id> -f apps/web/Dockerfile .` succeeds; `next build` inside the container reports the env vars baked.

### U15. deploy.yml — switch build-arg block, drop B2C, add Entra

**Files:** Modify `.github/workflows/deploy.yml` (the `build-web` job's `build-args:` block).

**Goal:** Pass the new build args; drop the obsolete B2C ones.

**Dependencies:** U14, U5 (SPA client ID exists). The GH repo variable `vars.NEXT_PUBLIC_AZURE_AD_CLIENT_ID` must be populated post-Part-A (operator step).

**Patterns to follow:** the existing `build-args:` block in `build-web` is the model — one `KEY=VALUE` per line, mapped to `vars.*` for environment-specific values.

**Approach:**
- Add:
  - `NEXT_PUBLIC_AZURE_AD_TENANT_ID=761e2c5f-34bd-4872-b86c-3a9f3b29d63a` (hardcoded — single-tenant, JP tenant; not env-specific).
  - `NEXT_PUBLIC_AZURE_AD_CLIENT_ID=${{ vars.NEXT_PUBLIC_AZURE_AD_CLIENT_ID }}` (GH repo variable, populated from U5's `terraform output -raw spa_client_id`).
- Remove all six `NEXT_PUBLIC_AZURE_AD_B2C_*` lines.

**Test scenarios:** `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"` exits 0 (YAML valid).

**Verification:** A merge to main triggers a deploy; `build-web` logs show the new args being passed to `docker build` and the B2C ones absent.

### U16. Populate GH repo variable

**Files:** none (operator step).

**Goal:** Make `vars.NEXT_PUBLIC_AZURE_AD_CLIENT_ID` available to `build-web`.

**Dependencies:** U5.

**Approach:** `gh variable set NEXT_PUBLIC_AZURE_AD_CLIENT_ID --body "<spa client id>" --repo joshua-project/jp-adopt-core`.

**Verification:** `gh variable list --repo joshua-project/jp-adopt-core` lists the new variable.

---

## Part E — Verification + rollout

> Requires Part A applied, Parts B-D merged, Part C migration in the `migrate` job, and U16's GH variable populated.

### U17. Local sign-in roundtrip (next dev)

- [ ] Set `NEXT_PUBLIC_AZURE_AD_TENANT_ID` + `NEXT_PUBLIC_AZURE_AD_CLIENT_ID` in `apps/web/.env.local` (the SPA app reg's client ID from U5, JP tenant ID).
- [ ] Add `http://localhost:3000/auth/callback` to the SPA app reg's redirect URIs (Azure portal, "Authentication" blade, "Single-page application") — temporary; remove or keep for ongoing dev.
- [ ] `pnpm run dev:stack` to bring up the API locally with the seed migration applied.
- [ ] In `apps/web`: `pnpm dev`. Visit `http://localhost:3000/`.
- [ ] Verify redirect to `/signin`. Click "Sign in with Microsoft". Sign in with a `@joshuaproject.net` account.
- [ ] Verify redirect to `/auth/callback`, then to `/`. Dashboard renders.
- [ ] Open browser dev tools → Network. Confirm a request to `/api/v1/contacts` carries `Authorization: Bearer ey...` and returns 200.

### U18. Cloud deploy + smoke (production ACA FQDN)

- [ ] Merge Parts B/C/D to main. CI's `Deploy` workflow runs.
- [ ] Confirm `build-web` logs show the new build args; the smoke job passes (web root 200, `/api/healthz` carries the SHA, `/api proxy reaches the API`).
- [ ] In an incognito browser, navigate to `https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/`. Verify redirect to `/signin`.
- [ ] Sign in with a JP staff account; verify the dashboard renders and at least one API-backed view (e.g., `/contacts`) loads real data.
- [ ] Confirm via the browser dev tools that no `Bearer dev-local` is sent on any request.
- [ ] Confirm the dev-token textbox does NOT appear anywhere.
- [ ] **Negative test for `app_role_assignment_required = true`:** sign in with a JP-tenant `@joshuaproject.net` account that has **NOT** been assigned to the SPA app registration. Verify Entra rejects at the authorization endpoint with an `AADSTS501051` (or similar "application is not assigned") error — i.e., the user never reaches `/auth/callback`. If Entra DOES issue a token (the SPA-PKCE enforcement is documented for confidential clients; the public-client behavior should be verified empirically), document that in the runbook and accept that `require_role` (U22) is the sole effective gate. Don't ship with the "belt-and-suspenders" claim unverified.
- [ ] **Negative test for `require_role`:** sign in with a JP-tenant account that IS assigned (passes the previous test) but has **no** `user_roles` row. Verify every `contacts.py` endpoint returns 403 `role_required`. Then add the user's OID + a `staff_admin` row, re-sign-in, verify 200.

### U19. Update operator docs

- [ ] `docs/runbooks/deploy.md`: add a brief "Auth" section noting Entra direct is the launch auth, the SPA app reg's redirect URIs, and the GH variable name.
- [ ] `docs/runbooks/multi-idp-b2c.md`: add the JP tenant to the seeded-tenants list; cross-reference to U13's migration revision.

---

## Part F — Deferred (out of scope here)

- [ ] Sign-out UI (`MsalProvider`'s `instance.logoutRedirect` + a header menu item). Easy follow-up; not blocking launch.
- [ ] Add the production custom-domain redirect URI when Part C of the web→ACA plan cuts the domain over (already registered in U3, so this is just a confirmation step at cutover).
- [ ] `RequireAuth` retrofit for static-only pages if any are added later that should remain public.
- [ ] Magic-link UI for non-staff users (separate scope; the API endpoints stay live).
- [ ] Account-collision path for the rare case a JP staff member's email also exists in some other tenant the API allows-lists later (the magic-link path has `account_resolution_conflict`; Entra direct does not). Not relevant at launch.
- [ ] Once the new `msalConfig.ts` has soaked, audit the codebase for residual references to `B2C` env vars and copy.
- [ ] **Admin UI to mint role assignments.** U21 seeds an initial set of staff OIDs via Alembic. Long-term, staff_admin users need a UI to assign roles to new hires; tracked separately (jp-adopt-core#59 for intake API keys is the adjacent pattern).
- [ ] **Design polish** (from doc review): hide `SiteHeader` on `/signin`; spec the loading-shell visual; pick Microsoft brand sign-in button vs plain text; sign-in button focus/aria-disabled/touch-target; callback `role="status"` live region; multi-account picker UX; copy for the in-flight callback state; cohesive theming for `/signin`.
- [ ] **Pre-launch audit** of current DT staff users' email domains. The "all staff have Entra accounts via `@joshuaproject.net`" assumption needs validation. For any non-JP-tenant staff: B2B guest invite into the JP Entra tenant (zero-code path).
- [ ] **CSP for the web Container App** — `cacheLocation: "sessionStorage"` exposes bearer tokens to same-origin XSS; a strict CSP at the ACA ingress (or via Next.js middleware) mitigates the XSS→token-theft vector.

---

## Alternative approaches considered

- **Azure SWA Easy Auth (`staticwebapp.config.json` `azureActiveDirectory`).** jp-link-hub uses this pattern and it's simpler — no client-side MSAL. **Rejected** because adopt-core runs as a Container App, not a Static Web App; SWA Easy Auth lives in the SWA edge layer, which we deliberately moved off of (see `docs/superpowers/plans/2026-05-24-web-on-container-app.md`).
- **Server-side cookie auth (e.g., NextAuth.js).** Standard for full-stack Next apps with their own backend. **Rejected** because adopt-core's API is a separate FastAPI service that validates JWTs directly; cookie-shaped auth would require a parallel cookie→JWT exchange layer with no obvious payoff. Also, the API's Entra dispatch is *built* — the cost is on the SPA side either way.
- **Keep B2C alive via an External ID tenant.** Phase 3 (jp-infrastructure#175). **Out of scope here.** External ID handles the general-public case (non-JP-staff users); for staff, Entra direct is correct and simpler.
- **Magic-link UI as the launch auth.** Was the original framing in jp-adopt-core#60. **Rejected** because (a) adopt-core's web is staff-only, so an email-link flow is unnecessary friction; (b) the magic-link UI was never built and would be net-new work; (c) JP staff all have Entra accounts via `@joshuaproject.net`. Magic-link API endpoints stay live for non-UI use.
- **Single Entra app registration with self-audience.** A SPA app reg can expose its own `api.access` scope under an `api://` URI of its own; the SPA then requests that scope and gets an access token. **Rejected** because for self-audience flows, MSAL issues an access token whose `aud` claim is the SPA's client-ID GUID, **not** the `api://jp-adopt-core` URI the API validates. Making it work would require either (a) changing the API's `entra_direct_audience` config to the SPA's GUID — which couples the API to whatever client ID Terraform happens to assign, and breaks on any app-reg recreation — or (b) overriding the audience via Entra's "Expose an API" with custom signing rules, which is more complex than the two-app-reg pattern. The clean separation (API resource exposing the scope; SPA client consuming it) is the documented Microsoft pattern for SPA + protected-API and is what every external Microsoft sample uses.

---

## Risk analysis & mitigation

| Risk | Likelihood | Mitigation |
|---|---|---|
| **A misconfigured redirect URI silently breaks sign-in.** | Medium — first SPA app reg in this repo. | U3 registers BOTH the ACA FQDN and the custom domain up front; U17 verifies on localhost; U18 verifies on the ACA FQDN before any user-visible cutover. |
| **The `partner_tenants` row is missing at sign-in time.** | Medium if U13 isn't deployed before the first sign-in attempt. | The migration runs in CI's `migrate` job before `deploy-api`; the deploy.yml ordering already ensures `migrate` precedes `deploy-api`, which precedes `smoke`. The 403 error message (`tenant_not_provisioned`) is explicit and matches the runbook. |
| **MSAL access-token expiry mid-session causes silent failures.** | Low (MSAL handles silent refresh) — but possible if the silent refresh fails. | U11's `resolveAccessToken` falls back to `acquireTokenRedirect` on `InteractionRequiredAuthError`; user sees the Entra page, signs in again, returns to the same place. |
| **Dev-token textbox leaks into production.** | Low (NODE_ENV gate is build-time). | U12 verifies via grep on the production bundle. |
| **The B2C scaffolding is deleted before any consumer is updated.** | Low — covered by typecheck. | U6 deletes the file in the same commit/PR that ships U7's updated imports. CI's typecheck fails if any consumer references the old paths. |
| **A staff member's `@joshuaproject.net` email also exists in an unrelated B2C tenant somewhere.** | Very low (B2C is dead). | Documented in multi-idp-b2c.md; not in scope at launch. |
| **An Entra outage during business hours locks staff out.** | Rare but real. | The magic-link API endpoints stay live as an emergency-access path (a staff member can hit `POST /v1/auth/magic-link/request` via curl + receive a link in email; this is operator-grade, not UI-grade). Documented in U19. |
| **A new redirect URI is needed at the Part C domain cutover.** | None (already registered in U3). | n/a. |
| **Token expiry mid-form causes redirect → lost form state.** | Medium (any staff member with a long edit session will eventually hit this). | U11 fallback is `acquireTokenRedirect`; after sign-in the user lands back at `/`, not the form they were editing. Acceptable for v1 — the alternative (`acquireTokenPopup`) is unreliable due to popup-blockers. Documented design choice; follow-up to consider client-side form-state preservation via `sessionStorage` if it bites in practice. |
| **Staff member with no `user_roles` row signs in successfully but every protected endpoint 403s.** | High (the seed in U21 is operator-driven; new staff onboarding has no automated path). | Documented as "expected on first sign-in"; U21 seeds known staff. Long-term mitigation is the admin-UI follow-up in Part F. |
| **`oauth2_permission_scope.id` UUID drift between U2 and U3.** | Low after this revision (U3 now cross-references via `azuread_application.jp_adopt_core_api.api[0].oauth2_permission_scope[0].id` instead of manual copy-paste). | n/a. |
| **`requested_access_token_version = 2` accidentally set on the SPA app reg.** | Low (explicit "do NOT set" callout in U3). | If it happens, every API call 401s with a v1 GUID `aud`. Detected by U17 local sign-in test. |
| **`user_subject_id` rename collides with an existing index name on `user_roles`.** | Low (foundation migration's PK is unnamed; column rename does not affect index identifiers). | U20's downgrade restores the original name; verify locally before applying to production. |

---

## Self-review

**Spec coverage (against jp-adopt-core#60):**
- "MSAL config additions" — covered by U6 (new `lib/msalConfig.ts`).
- "Sign-in UI" — covered by U8 (`/signin` page).
- "Alembic data migration to seed `partner_tenants`" — covered by U13.
- "Test plan: local / staging / production" — covered by U17 (local) and U18 (production). Staging is out of scope at this stage (no staging environment exists yet).
- "App registration in main Entra tenant" — covered by U2 (API app reg) AND U3 (SPA app reg). **The issue under-specified this** — #60 only mentioned one app reg; Part A discovery showed two are needed (the API's `aud = api://jp-adopt-core` requires an API app reg with that Application ID URI).
- "Add `NEXT_PUBLIC_*` env vars to the SWA build" — covered by U14+U15+U16. Updated for the web Container App (no more SWA).
- "Out of scope: External ID, social IdPs, magic-link removal" — preserved in Part F.

**Doc-review absorption (round 1, 2026-05-26):** Three launch-blockers + four P1 findings folded into the plan:
- *Authz bridge (BLOCKER 1):* added U20 (rename `user_b2c_subject_id` → `user_subject_id`), U21 (seed staff Entra OIDs), U22 (promote contacts router to `require_role(...)`). Auth contract table now states the two-gate model (tenant allowlist + row-level role).
- *MSAL `redirectUri` override (BLOCKER 2):* U7's Approach now explicitly removes the override in `MsalClientProvider`. Auth contract table calls it out.
- *B2C consumers (BLOCKER 3):* added U24 (delete `ContactsDevOnly.tsx`, rename `ContactsB2C.tsx` → `Contacts.tsx`, drop the `isB2cClientConfigured()` branch in `contacts/page.tsx`). File structure lists all three.
- *Popup → redirect (P1):* U11's Approach now explicitly replaces `acquireTokenPopup` with `acquireTokenRedirect`. Auth contract table calls out the redirect-flow fallback.
- *`gen_random_uuid()` dependency (P1):* U13 now uses Python `uuid4()` in `bindparams`, matching the foundation-migration pattern. Eliminates pgcrypto / PG-version coupling.
- */docs disable (P1):* promoted from Part F to Part C as U23.
- *Authz model decision (P1):* picked "User assignment required on SPA app reg" + `require_role` on contacts router. Both gates documented in the auth contract table and codified in U3 (`app_role_assignment_required = true`) and U22.

**Doc-review structural simplifications absorbed:**
- Flat `apps/web/src/lib/msalConfig.ts` (no `entra/` subdirectory — one file).
- Inline sign-in client logic in `app/signin/page.tsx` (no separate `SignInButton.tsx`).
- Inline auth-callback logic in `app/auth/callback/page.tsx` (no separate `AuthCallback.tsx`).

**Doc-review minor coherence fixes:** `outputs.tf` added to File structure (U4); U3 Verification uses `terraform output` form; U12 heading no longer says "harden"; U10 sign-out cross-ref points at Part F; Self-review no longer claims staging is "called out under Part E"; `_devTokenGate` ghost file removed from U6 sketch; `oauth2_permission_scope.id` now uses a Terraform cross-reference in U3.

**Adversarial-finding absorption:**
- `navigateToLoginRequestUrl: false` explicitly passed to `handleRedirectPromise()` in U9.
- Risk table now flags `requested_access_token_version = 2` placement (must be on API app reg, not SPA).
- Risk table now covers token-expiry-redirect form-state loss.

**Doc-review absorption (round 2, 2026-05-26):** All 5 of round 1's blockers/P1s confirmed RESOLVED by the round-1 revisions. Round 2 surfaced one new P0 + 6 P1s introduced by those revisions; all absorbed in this round:
- *FacilitatorOrgMembership column (new P0):* U20 expanded to cover BOTH `user_roles` AND `facilitator_org_membership` tables, including the admin.py public schema rename (request/response field + DELETE path parameter), the workflow.py + matches.py + digest.py callsites, and the contracts regeneration step. Auth contract row updated accordingly.
- *digest.py omission (P1):* added to U20's file list with both ORM-reference fixes.
- *U22 audit enumeration (P1):* U22 now contains the explicit audit table — only `contacts.py` (4 endpoints) needs the bare-`CurrentUser` → `require_role` swap; all other routers are already protected (verified). Auth contract narrowed to "no bare `CurrentUser`" with explicit acknowledgement that `CurrentUserWithRoles` + inline checks (matches.py, workflow.py) are functionally equivalent.
- *`app_role_assignment_required` verification (P1):* U18 now includes a negative-test step — sign in with an unassigned JP-tenant account, confirm Entra rejects at the authorization endpoint. If it doesn't, document and accept that `require_role` (U22) is the sole effective gate.
- *U21 OID-discovery chicken-and-egg (P1):* removed the "sign in and read from debug endpoint" path; kept portal + `az ad user show`.
- *U20 atomicity (P1):* explicit note added — migration + every ORM/router rename ships in one PR; the deploy.yml `migrate` job runs before `deploy-api`, so partial-deploy 500s are prevented by PR-level atomicity.
- *U23 `settings.is_production` (P1):* approach updated to use the canonical `settings.is_production` helper (matches the existing `_cors_params()` pattern).

**Round-2 advisory absorption:**
- `oauth2_permission_scope` cross-reference in U3 now uses a filter expression (`[for s in ... if s.value == "api.access"][0]`), not a positional `[0]` index on the set.
- `/auth/callback` page must not fire any analytics/error-reporting from `useEffect` before the URL hash is cleared (documented in U9's Approach).

**Round-2 coherence safe-autos:**
- U8 + U9 Files headers no longer list separate `SignInButton.tsx` / `AuthCallback.tsx` (inlined per Self-review decision).
- U8 Approach rewritten to describe inline implementation.
- U6 Approach + Patterns-to-follow updated to the flat `lib/msalConfig.ts` path; redirectUri described as `/auth/callback` (not bare origin).
- U11 Patterns-to-follow path updated to `lib/msalConfig`.
- U24 header tagged as Part B for unambiguous Part membership.

**Placeholder scan:** No TBDs. The two parameterized values are the `oauth2_permission_scope.id` UUID (U2 — stable UUID set at write time, do not regenerate) and the SPA client ID (U5 → U16 — captured from `terraform output`).

**Type/name consistency:** `api://jp-adopt-core` (App ID URI), `api.access` (scope name), `api://jp-adopt-core/api.access` (full scope string used in MSAL), `761e2c5f-34bd-4872-b86c-3a9f3b29d63a` (JP tenant ID), `NEXT_PUBLIC_AZURE_AD_TENANT_ID` / `NEXT_PUBLIC_AZURE_AD_CLIENT_ID` (env var names), `jp-adopt-core-api` / `jp-adopt-core-web` (app reg display names) — used identically across U2, U3, U6, U13, U14, U15.

**Cross-repo dependency:** U5 must complete before U16 (and therefore U18) can run. The plan calls this out explicitly. U13 must merge before U18 or the first sign-in 403s.

**Reversibility check:** Part A apps can be deleted with no orphan state. Part B file changes can be reverted. Part C migration has a `downgrade()`. Part D build args can be removed (Dockerfile still builds without them in dev). No data is destroyed.
