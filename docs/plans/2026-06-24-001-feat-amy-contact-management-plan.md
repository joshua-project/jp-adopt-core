---
title: "feat: Amy contact-management PR 1 — search, FPG count, hard-delete"
type: feat
status: active
created: 2026-06-24
depth: standard
---

# feat: Amy contact-management — search, facilitator FPG count, hard-delete

## Problem Frame

Amy (adoption manager) made three asks while working in jp-adopt-core. This plan is **PR 1** — the cohesive contact-management cluster. The three heavier, independent asks (email template editor, spreadsheet bulk upload, matching tuning) are **deferred to their own plans** (see Scope Boundaries) — bundling them here would span unrelated subsystems.

1. **Contact search** — there is no search on the contacts/people list (the only `<input>` is the dev-token box). Committed to Amy for delivery.
2. **Facilitator FPG count** — the per-facilitator FPG list already exists on the org detail page; Amy wants to *scan* facilitators without opening each, so surface a coverage **count** in the list.
3. **Delete contacts** — Amy-scoped during the demo: **spam → hard-delete**; **hostile → `do_not_engage`**. Hard-delete must not be silently re-imported by the hourly DT ETL, so it records a **suppression** (interim — DT is being decommissioned very soon).

## Scope & Success Criteria

- Searching the people list by a substring of name or email returns matching contacts **across the whole dataset**, not just the loaded page.
- The facilitator list shows each org's FPG coverage count at a glance.
- A staff user can permanently delete a spam contact (removed everywhere, never re-imported while DT still syncs) or mark a hostile contact `do_not_engage` (kept, flagged).
- All existing conventions honored: state changes via `/transition` not PATCH; transactional outbox; contracts regenerated + committed; new Alembic revision (not an edit).

---

## Key Technical Decisions

- **Search is server-side** (`q` ILIKE in the SQL `WHERE`), not a client-side page filter — Amy needs to *find* people across ~250+ contacts/pages. It appends to the existing `conditions` list in `list_contacts`, so it flows into both the count and the page automatically.
- **Search input lands on `PipelineView`** (the `/adopters` + `/facilitators` pipeline lists) — it already uses `apiFetch` + `URLSearchParams` + an `AbortSignal`, the clean integration point. `Contacts.tsx` (raw fetch, button-gated `/contacts`) is left for a follow-up to keep PR 1 focused. (Decision; revisit if Amy means the `/contacts` page specifically.)
- **Hard-delete is a new `DELETE /v1/contacts/{id}`** following the `revoke_user_role` shape: do the work + `emit_outbox` + single `commit`. It explicitly deletes non-cascading children and records suppression in one transaction.
- **Suppression is a new `deleted_contacts` table**, NOT the existing `suppression_list` (that's email-send suppression for drips — conflating them is wrong). Keyed on `(source_system, source_id)` with `email_normalized` fallback; checked in the ETL contacts upsert exactly like the merged `_SERVICE_ASSIGNEE_WP_USER_IDS` service-account skip. Interim bridge until DT shutoff.
- **Hostile path reuses the existing `do_not_engage` transition** (`POST /v1/contacts/{id}/transition`, already wired in `api-client.transitionContact`) — no new endpoint.

---

## Requirements Traceability

| Req | Source | Units |
|---|---|---|
| Search contacts by name/email across all pages | Amy (committed) | U1, U2 |
| Facilitator FPG coverage visible in list | Amy | U3 |
| Hard-delete spam contacts, not re-imported | Amy (demo) | U4, U5, U6, U7 |
| Mark hostile contacts do_not_engage | Amy (demo) | U7 |

---

## Implementation Units

### U1. API: `q` search param on `GET /v1/contacts`

**Goal:** Filter the contacts list by a case-insensitive substring of `display_name` OR `email_normalized`, across the whole dataset.
**Requirements:** Search.
**Dependencies:** none.
**Files:**
- Modify: `apps/api/src/jp_adopt_api/routers/contacts.py` (`list_contacts`, the param block + the `conditions` list ~lines 101-168)
- Test: `apps/api/tests/test_contacts_filters.py`
**Approach:** Add an optional `q: str | None` `Query` arg (trim; treat empty/whitespace as absent). When present, append `or_(Contact.display_name.ilike(f"%{q}%"), Contact.email_normalized.ilike(f"%{q}%"))` to the existing `conditions` list so it applies to BOTH `count_stmt` and `list_stmt`. Escape `%`/`_` in `q` (use `.ilike(pattern, escape="\\")` with the wildcards escaped) so a literal `%` doesn't match everything. No new schema — it's a query param on the existing op; contracts regen in U2.
**Patterns to follow:** the `conditions`-list construction already in `list_contacts`; param style of the existing `party_kind`/`adopter_status` `Annotated[..., Query(...)]` args; auth dep `_STAFF_DEP`.
**Test scenarios:**
- `q="jane"` returns contacts whose display_name contains "jane" (case-insensitive) AND contacts whose email contains "jane"; non-matches excluded.
- `q` matches across pages: with >limit total contacts, a `q` matching a contact on "page 2" returns it at offset 0 (proves SQL-level filtering, not page filtering).
- `q` combines with `party_kind`/status filters (AND semantics) — only matching adopters returned when both set.
- `q` with a literal `%` or `_` is treated literally (escaped), not as a wildcard.
- Empty/whitespace `q` behaves identically to no `q` (full list).
- `total` in the response reflects the filtered count, not the table size.
**Verification:** API tests green; `GET /v1/contacts?q=...` narrows results and `total`.

### U2. Web: debounced search input on the people list

**Goal:** A search box at the top of the pipeline list, wired to `q`, debounced.
**Requirements:** Search.
**Dependencies:** U1 (+ contracts regen).
**Files:**
- Modify: `apps/web/src/components/PipelineView.tsx` (state + `URLSearchParams` at ~102-108 + `fetchData` deps ~135 + render the input)
- Modify: `packages/contracts/src/generated/api.ts` (regenerated)
- Test: `apps/web/src/components/__tests__/PipelineView.test.tsx`
**Approach:** Add a `q` state + a debounced value (mirror the `useEffect` + `window.setTimeout` + `AbortController` pattern in `AdminUserTypeahead.tsx:101-143`, `DEFAULT_DEBOUNCE_MS=250`). When the debounced `q` is non-empty, `qs.set("q", debouncedQ)`; include the debounced value in the fetch deps. Render a labeled `<input type="search">` above the list. Reset to offset 0 on a new query. Run `pnpm contracts:generate` and commit the artifact.
**Patterns to follow:** `AdminUserTypeahead` debounce + abort; `PipelineView`'s existing `URLSearchParams` query assembly + `apiFetch`.
**Test scenarios:**
- Typing in the box updates the request `q` after the debounce (not per keystroke).
- A new query resets paging to offset 0.
- Clearing the box returns to the unfiltered list.
- An in-flight request is aborted when the query changes (no stale results overwrite).
- `Test expectation:` assert the fetch URL contains the debounced `q`; mock `apiFetch`.
**Verification:** vitest green; manual — typing filters the visible list; CI contracts check passes.

### U3. Facilitator FPG coverage count in the org list

**Goal:** Show each facilitating org's FPG coverage count in `OrgList` so Amy can scan without opening each.
**Requirements:** FPG count.
**Dependencies:** none (independent of search).
**Files:**
- Modify: `apps/api/src/jp_adopt_api/routers/admin.py` (`admin_list_facilitating_orgs` ~666; `FacilitatingOrgAdminRead` ~80)
- Modify: `apps/web/src/components/OrgList.tsx` (meta array ~101-106)
- Modify: `packages/contracts/src/generated/api.ts` (regenerated)
- Test: `apps/api/tests/test_admin_api.py`; `apps/web/src/components/__tests__/OrgList.test.tsx` (add if absent)
**Approach:** Add a `coverage_count: int` field to `FacilitatingOrgAdminRead`. In `admin_list_facilitating_orgs`, compute it with a single grouped aggregate — `select(FacilitatorFpgCoverage.facilitator_org_id, func.count()).group_by(...)` — and map counts onto the rows (avoid N+1; one query for all orgs). Web: add a `Coverage: N` chip to the OrgList row meta. Regenerate + commit contracts.
**Patterns to follow:** `_coverage_for` (the per-org list helper — this is its aggregate sibling); `OrgList.tsx` existing meta chips; `FacilitatorFpgCoverage` composite-PK model.
**Test scenarios:**
- An org with 3 coverage rows reports `coverage_count: 3`; an org with none reports `0`.
- The list endpoint issues one aggregate query, not one-per-org (assert via query count or structure).
- Web renders the count chip for each org.
- `Test expectation:` orgs with mixed coverage counts render distinct values.
**Verification:** API + web tests green; OrgList shows counts; contracts committed.

### U4. `deleted_contacts` suppression table (migration 0030 + model)

**Goal:** A table recording core-side hard-deletes so the DT ETL won't re-import them.
**Requirements:** Hard-delete not re-imported.
**Dependencies:** none.
**Files:**
- Create: `apps/api/alembic/versions/20260624_0030_deleted_contacts.py`
- Modify: `apps/api/src/jp_adopt_api/models.py` (new `DeletedContact` model)
- Test: covered via U5/U6 integration (table existence asserted by migration test harness)
**Approach:** Columns: `id` (uuid pk), `source_system` (text, nullable), `source_id` (text, nullable), `email_normalized` (text, nullable), `deleted_at` (timestamptz default now), `deleted_by` (text — subject id). Partial unique index `uq_deleted_contacts_source` on `(source_system, source_id) WHERE source_id IS NOT NULL` (matches the contacts idempotency convention so an ON CONFLICT lookup works). Index on `email_normalized` for the fallback lookup. New revision **0030** (do not edit applied migrations).
**Patterns to follow:** `EtlDeletedInSource` (`models.py:1009`) is the inverse concept (DT-side deletions) — mirror its shape; `uq_contacts_source_system_source_id` partial-index style.
**Test scenarios:** `Test expectation: none — schema unit; behavior is exercised in U5/U6.` (Migration up/down runs clean; `alembic heads` single head 0030.)
**Verification:** `alembic upgrade head` applies; `alembic heads` = single 0030.

### U5. API: `DELETE /v1/contacts/{id}` hard-delete

**Goal:** Permanently remove a contact + all its data, record suppression, emit an outbox event — in one transaction.
**Requirements:** Hard-delete.
**Dependencies:** U4.
**Files:**
- Modify: `apps/api/src/jp_adopt_api/routers/contacts.py` (new endpoint; new `EVENT_CONTACT_DELETED` constant near line 54)
- Modify: `packages/contracts/src/generated/api.ts` (regenerated)
- Test: `apps/api/tests/test_contacts_record.py` (or a new `test_contact_delete.py`)
**Approach:** New `@router.delete("/{contact_id}", status_code=204)`, dep `_STAFF_DEP` (restrict to `adoption_manager`/`staff_admin` per the do_not_engage role set). In one transaction: `SELECT ... FOR UPDATE` the contact (404 if absent); **explicitly `delete(TransitionAudit).where(contact_id==id)`** (no cascade — would otherwise FK-violate); purge the no-FK orphans for this contact (`delete(IdentityLink)` by the contact's `email_normalized`/`b2c_subject_id`; `delete(MigrationConflict)` by `(source_system, source_id)`); `delete(Contact)` (cascades profile/consent/assignment/interest→match/activity/matchattempt/enrollment); insert a `DeletedContact` row (source_system/source_id/email_normalized/deleted_by); `emit_outbox(db, event_type=EVENT_CONTACT_DELETED, payload={...contact_id...})`; `await db.commit()`. Contracts regen.
**Patterns to follow:** `revoke_user_role` (`admin.py:526`) — delete + `emit_outbox` + single commit; `unassign_contact` 204 shape; Track A loser-delete's manual-repoint-then-cascade precedent (note: it skips transition_audit because stubs never transitioned — this endpoint must NOT skip it).
**Test scenarios:**
- Deleting a contact with profile + interest + match + assignment + activity + transition_audit removes ALL of them (assert each child table empty for that contact_id) and returns 204.
- A contact with `transition_audit` rows deletes cleanly (regression guard for the no-cascade FK).
- A DT-sourced contact (`source_system='dt'`, `source_id`) writes a `deleted_contacts` row with those keys; a forms contact (no source_id) writes the row keyed by `email_normalized`.
- An `Outbox` row with `event_type='jp.adopt.v1.contact.deleted'` is written in the same transaction.
- Deleting a non-existent id → 404.
- Non-privileged role → 403.
- IdentityLink / MigrationConflict rows for the contact are purged (no orphans).
**Verification:** delete tests green; child tables + orphans cleared; suppression + outbox rows present.

### U6. ETL: suppression check in the contacts importer

**Goal:** The hourly DT contacts import skips any contact recorded in `deleted_contacts` (no re-create, no conflict).
**Requirements:** Hard-delete not re-imported.
**Dependencies:** U4.
**Files:**
- Modify: `apps/etl/src/jp_adopt_etl/orchestrator.py` (`_flush_contact_batch` ~410; a `_load_suppressed_contacts` loader mirroring `_load_email_owners` ~362)
- Test: `apps/etl/tests/test_orchestrator_integration.py`
**Approach:** Pre-load the suppression set once per run (a set of `(source_system, source_id)` plus a set of suppressed `email_normalized`), plumbed into `_flush_contact_batch` like `email_owner`. At the top of the per-row body (after `source_id`/`email_normalized` are known, before `pg_insert(Contact)`), if `('dt', source_id)` is suppressed OR the row's email is suppressed → bump a `rows_suppressed` counter and `continue` (no insert, no conflict). Mirror the `_SERVICE_ASSIGNEE_WP_USER_IDS` skip exactly.
**Patterns to follow:** `_SERVICE_ASSIGNEE_WP_USER_IDS` skip (`orchestrator.py:99/880`); the `_load_email_owners`/`_load_existing_dt_post_id_to_contact` pre-load + plumb pattern.
**Test scenarios:**
- A DT post whose `(dt, source_id)` is in `deleted_contacts` is NOT inserted and records NO conflict; `rows_suppressed` increments.
- A DT post whose email matches a suppressed `email_normalized` (but whose source_id isn't suppressed) is skipped via the email fallback.
- A normal (non-suppressed) DT post still imports as before (regression).
- Re-running the import is idempotent (suppressed stays skipped).
**Verification:** orchestrator integration tests green; suppressed contacts never reappear across runs.

### U7. Web: delete UX on the contact record (spam vs hostile)

**Goal:** A staff action to remove a contact — "spam" hard-deletes, "hostile" marks do_not_engage.
**Requirements:** Hard-delete + hostile.
**Dependencies:** U5 (spam path), existing transition (hostile path).
**Files:**
- Modify: `apps/web/src/components/ContactRecord.tsx` (delete affordance) and `apps/web/src/lib/api-client.ts` (add `deleteContact(ctx, id)` calling `DELETE /v1/contacts/{id}`)
- Modify: `apps/web/src/lib/vocab.ts` if new labels are needed
- Test: `apps/web/src/components/__tests__/ContactRecord.test.tsx`
**Approach:** Add a "Remove contact" control offering two clearly-labeled, confirmed actions: **Spam — delete permanently** (calls `deleteContact`, confirm dialog warning it's irreversible) and **Hostile — do not engage** (calls the existing `transitionContact(ctx, id, {kind, to_state:"do_not_engage", reason_code:"other", reason_text})`). On spam success, navigate away from the now-deleted record. Labels via `vocab.ts`.
**Patterns to follow:** existing `transitionContact` usage; the confirm-before-destructive pattern in `OrgDetail` coverage remove (`window.confirm`); `humanizeReasonCode` for labels.
**Test scenarios:**
- Choosing "Spam" → confirm → calls `DELETE /v1/contacts/{id}` then routes away.
- Choosing "Hostile" → calls the transition with `to_state="do_not_engage"`.
- Canceling the confirm makes no request.
- A failed delete surfaces an error and leaves the record visible.
**Verification:** vitest green; manual — both paths behave; contracts committed.

---

## System-Wide Impact

- **OpenAPI/contracts:** U1 (q param), U3 (coverage_count field), U5 (delete op) all change the API surface → `pnpm contracts:generate` + commit `packages/contracts/src/generated/api.ts` once at the end (CI gates this).
- **ETL behavior:** U6 adds a per-row suppression skip to the hourly import — a new `rows_suppressed` counter surfaces in `etl_run`.
- **Data safety:** U5 is irreversible deletion — covered by the FOR-UPDATE + single-transaction + explicit-non-cascade-delete design and the test matrix above.

## Risks & Mitigations

- **FK violation on delete** (transition_audit no-cascade) → explicit delete first; dedicated regression test (U5).
- **Re-import race:** a contact deleted mid-sync. Suppression is written in the delete transaction (U5) and checked at import (U6); worst case the in-flight row re-imports once and the next run skips it. Acceptable given DT shutoff is imminent.
- **Search performance:** ILIKE `%term%` is a seq scan, but the contact set is small (~hundreds). No index needed now; note as a follow-up if the set grows.
- **Wrong list surface:** if Amy meant `/contacts` not the pipeline view, U2 moves to `Contacts.tsx` (small change) — flagged as a decision.

## Scope Boundaries

### Deferred to Follow-Up Work (separate plans, per the confirmed grouping)
- **Email template editor** — in-app editing of drip MJML/Jinja content + activating the 3 draft campaigns. Own plan (storage model for editable content vs files, preview, activation flow).
- **Spreadsheet bulk upload** — adopters/facilitators via file. Own plan (parse, validate, map to intake, dedupe, error reporting).
- **Matching tuning** — the "too picky" thresholds. Own plan; data-grounded review against `docs/runbooks/matching-algorithm-v1.md` (belongs in analysis, not blind code change).
- **`Contacts.tsx` (`/contacts`) search** — if the pipeline-view search isn't the surface Amy meant.

### Non-goals
- Soft-delete/archive (explicitly chosen against — hard-delete with suppression).
- A general suppression/allowlist admin UI (the `deleted_contacts` table is interim until DT shutoff).
- Reusing or changing the email-send `suppression_list`.

## Deferred to Implementation
- Exact `ilike` escape helper choice and whether to add a trigram index later.
- Precise `deleted_by` value (subject id vs display) and outbox payload field set.
- Whether `MigrationConflict` purge should be by `(source_system, source_id)` only or also email.
