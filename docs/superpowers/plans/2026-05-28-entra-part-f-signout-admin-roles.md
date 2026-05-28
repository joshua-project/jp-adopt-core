---
status: active
created: 2026-05-28
type: feat
issue: 76
parent_plan: docs/superpowers/plans/2026-05-26-entra-direct-staff-auth.md
---

# feat: Entra Part F — sign-out UI + admin role-assignment UI

Closes the two web-side follow-ups that the Entra direct sign-in plan
intentionally deferred so launch could ship (see parent plan §Part F,
lines 812–824). Phase 2 (#60) is live; both features are quality-of-life,
not launch blockers.

## Problem frame

Two distinct gaps in the post-launch staff UI:

1. **No sign-out affordance.** The MSAL `logoutRedirect()` API is wired into
   the provider but nothing surfaces the action. A user can sign out only
   by clearing browser storage. Trivially blocks "let someone else use this
   laptop" flows and any kind of session-end testing.
2. **Granting access requires a code change.** Onboarding a new staff member
   today means (a) `az ad user show` to get their Entra OID and (b) writing
   a new Alembic migration that inserts one row into `user_roles`
   (`apps/api/alembic/versions/20260526_0015_seed_amy_banta_user_role.py` is
   the most recent example). Joel is a single point of failure; the audit
   trail lives in git not the DB; the workflow doesn't scale past a handful
   of users.

## Scope

### In scope

- Sign-out affordance in the staff chrome (header), invoking
  `instance.logoutRedirect({ postLogoutRedirectUri: window.location.origin })`.
- Staff-admin-only `/admin/users` page: list current `user_roles` entries
  (joined with `roles`), grant a new role by Entra OID + role name, revoke
  an existing entry.
- API endpoints under `apps/api/src/jp_adopt_api/routers/admin.py`
  (extending the existing router):
  - `GET    /v1/admin/user-roles` — list current grants.
  - `POST   /v1/admin/user-roles` — grant role to a subject.
  - `DELETE /v1/admin/user-roles/{user_subject_id}/{role_id}` — revoke.
- Audit via the existing transactional outbox: emit
  `admin.role.granted` / `admin.role.revoked` events in the same
  transaction as the row write.
- Nav link to `/admin/users` in `SiteHeader` (visible to everyone; page
  itself 403s for non-admins — see §Key technical decisions).

### Deferred to Follow-Up Work

- **Email → OID resolution** at grant time. Would require a Microsoft Graph
  service-principal permission (`User.Read.All`) on the API app reg plus a
  Graph client in the API. Tracked as a follow-up; v1 takes OID directly.
- **`partner_tenants` management** (adding new partner organizations beyond
  Joshua Project). #76 explicitly scopes Part F to `user_roles`; partner-
  tenant management is Phase 3.
- **Per-user role visibility on the client** (e.g., `GET /v1/me` returning
  the caller's roles so the nav can hide the Admin link from non-admins).
  Cheap follow-up; v1 just renders the link and lets the page 403.
- **Admin-action surface** in the contact-record's activity feed (would
  require an `admin_audit` table or join from outbox payload). Outbox emit
  is the source of truth; UI surface is later.

### Outside this product's identity

- **Self-service role request flows.** This is a staff-admin tool; users
  don't request access through the app.
- **OAuth consent-screen-style approval workflows.** Single staff_admin
  approves; no multi-stage approval queue.

## Dependencies

- **Cross-repo:** the SPA app reg's `single_page_application` block in
  `jp-infrastructure/stacks/azure/entra/jp-adopt-core-sso/` must list a
  `post_logout_redirect_uris` value matching `window.location.origin` (both
  the ACA FQDN and, post-#82, `https://adoption.joshuaproject.net`). If
  the field isn't present today, add it in jp-infrastructure as a sibling
  PR. Sign-out without it falls back to a generic Microsoft "you have
  signed out" page rather than returning to `/signin` — degraded but not
  broken. **Verify at implementation start** by reading the current
  Terraform module; only block on a missing entry if Entra rejects the
  logout redirect (`AADSTS500113` or similar).
- **No DB migration needed.** `roles` and `user_roles` already exist
  (migrations `0014`, `0015`); the API only reads/writes existing rows.

## Key technical decisions

- **Audit via outbox, not a new table.** AGENTS.md mandates the
  transactional outbox for state changes. Emitting
  `admin.role.granted` / `admin.role.revoked` to `outbox` with payload
  `{actor_subject_id, target_subject_id, role_id, role_name}` keeps the
  pattern uniform and lets the existing worker drain to downstream
  consumers. No new `admin_audit` table; a future UI surface can read
  outbox rows or join via payload.
- **OID-only grant input.** Email-to-OID needs Microsoft Graph; that's a
  new app-reg permission, a new client, and a new failure mode. The issue
  itself calls "enter an email or OID" — OID-only is the minimum that
  works, email is the polish.
- **Nav link is always visible; page enforces the gate.** Adding role-aware
  client rendering means new endpoints (`GET /v1/me/roles`) and threading
  the result through `useApiContext`. Not worth it for v1 — non-admins
  who click the link see a clean "Forbidden" state and learn not to.
- **Sign-out clears MSAL cache via `logoutRedirect`.** MSAL's logout helper
  owns its own cache; we don't need to manually purge `sessionStorage`.
  Any cached React Query state lives in component memory and dies on the
  redirect.
- **Extend `admin.py`, don't fork a new router.** The existing router
  already gates on `require_role("staff_admin")` and uses the same
  Pydantic + SQLAlchemy + `DbSession` shape — pattern-match it.

## Implementation units

### U1. Sign-out button in `SiteHeader`

**Files:**
- Modify: `apps/web/src/components/SiteHeader.tsx`
- (No new component file — one consumer, inline the button per the
  Entra parent plan's "one consumer, one file" convention.)
- Tests: manual verification (no `apps/web` test harness exists yet — #31).

**Goal:** A "Sign out" button on the right side of the header chrome that
invokes MSAL `logoutRedirect` and returns the user to `/signin` via the
SPA app reg's `post_logout_redirect_uris`.

**Requirements:** Closes the first acceptance criterion of #76
("Sign out from any page → land on Entra logout → return to `/signin`").

**Dependencies:** none in this plan; jp-infrastructure
`post_logout_redirect_uris` check (see §Dependencies).

**Approach:**
- Make `SiteHeader` a client component that consumes `useMsal()` (already
  the existing `"use client"` pattern in this file).
- Render the active account's display name (from `accounts[0].name` or
  `accounts[0].username` as fallback) and a "Sign out" button on the
  right side of the nav row. Button is `<button>` with a `jp-nav-link`-
  matched style so it sits visually with the rest of the chrome.
- On click: `instance.logoutRedirect({ postLogoutRedirectUri: window.location.origin })`.
  No try/catch — let MSAL handle the redirect; failure means the user
  stays on the page, which is fine.
- Hide both the account name and button on `/signin` and
  `/auth/callback` (the existing `SiteHeader` already renders on every
  page; just guard via `pathname` check).

**Patterns to follow:**
- `apps/web/src/components/RequireAuth.tsx` — `useMsal()` consumption,
  `pathname` checks for auth-exempt routes.
- `apps/web/app/signin/page.tsx` — `instance.loginRedirect` call shape
  (logout mirrors it).

**Technical design:** the button is a single `<button>` inside the
existing `<header>`'s flex row, after `<nav>`. Pseudo-shape (directional,
not copy-paste):

```tsx
const { instance, accounts } = useMsal();
const account = accounts[0];
const onSignOut = () => instance.logoutRedirect({
  postLogoutRedirectUri: window.location.origin,
});
// …rendered after the nav element, alongside account display name
```

**Test scenarios:**
- Test expectation: none — covered by manual smoke per #76 acceptance.
  Add to the #31 web-test-harness backlog as a deferred unit test
  ("renders sign-out button when MSAL has an active account; hides on
  `/signin` and `/auth/callback`").

**Verification:**
- Header shows account display name + Sign-out button on `/contacts`,
  `/adopters`, etc.
- Clicking Sign out redirects to Entra logout, then back to
  `window.location.origin` (which `RequireAuth` will bounce to `/signin`).
- `/signin` and `/auth/callback` do not render the button (no flicker).

---

### U2. List `user_roles` API endpoint

**Files:**
- Modify: `apps/api/src/jp_adopt_api/routers/admin.py`
- Modify: `apps/api/tests/test_admin_api.py`

**Goal:** `GET /v1/admin/user-roles` returns all current grants, joining
`user_roles` with `roles` so the UI can show role name + granted_at.

**Requirements:** Closes "see at least 2 existing users (Joel + Amy)" from
#76 acceptance.

**Dependencies:** none.

**Approach:**
- Add Pydantic schemas in `admin.py`:
  - `UserRoleRead { user_subject_id: str, role_id: UUID, role_name: str, granted_at: datetime }`
  - `UserRoleListResponse { items: list[UserRoleRead], total: int }`
- Endpoint: gate on `_staff_admin_dep` (already defined at the top of
  `admin.py`); query joins `UserRole` ⨝ `Role` ordered by `granted_at`
  desc.
- Regenerate contracts (`pnpm contracts:generate`); CI fails otherwise.

**Patterns to follow:**
- `list_facilitating_orgs` in `admin.py` — schema shape, dependency
  injection, response model wrapping.

**Test scenarios:**
- **Happy path:** seeded `user_roles` rows (staff_admin + facilitator)
  → 200 with both items, role_name populated, ordered by granted_at desc.
- **Empty:** no rows → 200 with `items: [], total: 0`.
- **Auth gate:** caller with no role (non-admin dev token or none) → 403
  with `code: "role_required"`.

**Verification:**
- Pytest scenarios above pass.
- `packages/contracts/src/generated/api.ts` carries the new endpoint and
  `UserRoleRead` schema; CI "contracts artifact must be committed" check
  passes.

---

### U3. Grant + revoke endpoints with outbox audit

**Files:**
- Modify: `apps/api/src/jp_adopt_api/routers/admin.py`
- Modify: `apps/api/tests/test_admin_api.py`

**Goal:** `POST /v1/admin/user-roles` grants; `DELETE /v1/admin/user-roles/{user_subject_id}/{role_id}` revokes. Both write to `outbox` in the
same transaction as the row mutation.

**Requirements:** Closes "Grant role to a third user (test by adding then
removing), verify the new user can sign in and see the dashboard" and
"Revoke access, verify the user gets 403 on protected endpoints" from #76
acceptance.

**Dependencies:** U2 (shares schemas + router pattern).

**Approach:**
- Pydantic request schemas:
  - `UserRoleGrantRequest { user_subject_id: str (min_length=1), role_id: UUID }`
    — `extra="forbid"` per existing pattern.
- `POST` handler:
  - Look up `Role` by id; 404 if missing.
  - `INSERT … ON CONFLICT DO NOTHING` on `user_roles` (PK is
    `(user_subject_id, role_id)` — see `models.py` line 136). Idempotent;
    re-granting is not an error.
  - `emit_outbox(session, event_type="admin.role.granted", payload={
      "actor_subject_id": <current user OID>,
      "target_subject_id": <granted OID>,
      "role_id": str(role.id),
      "role_name": role.name,
    })` BEFORE `await session.commit()` — same transaction, same shape as
    every other state-changing handler in this app.
  - Return `UserRoleRead` for the newly granted row (re-query with the
    join so `role_name` is populated; mirrors the read shape from U2).
- `DELETE` handler:
  - Resolve target row; 404 if absent (clean signal for the UI).
  - `delete()` on the row.
  - `emit_outbox(session, event_type="admin.role.revoked", payload={
      actor, target, role_id, role_name})`.
  - Return 204.
- Self-revoke guard: refuse to revoke the caller's own `staff_admin` row
  (returns 422 `code: "self_revoke_forbidden"`). Without this, a single
  admin can lock themselves out and the only recovery is the Alembic
  seed path we just replaced.

**Patterns to follow:**
- `apps/api/src/jp_adopt_api/routers/contacts.py` — `emit_outbox` call
  sites alongside `session.add` and `session.commit`.
- `apps/api/src/jp_adopt_api/routers/admin.py` `FacilitatorMembership`
  POST/DELETE — request validation, 404 shape, dependency wiring.
- AGENTS.md "Transactional outbox pattern" — outbox write goes inside
  the same transaction; never call a webhook client directly.

**Test scenarios:**
- **Happy grant:** POST with a fresh OID + valid role_id → 200 with role
  joined; row visible in subsequent `GET`.
- **Idempotent grant:** POST same payload twice → both succeed, only one
  row in `user_roles`, two `admin.role.granted` outbox rows (write-write
  is auditable; downstream consumers dedupe if they care).
- **Unknown role:** POST with random UUID → 404 `code: "role_not_found"`.
- **Validation:** POST with empty `user_subject_id` → 422.
- **Auth gate:** non-admin caller → 403 `code: "role_required"`.
- **Happy revoke:** DELETE existing grant → 204; row gone; outbox carries
  `admin.role.revoked`.
- **Revoke unknown:** DELETE non-existent (user, role) → 404.
- **Self-revoke guard:** admin attempts to DELETE their own staff_admin
  row → 422 `code: "self_revoke_forbidden"`; row unchanged.
- **Outbox transactionality:** force the post-emit commit to fail (e.g.
  by closing the session) and assert no outbox row persists either.
  Pattern-match the existing transactional outbox tests in
  `apps/api/tests/test_contacts_*.py` if a similar harness exists; if
  not, this scenario is the next-best assertion of the AGENTS.md
  guarantee.

**Verification:**
- Pytest scenarios above pass.
- Manual: grant a test OID, watch them sign in successfully; revoke;
  watch them 403.
- Outbox row for grant + revoke visible via `SELECT event_type, payload_json
  FROM outbox ORDER BY id DESC LIMIT 2;`.

---

### U4. Admin page UI

**Files:**
- Create: `apps/web/app/admin/users/page.tsx` — route entry, wraps the
  client component.
- Create: `apps/web/src/components/AdminUserRoles.tsx` — client component
  (list + grant form + revoke action).
- Tests: manual smoke per acceptance criteria; deferred unit tests on #31.

**Goal:** Single-page surface that lists grants and lets a staff_admin
grant or revoke. Replaces the Alembic-migration-per-user workflow.

**Requirements:** Closes #76 acceptance criteria 2–5 (sign in as admin,
see existing users, grant role, revoke, verify 403).

**Dependencies:** U2, U3.

**Approach:**
- Page renders the client component inside `<main>` (RootLayout already
  wraps in `<RequireAuth>` and `<SiteHeader>`).
- Client component:
  - On mount: `apiFetch(ctx, "/v1/admin/user-roles")` → list. On 403,
    render a "You don't have admin access" card (no error toast).
  - List: table with columns User Subject ID, Role, Granted at, Revoke
    button. Use existing styles from `Contacts.tsx` / `ContactRecord.tsx`
    for visual consistency (don't invent new ones).
  - Grant form: two inputs (Entra OID text, role dropdown populated from
    a small static list `["staff_admin", "staff", "facilitator"]` — the
    role names already in the seed migrations; resolved to `role_id` via
    a sibling `GET /v1/admin/roles` or inlined static map for v1).
  - Submit: `POST /v1/admin/user-roles`, reload list on success, surface
    the API error message on 4xx (422 self_revoke_forbidden /
    role_not_found / role_required).
  - Revoke: per-row button; `DELETE`, reload list. Confirm via
    `window.confirm` (matches the existing weight of confirmation in this
    app — no modal library yet).

**Note on roles dropdown:** if there's no existing `roles` list
endpoint, U4 either inlines the three known role names or adds a tiny
`GET /v1/admin/roles` to U2/U3's surface. Implementer decides during
execution — both work. Document the choice in the PR description.

**Patterns to follow:**
- `apps/web/src/components/ContactRecord.tsx` — pageful client component
  with API fetch, action buttons, inline error display, busy/disabled
  states.
- `apps/web/src/components/ManualContactForm.tsx` — form input + submit +
  error patterns.
- `apps/web/src/lib/api-client.ts` `apiFetch` — request shape.

**Test scenarios:**
- Test expectation: none — covered by manual smoke per #76 acceptance.
  Add to #31 backlog: "Admin page renders list; grant flow submits;
  revoke triggers confirm; 403 renders forbidden state."

**Verification:**
- As staff_admin: navigate to `/admin/users`, see Joel + Amy in the
  list; grant a test OID + `staff` role; refresh, see the new row; sign
  in as that OID, reach `/contacts`; revoke; sign in again, 403.
- As non-admin: navigate to `/admin/users`, see the "no access" state.

---

### U5. Nav link to `/admin/users`

**Files:**
- Modify: `apps/web/src/components/SiteHeader.tsx`

**Goal:** "Admin" link in the primary nav so admins can reach the page
without typing the URL.

**Requirements:** UX completion of U4; no #76 acceptance criterion
specifically requires it, but the issue's "Joel shouldn't be the only
person who can grant staff_admin" intent implies discoverability.

**Dependencies:** U1 (also touches `SiteHeader`), U4 (route must exist).

**Approach:**
- Add `{ href: "/admin/users", label: "Admin" }` to `NAV_ITEMS`.
- No role-aware hiding — see §Key technical decisions. Non-admins who
  click see the page's 403 state.

**Patterns to follow:** the existing `NAV_ITEMS` array — single source
of truth for the nav.

**Test scenarios:**
- Test expectation: none — trivial nav addition, covered by manual smoke.

**Verification:**
- "Admin" link appears in the header next to "My contacts".
- Clicking lands on `/admin/users`.

---

## System-wide impact

- **API surface:** three new `/v1/admin/user-roles*` endpoints. Contracts
  regenerated; CI gate enforces commit.
- **Outbox event types:** `admin.role.granted` / `admin.role.revoked`
  added. No new consumer required immediately; the existing worker drains
  them as unhandled (logged but not actioned). Document the event types
  in `docs/runbooks/operator-handbook.md` if that runbook enumerates
  them.
- **Auth chrome:** `SiteHeader` becomes a client component (or stays
  one — it already uses `"use client"`). No layout-level changes; the
  `<MsalClientProvider>` already wraps everything.
- **No DB migration.** Reads/writes target existing tables.
- **No new env vars or secrets.**

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `post_logout_redirect_uris` missing on the SPA app reg → Entra rejects logout. | Medium (Terraform may not list it; never tested end-to-end). | Verify at implementation start by reading the current jp-infrastructure module. If absent, sign-out falls back to a generic Microsoft "you are signed out" page rather than returning to `/signin` — degraded but not broken. Add the URI in a sibling jp-infrastructure PR. |
| Admin accidentally revokes their own `staff_admin` row, locks the org out. | Medium (single-admin teams; one slip is total lockout). | U3 self-revoke guard. Recovery if the guard is bypassed: the original Alembic-seed path (migration `0015`) still works. |
| Grant idempotency masks a typo (admin pastes the wrong OID, doesn't notice). | Medium. | The UI reloads the list after every grant — the new row appears under the typed OID, and a mistyped 1-character-different OID will show two rows. Surface the granted OID prominently in the success state. |
| Outbox event not consumed → silent audit gap. | Low (the worker drains everything; unhandled types just log). | Document the event types in the operator handbook; downstream consumers can subscribe later without code changes. |
| Email→OID expectation creep — staff ask "why can't I just type an email?" | High (Joel's first reaction was to type both in the issue body). | Documented in §Scope as a follow-up; UI input placeholder reads "Entra user OID (UUID)" so the expectation is set. |

## Verification (whole-plan)

- All three new API endpoints pass pytest with the scenarios enumerated
  in U2 and U3.
- Manual end-to-end on the live ACA URL:
  1. Sign in as Joel → see Sign-out button → click → land on Entra logout
     → return to `/signin`.
  2. Sign back in → navigate to `/admin/users` → see existing rows.
  3. Grant a test OID `staff` role → that user signs in successfully →
     reaches `/contacts`.
  4. Revoke → that user reloads → hits 403 on every protected endpoint.
  5. Confirm `outbox` table has the two `admin.role.granted` and one
     `admin.role.revoked` rows from the test.
- CI green (API + web build + contracts artifact check).

## Open questions deferred to implementation

- **Roles dropdown source** — static list vs new `GET /v1/admin/roles`
  endpoint. Either works; pick during U4. Document the choice in the PR.
- **Account display name shape** — `accounts[0].name` (display name) vs
  `accounts[0].username` (UPN/email). MSAL populates both; pick the one
  that renders cleanly with no overflow. Trivial during U1.
- **Header layout for the sign-out + nav combination** — depends on how
  the new "Admin" link + account display + button affect the existing
  flex row at narrow widths. Adjust during U5; not worth pre-deciding.
