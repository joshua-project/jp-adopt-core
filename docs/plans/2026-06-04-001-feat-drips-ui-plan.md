---
title: "feat: Drips UI — campaigns, steps, per-contact enrollment, suppression (#55)"
type: feat
status: active
created: 2026-06-04
depth: standard
origin: docs/brainstorms/2026-06-04-drips-ui-requirements.md
related_issues: ["#55"]
---

# feat: Drips UI — campaigns, steps, per-contact enrollment, suppression (#55)

Closes the curl-only gap on the drip engine: a four-surface staff UI in
`apps/web` plus five new API endpoints. Origin
`docs/brainstorms/2026-06-04-drips-ui-requirements.md` is the source of truth
for product behavior, actors, flows, and acceptance examples — this plan
defines **how** it's built, not **what**.

Backend-first sequencing: the five API additions + a single contracts
regeneration land before any UI consumes them. UI units carry
`Test expectation: none — #31` per the existing web-test-harness gap.

---

## Problem Frame & Scope

The drip engine has been in production since U10 (campaigns, steps,
enrollment auto-drain on outbox events, ACS send). Staff cannot reach any of
it from the staff app — every campaign list, step add, manual enrollment,
or suppression edit is a `curl` against `docs/runbooks/drip-engine.md`. Amy
will reach for drips the first time she wants to enroll a freshly-matched
facilitator in the "Facilitator welcome" campaign without paging engineering,
and the lack of a UI is on the Phase 1 "strongly recommended" list because of
that.

**In scope** (per origin):
- Four surfaces in `apps/web`: `/campaigns` (list + create), `/campaigns/[id]`
  (detail + step add/delete + activate/pause/archive), per-contact drips
  panel (full on `/contacts/[id]`, read-only summary on `/workflow/[id]`),
  and `/admin/suppression`.
- Five API additions: one templates list, one per-contact enrollments
  read, plus suppression GET/POST/DELETE.

**Out of scope** — see [Scope Boundaries](#scope-boundaries).

---

## Key Technical Decisions

- **KTD-1 — Backend-first sequencing with a single contracts regen.** All
  five endpoints land first, then `pnpm contracts:generate` runs once, then
  the four UI surfaces consume typed contracts. Reviewers see one
  surface-shape change in `packages/contracts` instead of four; each UI unit
  builds against a stable surface.

- **KTD-2 — Template dropdown sources from a filesystem scan, not a
  registry.** `GET /v1/drips/templates` enumerates `*.mjml` files in
  `EMAIL_TEMPLATES_DIR` (defined in `apps/api/src/jp_adopt_api/domain/drips.py`)
  on each request and returns a sorted list. No caching, no registry table —
  templates are a developer-time concern (drop a file per the existing
  `docs/runbooks/drip-engine.md` convention), and a per-request scan is
  trivially cheap at this scale (current count: 2).

- **KTD-3 — Suppression endpoints live in a new router, not under `/v1/drips/`.**
  The brainstorm names `/v1/suppression-list` and the model
  (`SuppressionList` in `apps/api/src/jp_adopt_api/models.py`) is independent
  of any specific campaign. A standalone router (`apps/api/src/jp_adopt_api/routers/suppression.py`)
  keeps the URL surface honest. Server-side normalization + SHA-256 hashing
  reuses the existing `email_hash` helper from `apps/api/src/jp_adopt_api/domain/drips.py`
  so no PII is persisted.

- **KTD-4 — Idempotent POST on suppression, not 409.** Re-adding the same
  address returns the existing row with 200 (per the origin's R14 and AE5),
  rather than a duplicate-key error. Matches how staff reach for the
  endpoint: they're not tracking whether they've already added the address.

- **KTD-5 — `/admin/suppression` gated to `{staff_admin, adoption_manager}`
  (the standard staff set).** Suppression is operational, not strictly
  admin. Matches the gating on `contacts` / `drips` / `manual_contacts`
  routers; differs from `/v1/admin/*` (role mgmt + facilitator orgs), which
  stays `staff_admin`-only.

- **KTD-6 — UI test convention follows PR #94.** Every web component unit
  here carries `Test expectation: none — #31` until the Vitest harness lands.
  Backend additions are tested in `apps/api/tests/test_drips.py` (existing)
  and a small new `apps/api/tests/test_suppression.py` for the dedicated
  suppression router.

- **KTD-7 — Repo conventions.** `vocab.ts` is the source of truth for
  human-readable labels — add a new `StatusKind = "campaign"` table with the
  documented `STATUS_TONE` colors (active=green, draft=slate, paused=amber,
  archived=rose). Reuse `DataTable`, `StatusBadge`, `StatusFilter`,
  `EmptyState`, `LoadingRows`. Add `Campaigns` to the top nav next to
  `Adopters` / `Facilitators`. After any API surface change, run
  `pnpm contracts:generate` and commit the regenerated artifact.

---

## System-Wide Impact

| Surface | Touched by | Notes |
|---|---|---|
| `apps/api/src/jp_adopt_api/routers/drips.py` | U1 | New `GET /v1/drips/templates` endpoint + response model |
| `apps/api/src/jp_adopt_api/routers/contacts.py` | U2 | New `GET /v1/contacts/{id}/enrollments` endpoint + response model |
| `apps/api/src/jp_adopt_api/routers/suppression.py` | U3 | **New file** — GET/POST/DELETE suppression endpoints |
| `apps/api/src/jp_adopt_api/main.py` | U3 | Register the new suppression router |
| `apps/api/openapi.json`, `packages/contracts/src/generated/api.ts` | U4 | Regenerated artifact, single commit |
| `apps/web/src/lib/vocab.ts` | U5 | New `StatusKind = "campaign"` label table |
| `apps/web/src/components/SiteHeader.tsx` | U5 | Add `Campaigns` nav link |
| `apps/web/app/campaigns/` | U5, U6 | New routes: list + detail |
| `apps/web/src/components/ContactRecord.tsx` | U7 | Add Drips tile + Manual enroll action |
| `apps/web/src/components/WorkflowTransition.tsx` | U7 | Add read-only "active in N drips" summary |
| `apps/web/app/admin/suppression/` | U8 | New route + page |
| `apps/web/src/lib/api-client.ts` | U5–U8 | New helpers for each new endpoint |

---

## Implementation Units

Dependency order: U1, U2, U3 run independently; U4 closes the backend half;
U5–U8 consume the regenerated contracts.

```mermaid
graph LR
  U1[U1 GET /drips/templates] --> U4[U4 contracts regen]
  U2[U2 GET /contacts/{id}/enrollments] --> U4
  U3[U3 suppression GET/POST/DELETE] --> U4
  U4 --> U5[U5 /campaigns list + create + vocab + nav]
  U4 --> U6[U6 /campaigns/{id} detail + steps]
  U4 --> U7[U7 contact drips panel + workflow summary]
  U4 --> U8[U8 /admin/suppression]
  U5 --> U6
```

---

### U1. `GET /v1/drips/templates` — list available MJML templates

**Goal:** Surface the set of `.mjml` template filenames so the add-step UI
can present a dropdown instead of a free-text field.

**Requirements:** Covers R8 (origin); enables AE4.

**Dependencies:** none.

**Files:**
- `apps/api/src/jp_adopt_api/routers/drips.py`
- `apps/api/tests/test_drips.py`

**Approach:** Add a new endpoint to the existing drips router, gated by the
same staff role dep used by the rest of the campaign endpoints. The handler
reads `EMAIL_TEMPLATES_DIR` (the existing constant in
`apps/api/src/jp_adopt_api/domain/drips.py`), enumerates `*.mjml` files
synchronously, returns a sorted list of `{ name }` items. No caching, no DB.
If the directory is missing or unreadable, return an empty list with 200 (a
fresh dev environment has no templates, not an error condition).

**Patterns to follow:** Existing drips router endpoint shape; response model
mirrors `CampaignListResponse` minus the per-row complexity.

**Test scenarios:**
- Endpoint returns the current `*.mjml` filenames, sorted lexicographically.
- Non-`.mjml` files in the directory (e.g., a stray `.md`) are excluded.
- Missing directory → returns `{ items: [] }`, status 200, no exception.
- Unauthorized role → 403.

**Verification:** Endpoint visible in `openapi.json` after regen; returns
the existing 2 demo templates on a fresh dev DB.

---

### U2. `GET /v1/contacts/{id}/enrollments` — per-contact enrollments + events

**Goal:** Single endpoint the per-contact drips panel queries so the UI
doesn't fan out per-campaign.

**Requirements:** Covers R5, R12.

**Dependencies:** none.

**Files:**
- `apps/api/src/jp_adopt_api/routers/contacts.py`
- `apps/api/tests/test_contacts_record.py`

**Approach:** Return all `Enrollment` rows for the contact (any state) with
each enrollment's campaign name, current step position, `last_step_sent_at`,
and a bounded set of `EnrollmentEvent` rows (most recent first, capped per
enrollment to keep payload tight — exact cap deferred to implementation
based on real data, target ~20). Gated by `_STAFF_DEP` (the existing
`{staff_admin, adoption_manager}` set in `contacts.py`). 404 if the contact
is missing.

**Patterns to follow:** `add_contact_note` / `get_contact_*` patterns in
`apps/api/src/jp_adopt_api/routers/contacts.py`; response model mirrors
`ContactActivityResponse` shape (items + total).

**Test scenarios:**
- Contact with zero enrollments → `{ items: [], total: 0 }`.
- Contact with one active enrollment → one item with campaign name +
  current_step_position + last_step_sent_at (`null` until the worker has
  sent a step).
- Multiple enrollments — order is deterministic (e.g. created_at desc); each
  carries its events list.
- Unknown contact id → 404.
- Unauthorized role → 403.

**Verification:** Endpoint appears in `openapi.json` and returns the
expected shape on the local stack against a seeded contact with one
enrollment.

---

### U3. Suppression API — GET/POST/DELETE `/v1/suppression-list`

**Goal:** Read, write, and remove suppression entries with server-side
email normalization + hashing.

**Requirements:** Covers R13, R14, R15; enables AE5, AE7.

**Dependencies:** none.

**Files:**
- `apps/api/src/jp_adopt_api/routers/suppression.py` (**new**)
- `apps/api/src/jp_adopt_api/main.py` (register the router)
- `apps/api/tests/test_suppression.py` (**new**)

**Approach:** New top-level router with prefix `/v1/suppression-list`.
- `GET /` — paginated list (`limit`, `offset` query params, defaults 50/0,
  hard cap 200). Returns `{ items: [{ email_hash, reason, suppressed_at }],
  total }`. Gated by `{staff_admin, adoption_manager}`.
- `POST /` — body `{ email, reason="manual", source_metadata? }`.
  Normalize via the existing helper, hash via `email_hash` in
  `apps/api/src/jp_adopt_api/domain/drips.py`, `INSERT ... ON CONFLICT DO
  NOTHING`, then `SELECT` and return the row. **Idempotent**: re-adding the
  same email returns the existing row at 200, not 409 (per KTD-4 / AE5).
- `DELETE /{email_hash}` — remove by hash; 404 if not present; 204 on
  success.

**Patterns to follow:** `admin.py` router shape for response/list models;
`add_contact_note`'s commit-then-refresh pattern; `email_hash` helper in
`apps/api/src/jp_adopt_api/domain/drips.py` (reuse, don't reinvent).

**Test scenarios:**
- POST with a fresh email → 200, returns the row with `email_hash`,
  `reason='manual'`, server-stamped `suppressed_at`.
- POST same email twice → 200 both times; only one row in the table.
- POST with an explicit `reason='hard_bounce'` and `source_metadata={...}`
  → row persists both.
- GET paginated → returns at most `limit` items, correct `total`.
- DELETE existing hash → 204; subsequent GET no longer returns it.
- DELETE unknown hash → 404.
- Unauthorized role (monkeypatched `load_user_roles` returning
  `{"facilitator"}`) → 403 on all three verbs.

**Verification:** Endpoints in `openapi.json`; the local stack can POST a
new suppression, see it on GET, and DELETE it.

---

### U4. Regenerate API contracts

**Goal:** Bring `apps/api/openapi.json` and
`packages/contracts/src/generated/api.ts` up to date with U1, U2, U3 in a
single commit so subsequent UI units consume typed contracts.

**Requirements:** Project convention (AGENTS.md: "OpenAPI is the source of
truth for the web client").

**Dependencies:** U1, U2, U3.

**Files:**
- `apps/api/openapi.json` (regenerated)
- `packages/contracts/src/generated/api.ts` (regenerated)

**Approach:** Run `pnpm contracts:generate` from the repo root with the new
endpoints in place. Verify that the regenerated `api.ts` contains entries
for `/v1/drips/templates`, `/v1/contacts/{contact_id}/enrollments`, and the
three `/v1/suppression-list` paths. Commit the regenerated files as a
single `chore(contracts)` commit so the UI commits don't carry contract
noise.

**Patterns to follow:** Exact pattern from this branch's PR #94 history
(`chore(contracts): regenerate for assignable-orgs, override field, emails`).

**Test scenarios:** *Test expectation: none — generated artifact, behavior
covered by U1/U2/U3 API tests.*

**Verification:** `git diff` shows only the openapi.json + api.ts deltas;
each new endpoint and request/response type is present in `api.ts`.

---

### U5. `/campaigns` list page + create + vocab + nav

**Goal:** A working campaigns list with status badges, inline Activate /
Pause, and a "+ New campaign" form that lands on the new draft's detail
page. Adds the supporting vocab + nav scaffolding.

**Requirements:** Covers R1, R2, R3, R17, R18, R19 (origin); enables AE1.

**Dependencies:** U4.

**Files:**
- `apps/web/src/lib/vocab.ts` (add `StatusKind = "campaign"` label table)
- `apps/web/src/components/SiteHeader.tsx` (add `Campaigns` nav link)
- `apps/web/src/lib/api-client.ts` (helpers: `listCampaigns`,
  `createCampaign`, `activateCampaign`, `pauseCampaign`)
- `apps/web/app/campaigns/page.tsx` (list page route)
- `apps/web/src/components/CampaignList.tsx` (list component using
  `DataTable` / `StatusBadge` / `EmptyState` / `LoadingRows`)
- `apps/web/src/components/NewCampaignModal.tsx` (the "+ New campaign"
  form modal, mirrors the email composer modal in `ContactRecord.tsx`)

**Approach:** Build the page route + a `CampaignList` client component
that loads via the new `listCampaigns` client helper. Render `DataTable`
columns: name, trigger_event_type, status (via `StatusBadge` with the new
`"campaign"` kind), `last_activity_at`. Inline Activate / Pause buttons
fire `activateCampaign` / `pauseCampaign`; row state updates optimistically
or via re-fetch. "+ New campaign" opens a modal mirroring
`ContactRecord.tsx`'s email composer (subject/body), substituting fields:
`name`, `description` (optional), `trigger_type`, `trigger_event_type`,
`precedence`. On success, route to `/campaigns/[id]`. Status colors come
from the documented `STATUS_TONE` map; the new `"campaign"` `StatusKind`
table maps `draft`/`active`/`paused`/`archived` to humanized labels.

**Patterns to follow:** `apps/web/app/admin/users/page.tsx` for the admin
page shape; `MatchQueue.tsx` for `DataTable` + `StatusBadge` use;
`ContactRecord.tsx`'s email composer modal for the "+ New campaign"
modal pattern.

**Test scenarios:** *Test expectation: none — #31 (no web test harness).*
Manual verification scenarios for the implementer:
- AE1 happy path: open `/campaigns` → click "+ New campaign" → fill the
  form → submit → land on `/campaigns/[id]` with status `draft`.
- Empty state: a fresh DB with no campaigns shows the documented empty
  state, not a crash.
- Activate row → badge flips to `active` without a hard reload.
- Pause an active row → badge flips to `paused`; subsequent re-fetch
  agrees.

**Verification:** `/campaigns` renders on the local stack and reflects the
seeded "Facilitator welcome" campaign; `+ New campaign` produces a new
draft visible in the list and on the detail route.

---

### U6. `/campaigns/[id]` detail — meta + steps with template dropdown

**Goal:** Single canonical place to inspect and edit a campaign — meta,
ordered steps with the template-from-dropdown add-step form, delete-step
controls, and Activate / Pause / Archive controls.

**Requirements:** Covers R4, R8, R9, R10 (origin); enables AE1 (Activate
half), AE4.

**Dependencies:** U4, U5.

**Files:**
- `apps/web/app/campaigns/[campaignId]/page.tsx` (detail page route)
- `apps/web/src/components/CampaignDetail.tsx` (detail component)
- `apps/web/src/components/AddCampaignStepForm.tsx` (the add-step form
  with the template `<select>`)
- `apps/web/src/lib/api-client.ts` (helpers: `getCampaign`,
  `patchCampaign`, `addCampaignStep`, `deleteCampaignStep`,
  `archiveCampaign`, `listDripTemplates`)

**Approach:** Two visual regions: a meta panel (name, description, trigger
event type, status, version) editable inline via PATCH on save; and a
steps panel listing steps in `position` order with subject, `delay_days`,
`mjml_template_name`, `send_at_hour:send_at_minute`. Add-step form fields
mirror the API (`position` auto-suggested as `max(positions)+1` but
editable; `subject`, `delay_days`, `mjml_template_name` from `<select>`
sourced by `listDripTemplates`, `send_at_hour`, `send_at_minute`). Delete
button per step asks for confirm. The Activate / Pause / Archive controls
post to the existing campaign-state endpoints; Archive uses the existing
`DELETE /v1/drips/campaigns/{id}` (which the issue clarifies is an
archive, not a hard delete). All status displays go through the
`"campaign"` `StatusKind` added in U5.

**Patterns to follow:** `MatchReview.tsx` for the meta/decision two-region
layout; `ContactRecord.tsx`'s inline-edit + modal patterns; the same
`<select>` template-picker shape used in `MatchReview.tsx`'s assignable-org
dropdown.

**Test scenarios:** *Test expectation: none — #31.* Manual:
- AE4: open the add-step form → the dropdown lists the worker's known
  `.mjml` filenames; submit a step → it appears in the list at the chosen
  position. The form never exposes a free-text template field.
- AE1 Activate half: from a `draft` campaign, click Activate → badge
  flips to `active`; the detail page status agrees.
- Delete step → confirm prompt → step disappears from the list.
- Patch meta → save → refetch shows the updated fields.

**Verification:** Add a step to the seeded campaign via the UI, see it
listed; Activate the campaign; verify status round-trips.

---

### U7. Per-contact drips panel + workflow read-only summary

**Goal:** The per-contact tile on `/contacts/[id]` (active enrollments +
event history + manual-enroll) and the read-only summary on
`/workflow/[id]`.

**Requirements:** Covers R5, R6, R7, R12 (origin); enables AE2, AE3, AE6.

**Dependencies:** U4.

**Files:**
- `apps/web/src/components/ContactRecord.tsx` (add a Drips tile + manual
  enroll modal/dropdown)
- `apps/web/src/components/WorkflowTransition.tsx` (add the read-only
  drips summary block)
- `apps/web/src/lib/api-client.ts` (helpers: `getContactEnrollments`,
  `enrollInCampaign`)

**Approach:** On `ContactRecord.tsx`, add a new `Tile` (matching the
existing tile pattern) titled "Drips" that calls
`getContactEnrollments(contactId)` on mount. Render each active
enrollment as a row with campaign name, current step position, and
`last_step_sent_at`. A collapsed `<details>` per enrollment lists its
events (`step_sent` / `paused` / `exited`) with timestamps. A "Manual
enroll" button opens a small dropdown of active campaigns (sourced via the
existing campaigns list filtered to `status === "active"`) and posts to
`POST /v1/drips/campaigns/{id}/enroll`. On success, refresh the tile.
Surface duplicate-enrollment errors inline without clearing the user's
selection (covers AE3). Empty state: "Not enrolled in any drips."

On `WorkflowTransition.tsx`, add a one-line read-only block: "Active in N
drips" (or "Not enrolled in any drips"), sourced from the same
`getContactEnrollments` helper. No manage controls.

**Patterns to follow:** `ContactRecord.tsx`'s existing tile pattern + the
email composer modal for the manual-enroll affordance.

**Test scenarios:** *Test expectation: none — #31.* Manual:
- AE2: contact with zero active enrollments → empty state on the tile;
  manual-enroll dropdown lists only `active` campaigns; selecting one and
  confirming results in exactly one new active enrollment listed.
- AE3: contact already enrolled in campaign X → second manual enroll in X
  surfaces the duplicate-enrollment error inline; the dropdown selection
  is preserved.
- AE6: contact with zero enrollments → `/workflow/[id]` reads "Not
  enrolled in any drips"; no manage control.
- The workflow summary updates to "Active in 1 drip" after enrollment via
  the contact tile.

**Verification:** Open `/contacts/[id]` for the seeded `Grace Adeyemi`
contact (from the earlier local-stack seed), enroll her in the seeded
campaign, see the tile reflect the new enrollment, see the workflow
summary update.

---

### U8. `/admin/suppression` admin page

**Goal:** A small admin page listing suppression entries, with add-by-email
and remove-by-hash controls, gated to the standard staff set.

**Requirements:** Covers R13, R14, R15, R16 (origin); enables AE5, AE7.

**Dependencies:** U4.

**Files:**
- `apps/web/app/admin/suppression/page.tsx` (page route)
- `apps/web/src/components/SuppressionListAdmin.tsx` (component)
- `apps/web/src/lib/api-client.ts` (helpers: `listSuppression`,
  `addSuppression`, `removeSuppression`)

**Approach:** Page route renders the `SuppressionListAdmin` component. The
component fetches the paginated list and renders a `DataTable` with
columns: hashed email (display the first 12 hex chars + ellipsis to keep
it scannable), reason, suppressed_at, and a Remove button per row. Above
the table, an add-by-email form (single email input + optional
reason-default `"manual"`, submit calls `addSuppression`). Surface the
idempotent-200 response without confusing the user — if the row already
exists, just append/refresh it without an error toast. Empty state copy:
"No suppressed addresses." The page is auth-gated by both server (the new
router enforces `{staff_admin, adoption_manager}`) and client (mirroring
`/admin/users` — show a 403 fallback if the response is 403).

**Patterns to follow:** `apps/web/app/admin/users/page.tsx` for the admin
page layout and 403 handling; `DataTable` + `EmptyState` primitives.

**Test scenarios:** *Test expectation: none — #31.* Manual:
- AE5: add an email already on the list → row appears once, no error.
- AE7: monkeypatched non-staff role (or signing in as a pure facilitator)
  → page surfaces 403 fallback; the add/remove form is not reachable.
- Add a fresh email → row appears with `reason='manual'` and current
  timestamp.
- Remove a row → it disappears; refetch confirms.
- Pagination: with > limit rows, next page returns the next chunk and
  total is correct.

**Verification:** On the local stack, add `bounce@example.com`, see it on
the list, remove it; confirm the worker's suppression filter skips that
address (visible in worker logs on next drip-send tick).

---

## Scope Boundaries

### Deferred to Follow-Up Work
- **Importing existing MJML templates into `apps/api/email-templates/`.** You
  mentioned you have a bunch of templates already; staging and committing
  those is a follow-up content task, not part of this UI work. The dropdown
  surfaces whatever's on disk at the time.
- **MJML preview** (surface 5 in the issue) — `POST /v1/drips/campaigns/{id}/steps/{position}/preview`
  returning `{html, plain}` and a Preview button on the detail page.
  Explicit v2 stretch in the brainstorm.
- **Vitest + RTL web test harness** (#31) — once it lands, retrofit
  component-level coverage for the four UI surfaces.
- **Per-enrollment pause API + UI** — the `paused` state exists on
  `Enrollment` but no API mutates it; out of scope here.
- **Auto-exit enrollment when an email is added to suppression
  mid-campaign** — the worker already skips suppressed sends silently; no
  behavior change here.

### Non-Goals
- Rich MJML editing, drag-drop step reorder, A/B testing — out of scope per
  the origin.
- Bounce-handler integration (`hard_bounce` → auto-suppress) — tracked
  separately.
- One-click unsubscribe (RFC 8058 `List-Unsubscribe`) — future.
- Changes to the drip worker or ACS send path — out of scope; this work
  adds API + UI on top of the existing engine.

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Templates directory empty on a fresh dev DB → dropdown is empty → add-step UX dead-ends | U1 returns `{ items: [] }` cleanly; U6 shows a clear empty-state message ("No MJML templates available — drop a file in `apps/api/email-templates/`"). Engineering follow-up imports the existing templates (see Deferred). |
| Duplicate enrollment from the per-contact panel surfaces an opaque server error | U7 explicitly handles the API's duplicate-enrollment response (per AE3) and shows it inline. |
| Suppression hash display ("first 12 hex chars + ellipsis") loses information when an operator needs the full hash | Show the full hash on hover (`title` attr) so click-to-copy works without losing scannability. |
| Contract drift between API and UI | Single contracts-regen commit (U4) gates the UI units; CI's "contracts artifact must be committed" check catches any divergence. |
| `/admin/suppression` accessed by a non-staff role | Server gates (U3) AND client 403 fallback (U8). Mirrors the `/admin/users` pattern. |

---

## Verification Strategy

- **API:** `uv run pytest` in `apps/api` against a fresh local Postgres (the
  suite's seed data is not idempotent across repeated runs on one DB — same
  caveat as PR #94). New tests live in `test_drips.py` (extend) and the new
  `test_suppression.py`; existing drips/contacts tests must remain green.
- **Contracts:** `pnpm contracts:generate` once after U1+U2+U3 (U4); commit
  the regenerated `apps/api/openapi.json` + `packages/contracts/src/generated/api.ts`.
- **Web typecheck:** Use `tsc --noEmit -p apps/web/tsconfig.json` (not a
  production build) to avoid clobbering a running dev server's `.next` —
  same lesson as PR #94's webpack-cache recovery.
- **Manual (local stack):** create a campaign via "+ New campaign", add a
  step picking from the template dropdown, activate it, enroll a seeded
  contact via the per-contact panel, see the workflow summary update, add an
  email to suppression and remove it.
