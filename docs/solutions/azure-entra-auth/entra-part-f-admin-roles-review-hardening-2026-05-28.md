---
title: Entra Part F code review hardening — sign-out, admin user_roles API, AdminUserRoles UX
date: 2026-05-28
category: azure-entra-auth
module: entra-part-f
problem_type: best_practice
component: authentication
severity: medium
applies_when:
  - "Implementing MSAL sign-out in the staff web app alongside msalConfig.ts"
  - "Adding or reviewing staff_admin-gated admin endpoints that grant/revoke platform roles by Entra OID"
  - "Building admin UI that grants or revokes user_roles with confirm dialogs and DELETE semantics"
  - "Writing API tests for admin role endpoints using dev-local auth and monkeypatched load_user_roles"
related_components:
  - testing_framework
  - development_workflow
tags:
  - entra
  - msal
  - sign-out
  - staff-admin
  - user-roles
  - admin-api
  - uuid-validation
  - code-review
---

# Entra Part F code review hardening — sign-out, admin user_roles API, AdminUserRoles UX

## Context

PR #83 implemented Entra Part F (issue #76): MSAL sign-out in `SiteHeader`, `GET/POST/DELETE /v1/admin/user-roles`, and `/admin/users` UI. A structured code review flagged several P2/P3 issues before merge. The fix commit `ee1c1e4` addressed the safe items; verification was **22 pytest** in `test_admin_api.py` and a clean `pnpm run build` in `apps/web`.

Production sign-out E2E still depends on `post_logout_redirect_uris` in the Entra SPA app registration (`jp-infrastructure`); that infra gap is documented in the Part F plan, not fixed in this repo.

## Guidance

### MSAL sign-out must match `msalConfig.ts`

Use the active account and the same logout redirect as MSAL init — `${origin}/signin`, not bare `origin`:

```typescript
const account = instance.getActiveAccount() ?? accounts[0] ?? null;

void instance.logoutRedirect({
  account: account ?? undefined,
  postLogoutRedirectUri: origin ? `${origin}/signin` : undefined,
});
```

`apps/web/src/lib/msalConfig.ts` already registers `postLogoutRedirectUri` that way. A bare-origin override in `SiteHeader` diverges from Entra app registration and can cause logout failures (`AADSTS50011`-class errors).

### Admin API — Entra OID on `user_subject_id`

`user_subject_id` is the Entra object ID (UUID string). Validate on grant (and implicitly on DELETE path segments):

```python
_ENTRA_OID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

class UserRoleGrantRequest(BaseModel):
    user_subject_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=_ENTRA_OID_PATTERN,
    )
    role_id: uuid.UUID
```

Malformed IDs return **422** with a stable `detail.code` — do not use `user-{hex}` placeholders in real requests or tests.

### Admin API — revoke ordering

Self-revoke of `staff_admin` is a business rule on an **existing** grant. Order checks as:

1. Resolve role → **404** `role_not_found` if missing  
2. Load `UserRole` row → **404** `user_role_not_found` if missing  
3. If target is caller’s own `staff_admin` → **422** `self_revoke_forbidden`  
4. Delete + outbox `admin.role.revoked`

Checking self-revoke before the row exists incorrectly returns **422** when the grant was already removed.

### AdminUserRoles UX

- One `revokingKey` state keyed as `` `${user_subject_id}-${role_id}` `` (duplicate `useState` declarations break the web build).
- Disable revoke controls while any DELETE is in flight.
- Treat **404** `user_role_not_found` after revoke as success (idempotent UI).
- `window.confirm()` before granting `staff_admin`.

### pytest patterns for `staff_admin` routes

- Grant/revoke bodies: `str(uuid.uuid4())`, not `user-{uuid.hex[:8]}`.
- For routes gated by `require_role("staff_admin")`, patch **`load_user_roles`** to return `frozenset({"staff_admin"})` when the DB has no seeded row — patching only `authenticate_bearer_async` yields **403** `role_required`.
- Cover **403** on grant/revoke without admin role, **404** revoke not found, and invalid OID **422**.

## Why This Matters

These gaps are easy to miss in review but show up as broken builds, flaky admin UI, wrong HTTP semantics (404 vs 422), or tests that pass only before API validation tightens. Aligning sign-out URIs with Entra registration avoids “works in dev token box, fails on ACA” logout behavior.

Accepted by design (not changed in the review pass):

- Idempotent re-grant still emits multiple `admin.role.granted` outbox rows (plan U3).
- No last-admin safeguard when revoking another user’s `staff_admin`.
- Admin nav link visible to all authenticated users; authorization is API-only **403** on the page.

## When to Apply

- After implementing or reviewing Entra Part F–style admin role UI/API.
- When adding MSAL `logoutRedirect` call sites — grep for `postLogoutRedirectUri` and keep them consistent.
- When writing admin API tests with `Bearer dev-local` and monkeypatched auth.

## Examples

**Before (sign-out):** `postLogoutRedirectUri: window.location.origin`  
**After:** `postLogoutRedirectUri: origin ? \`${origin}/signin\` : undefined`

**Before (revoke test subject):** `user_subject_id: f"user-{uuid.uuid4().hex[:8]}"`  
**After:** `user_subject_id: str(uuid.uuid4())`

**Before (self-revoke missing-grant test):** only `authenticate_bearer_async` patched → **403**  
**After:** also `monkeypatch.setattr(deps_module, "load_user_roles", _fake_roles)` with `frozenset({"staff_admin"})` → **404** `user_role_not_found`

## What Didn't Work

| Attempt | Failure | Fix |
|--------|---------|-----|
| Test grant bodies like `user-{hex}` | **422** after OID pattern on grant request | `str(uuid.uuid4())` |
| Self-revoke test with only auth patch | **403** `role_required` | Patch `load_user_roles` too |
| Self-revoke guard before DB lookup | Missing own grant → **422** `self_revoke_forbidden` | Existence check first → **404** |
| Duplicate `revokingKey` `useState` | TypeScript build error | Single state + row key |

(session history)

## Related

- Plan: `docs/superpowers/plans/2026-05-28-entra-part-f-signout-admin-roles.md`
- `docs/solutions/azure-entra-auth/v2-token-aud-is-appid-not-uri-2026-05-26.md` — API token `aud` after sign-in
- `docs/solutions/architecture-patterns/two-app-reg-pattern-spa-plus-api-entra-2026-05-28.md` — SPA + API app registration pattern
- GitHub: [#76](https://github.com/joshua-project/jp-adopt-core/issues/76), PR #83
- **Stale runbook:** `docs/runbooks/multi-idp-b2c.md` still says staff add is deferred Part F / Alembic-only — consider `/ce-compound-refresh multi-idp-b2c` to point at `/admin/users`
