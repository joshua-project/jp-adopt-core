---
title: "feat: Contact record page + adoption field parity (single track)"
type: feat
status: active
created: 2026-05-26
depth: deep
origin: docs/dt-parity-inventory.md
related_issues: ["#56", "#55", "#52", "#57", "#31"]
---

# feat: Contact record page + adoption field parity (single track)

One delivery track that takes jp-adopt-core's contact UI from "thin" to
DT-parity: a canonical `/contacts/[id]` record page that first surfaces the data
we already store, then grows the JP-custom adoption field set (the
`dt-adoption-fields` plugin schema) end-to-end — model → API → contracts → UI →
intake. Grounded in `docs/dt-parity-inventory.md` (§2.5 forms-canonical set,
§2.6 provenance split).

**Scope is "everything needed"** per the requester. The units are ordered so the
read-only page (Group A) is independently shippable; Groups B/C layer the field
parity on top.

> **Effort honesty (requested):** the full track (3 migrations, ~38 new fields,
> contracts regen, inline edit, intake promotion, cross-repo forms wiring, staff
> assignment) is **realistically 3–5 focused days for one developer**, not 1.
> The **1-day achievable slice is Group A** (U1–U5): the contact record page over
> existing data. Groups B/C are where the day target breaks — flagged per unit.

---

## Problem Frame

DT (the system being replaced) gives staff a dense, tiled contact record with
~50 form-driven fields, an activity/comment feed, and inline edit. jp-adopt-core
has no `/contacts/[id]` route at all, stores ~12 contact fields, and drops the
rich intake fields into an ignored `extra` bag (`schemas.py` `IntakeBase.extra`).
Staff currently bounce between `/matches`, `/workflow/[id]`, and (after #55) a
drips panel with no "everything about this person" surface.

Two distinct gaps, one track:
1. **Surfacing** — existing data (`activity_log`, `transition_audit`, `Match`,
   `Enrollment`, `AdopterInterest`) is invisible. (Issue #56 as written.)
2. **Field parity** — the JP-custom adoption fields (`dt-adoption-fields`
   plugin) aren't modeled, persisted, exposed, edited, or captured at intake.

---

## Scope Boundaries

**In scope**
- `/contacts/[id]` record page (header, quick actions, read tiles).
- The 42 JP-custom plugin fields (`docs/dt-parity-inventory.md` §2.6 A) →
  ~38 net-new (jp-adopt-core already has adopter_status, facilitator_status,
  commitment_level). Persisted, exposed, inline-editable.
- Per-FPG fields on `AdopterInterest` (commitment_types, engagement_status,
  facilitation_services, network_services).
- MOU consent record persistence.
- Intake promotion (forms fields → typed + persisted) and `jp-adopt-forms`
  wiring (cross-repo).
- Staff assignment (`assigned_to`) — new concept.

**Out of scope (stock disciple.tools / WordPress — §2.6 C)**
- faith_status, baptism/milestones, coaching/group/relation connections,
  seeker_path, tags, gender, age, `people_groups` connection, favorite,
  follow/unfollow. jp-adopt-core uses structured `AdopterInterest`/`fpg`, not
  DT's `fpg_submission_data` JSON blob.

### Deferred to Follow-Up Work
- List-view parity (saved/custom filters, CSV/BCC/phone exports) — separate effort.
- Merge/dedupe + contact-history diff view.
- `GET /timeline` server-side merge optimization may ship after the page if the
  client-side fan-out is acceptable (U5 note).

---

## Key Technical Decisions

1. **Persistence: dedicated `contact_profile` table (1:1 with `contacts`).**
   Rationale: profile edits must NOT bump `Contact.version` — that column gates
   the `SELECT FOR UPDATE` + version-check used by the match/transition flows
   (AGENTS.md optimistic-locking convention); co-locating ~38 mutable fields on
   `contacts` would create write contention and spurious 409s. A 1:1 table gives
   typed columns + CHECK-constrained enums + indexable arrays in one focused
   migration. *Alternatives rejected:* columns-on-`contacts` (version churn);
   JSONB blob (loses typed filters/CHECKs, regresses list-view filtering).
2. **Per-FPG data on `AdopterInterest`, structured — not a JSON blob.** Mirrors
   the existing model and is strictly better than DT's `fpg_submission_data`
   (which the plugin itself hides behind a custom renderer).
3. **Status stays transition-only.** `ContactPatch` never gains
   `adopter_status`/`facilitator_status` (AGENTS.md state-machine-via-HTTP;
   `schemas.py:54` comment). Edit action covers free-form + profile fields only.
   `referral_source`/`campaign`/`partner` are readonly (set at intake).
4. **MOU consent → dedicated `consent` table** (consent_type, version,
   content_hash, accepted_at, conversation_id, evidence jsonb), not a status enum.
5. **Contact page IA = the plugin's tile keys** (`docs/dt-parity-inventory.md`
   §2.6 A): adopter_pipeline, facilitator_pipeline, contact_info,
   adoption_profile, facilitation_profile, connection_prefs, network_prefs,
   vetting, fpg_commitments, engagement, form_submission.
6. **OpenAPI is the contract source.** Every API-surface unit ends with
   `pnpm contracts:generate`; CI fails otherwise (AGENTS.md).

---

## High-Level Technical Design

```
contacts (existing, hot row, version-gated)
  ├─1:1─ contact_profile      (NEW — ~33 adoption fields, enums via CHECK, text[])
  ├─1:N─ adopter_interest     (EXTEND — commitment_types[], engagement_status, …)
  ├─1:N─ consent              (NEW — MOU acceptance records)
  └─1:N─ contact_assignment   (NEW — staff assignment; or assigned_to col)

Read page  GET /v1/contacts/{id}            (ContactRead + profile)
           GET /v1/contacts/{id}/matches | /transitions | /activity | /timeline
Write      PATCH /v1/contacts/{id}          (ContactPatch + profile fields)
           POST  /v1/contacts/{id}/activity (add note)
           POST  /v1/contacts/{id}/transition (existing — status)
Intake     POST /v1/intake/*                (promote `extra` bag → typed + persist)
```

*Directional guidance for review, not implementation specification.*

---

## Group A — Contact record page over existing data (the 1-day slice)

### U1. Read endpoints for the contact record
**Goal:** Serve the per-contact aggregates the page needs.
**Requirements:** #56 (timeline/matches/transitions/activity).
**Dependencies:** none.
**Files:** `apps/api/src/jp_adopt_api/routers/contacts.py`,
`apps/api/src/jp_adopt_api/schemas.py`, `apps/api/tests/test_contacts_record.py` (new).
**Approach:** Add `GET /v1/contacts/{id}/matches` (all Matches across the
contact's AdopterInterests — today matches are keyed on interest),
`/transitions` (paginated `transition_audit`), `/activity` (paginated
`activity_log`), and a convenience `/timeline` that merges the three newest-first.
Role-gate to staff (`require_role`). Reuse `humanize`-friendly raw values; UI
humanizes.
**Patterns to follow:** `routers/matches.py` query + pagination shape;
`require_role` from `deps.py`.
**Test scenarios:**
- Happy: contact with 2 interests → `/matches` returns Matches from both.
- Happy: `/transitions` returns audit rows newest-first, paginated.
- Edge: unknown contact id → 404; contact with zero activity → empty list, not 500.
- Error: caller without staff role → 403.
- Integration: `/timeline` merges transition + match + activity in correct order.
**Verification:** endpoints return expected shapes; `pnpm contracts:generate`
produces no uncommitted diff after commit.

### U2. Add-note write endpoint
**Goal:** Let staff post a note to `activity_log` from the page.
**Requirements:** #56 (Add note quick action).
**Dependencies:** none.
**Files:** `apps/api/src/jp_adopt_api/routers/contacts.py`,
`apps/api/src/jp_adopt_api/schemas.py`, `apps/api/tests/test_contacts_record.py`.
**Approach:** `POST /v1/contacts/{id}/activity` writing an `activity_log` row
(kind=`note`, author resolved from caller). No outbox needed (internal note).
**Patterns to follow:** `activity_log` model in `models.py`; author handling per
`StaffIdentityLink` doc comment.
**Test scenarios:**
- Happy: post note → row persisted with author + occurred_at.
- Edge: empty body → 422.
- Error: non-staff → 403.
**Verification:** posted note appears in `/activity`; contracts regen clean.

### U3. `/contacts/[id]` route + page shell
**Goal:** New route with header + tile scaffold consuming `ContactRead` + U1.
**Requirements:** #56 (Header section).
**Dependencies:** U1.
**Files:** `apps/web/src/app/contacts/[id]/page.tsx` (new),
`apps/web/src/components/ContactRecord.tsx` (new),
`apps/web/src/lib/api-client.ts`.
**Approach:** Header = display_name, party-kind-aware `StatusBadge`, email,
country, language chips, origin chip. Tile container keyed to the §2.6 IA.
Row-click links from `/matches`, `/adopters`, `/facilitators` land here.
**Patterns to follow:** `components/StatusBadge.tsx`, `DataTable.tsx`,
`lib/vocab.ts` (`humanizeStatus(value, kind)`), `useApiContext.ts`.
**Test scenarios:** `Test expectation: none — no web test harness yet (issue #31)`;
**manual verification required** (see Verification).
**Verification:** load `/contacts/<seeded id>` in the running stack (web :3030,
bearer dev-local) and confirm header renders for an adopter AND a facilitator;
screenshot both.

### U4. Quick actions — Transition, Edit, Add note
**Goal:** Wire the three actions into the header.
**Requirements:** #56 (Quick actions).
**Dependencies:** U2, U3.
**Files:** `apps/web/src/components/ContactRecord.tsx`,
`apps/web/src/components/WorkflowTransition.tsx` (reuse),
`apps/web/src/lib/api-client.ts`.
**Approach:** Transition reuses the existing `WorkflowTransition` modal. Edit =
inline `PATCH /v1/contacts/{id}` (display_name/party_kind now; widens in U10).
Add note posts to U2. Status edits are NOT in the Edit form (transition-only).
**Patterns to follow:** `WorkflowTransition.tsx` modal invocation; existing
PATCH call in `ContactsB2C.tsx`.
**Test scenarios:** `Test expectation: none — web harness gap (#31)`; manual.
**Verification:** in the running stack, transition a contact via the modal and
see the badge update; edit display_name and see it persist; add a note and see it
in the activity tile.

### U5. Read tiles — Interests, Matches, Drips, Workflow history, Activity
**Goal:** Render the read sections from U1 + existing endpoints.
**Requirements:** #56 (sections 3–7); coordinates with #55 drips panel.
**Dependencies:** U1, U3.
**Files:** `apps/web/src/components/ContactRecord.tsx`, new tile components under
`apps/web/src/components/contact-record/`.
**Approach:** AdopterInterests (rop3 + humanized FPG + notes), Matches
(`DataTable`: recommended_at, org, status, decider+reason), Drip enrollments
(consume #55 panel; placeholder until it lands), Workflow history
(`transition_audit`, humanized reason codes via `reason-codes.ts`), Activity log.
Client-side fan-out to the 4 endpoints; `/timeline` swap is a deferred optimization.
**Patterns to follow:** `MatchQueue.tsx`/`MatchReview.tsx` table usage;
`reason-codes.ts`, `vocab.ts`.
**Test scenarios:** `Test expectation: none — web harness gap (#31)`; manual.
**Verification:** on a seeded multi-FPG adopter (e.g. Bob) all five tiles render
real rows; screenshot.

---

## Group B — Adoption field data model + API + UI (parity core)

### U6. Migration + model: `contact_profile` table
**Goal:** Persist the ~33 contact-level JP-custom fields.
**Requirements:** parity §2.6 A (contact_info/adoption_profile/facilitation_profile/
connection_prefs/network_prefs/vetting/engagement/form_submission tiles).
**Dependencies:** none (additive).
**Files:** `apps/api/alembic/versions/20260526_0012_contact_profile.py` (new
revision — never edit applied migrations),
`apps/api/src/jp_adopt_api/models.py`,
`apps/api/tests/test_contact_profile_model.py` (new).
**Approach:** 1:1 table FK→contacts(id) ON DELETE CASCADE. Scalars (nickname,
primary/secondary contacts, website, state_region, mou_signature_name,
doctrinal_distinctives, accountability_memberships, additional_notes,
referral_source, campaign, partner, engagement_score int, last_contact_date,
next_followup_date, commitment_date), enums via CHECK (adopter_type, entity_size,
preferred_communication, mou_status), booleans (works_with_fpgs,
willing_to_facilitate, want_facilitator_connection, want_network_connection,
has_doctrinal_distinctives, has_accountability_membership), and `text[]`
(ministry_areas, commitment_types, facilitation_entity_types,
facilitation_entity_sizes, facilitator_entity_types, desired_facilitator_info,
network_partner_info). Mirror enum option-sets from
`dt-adoption-fields/includes/custom-fields.php`.
**Patterns to follow:** existing migrations in `apps/api/alembic/versions/`;
CHECK-constraint style on `Contact` in `models.py`.
**Execution note:** confirm migration applies cleanly on a fresh DB AND on the
existing seeded DB (additive, so safe) before proceeding.
**Test scenarios:**
- Migration up/down round-trips on a clean DB.
- Insert with valid enum → ok; invalid enum value → IntegrityError (CHECK).
- 1:1 uniqueness: two profiles for one contact → rejected.
- Cascade: deleting a contact removes its profile.
**Verification:** `alembic upgrade head` → `0012`; model imports; tests green.

### U7. Migration + model: extend `adopter_interest`
**Goal:** Per-FPG adoption/facilitation answers.
**Requirements:** parity §2.5 (per-FPG fields).
**Dependencies:** none.
**Files:** `apps/api/alembic/versions/20260526_0013_interest_fpg_fields.py` (new),
`apps/api/src/jp_adopt_api/models.py`, `apps/api/tests/test_adopter_interest.py` (new).
**Approach:** add `commitment_types text[]`, `engagement_status` (CHECK:
ready/potential/none), `facilitation_services text[]`, `network_services text[]`.
Nullable/defaulted; existing rows unaffected.
**Test scenarios:** valid insert; bad engagement_status → CHECK violation; existing
rows readable with NULLs.
**Verification:** upgrade head → `0013`; tests green.

### U8. Migration + model: `consent` table
**Goal:** Persist MOU acceptance records.
**Requirements:** parity §2.5 (MOU consent record).
**Dependencies:** none.
**Files:** `apps/api/alembic/versions/20260526_0014_consent.py` (new),
`apps/api/src/jp_adopt_api/models.py`, `apps/api/tests/test_consent.py` (new).
**Approach:** `consent(id, contact_id FK, consent_type, version, content_hash,
accepted_at, conversation_id, evidence jsonb, created_at)`. content_hash CHECK
64-hex. Index on (contact_id, consent_type).
**Test scenarios:** insert valid MOU consent; bad content_hash → rejected;
multiple consents per contact allowed.
**Verification:** upgrade head → `0014`; tests green.

### U9. Expand `ContactRead` + `ContactPatch`
**Goal:** Expose profile fields (read) and make free-form/profile fields editable.
**Requirements:** #56 (Edit); parity field exposure.
**Dependencies:** U6.
**Files:** `apps/api/src/jp_adopt_api/schemas.py`,
`apps/api/src/jp_adopt_api/routers/contacts.py`,
`apps/api/tests/test_contact_patch.py` (extend existing patch tests).
**Approach:** `ContactRead` gains a nested `profile` object. `ContactPatch` gains
the editable profile fields (NOT status; NOT readonly referral_source/campaign/
partner). Keep `extra="forbid"` so removed status keys still 422 (`schemas.py:66`).
PATCH writes go to `contact_profile`, leaving `contacts.version` untouched
(Decision 1). Regenerate contracts.
**Patterns to follow:** existing `ContactPatch` validator (`schemas.py:54`);
`reject_null_for_non_nullable_columns`.
**Test scenarios:**
- Patch a profile enum field → persisted; `contacts.version` unchanged.
- Patch `adopter_status` → 422 (forbidden key).
- Patch `referral_source` (readonly) → 422.
- Patch invalid enum value → 422.
- Covers #56: edit free-form field round-trips through ContactRead.
**Verification:** tests green; `pnpm contracts:generate` diff committed.

### U10. Intake promotion — typed form fields + persistence
**Goal:** Stop dropping rich form fields; persist them on submission.
**Requirements:** parity (intake wiring, core side).
**Dependencies:** U6, U7, U8.
**Files:** `apps/api/src/jp_adopt_api/schemas.py` (IntakeBase/AdoptionIntake/
FacilitationIntake/FpgInterestIn), `apps/api/src/jp_adopt_api/routers/intake.py`,
`apps/api/tests/test_intake.py` (extend).
**Approach:** Promote the §2.6-A fields from the `extra` bag into typed optional
fields; on intake, write `contact_profile`, extend `AdopterInterest` rows, and
insert `consent` when MOU accepted. Preserve `extra="ignore"` for forward-compat.
Keep the existing `submission.received` outbox emission.
**Test scenarios:**
- Adoption intake with full profile → contact + profile + interests persisted.
- Facilitation intake with MOU → consent row written.
- Unknown extra field → ignored (no 422).
- Covers existing intake idempotency (ApiIdempotencyKey) still holds.
**Verification:** intake tests green; smoke an adoption submission end-to-end.

### U11. Web — profile tiles + inline edit
**Goal:** Render + edit the JP-custom tiles on the record page.
**Requirements:** parity §2.6 A; #56.
**Dependencies:** U5, U9.
**Files:** new tile components under `apps/web/src/components/contact-record/`,
`apps/web/src/lib/vocab.ts` (new enum label tables).
**Approach:** One component per plugin tile (adoption_profile,
facilitation_profile, connection_prefs, network_prefs, vetting, engagement,
form_submission), party-kind-gated. Inline edit via expanded `ContactPatch`.
Readonly fields render non-editable.
**Patterns to follow:** `vocab.ts` `humanizeStatus`/`humanizeReasonCode`
(per AGENTS.md enum→UI-label convention — add per-kind label tables, no
mechanical underscore-replace).
**Test scenarios:** `Test expectation: none — web harness gap (#31)`; manual.
**Verification:** edit a field in each tile against the running stack; values
persist and re-render with humanized labels; screenshot the full record.

---

## Group C — Intake wiring, assignment, polish

### U12. `jp-adopt-forms` → jp-adopt-core wiring (cross-repo)
**Target repo:** `jp-adopt-forms`
**Goal:** Send the new fields to jp-adopt-core's intake.
**Requirements:** parity (intake wiring, forms side).
**Dependencies:** U10.
**Files (jp-adopt-forms, repo-relative):** `src/lib/dt-client.ts` (or a new
`jp-adopt-core` submission client), `src/lib/submissions/types.ts`,
`src/lib/__tests__/dt-field-parity.test.ts` (extend the allowlist test).
**Approach:** Map the zod form values (`src/lib/schema.ts`,
`src/app/[locale]/adopt-frontier-people-groups/schema.ts`) to the expanded
jp-adopt-core intake payload. Coordinate with the DT cutover — this is the
forms' eventual target.
**Execution note:** cross-repo; the biggest threat to any aggressive timeline.
**Test scenarios:** mapper emits only fields jp-adopt-core accepts (extend the
existing parity test); maximal adoption + facilitation submissions map cleanly.
**Verification:** forms unit tests green against the new payload shape.

### U13. Staff assignment (`assigned_to`)
**Goal:** Per-contact staff ownership — a new concept for jp-adopt-core.
**Requirements:** parity §2.6 B (DT `assigned_to`); requester scope.
**Dependencies:** U3.
**Files:** `apps/api/alembic/versions/20260526_0015_contact_assignment.py` (new),
`apps/api/src/jp_adopt_api/models.py`, `apps/api/src/jp_adopt_api/routers/contacts.py`,
`apps/web/src/components/ContactRecord.tsx`,
`apps/api/tests/test_contact_assignment.py` (new).
**Approach:** `contact_assignment(contact_id, user_b2c_subject_id, assigned_at,
assigned_by)`; `POST/DELETE /v1/contacts/{id}/assignment`; surface on the record
header + a "my contacts" filter on `/contacts`. (Routing still goes to
facilitator orgs — this is a staff-workflow overlay, not a matching change.)
**Test scenarios:** assign/unassign; reassign replaces; non-staff → 403; filter
returns only the caller's assigned contacts.
**Verification:** assign in UI; appears in header + filter; contracts regen clean.

### U14. Cross-surface link-in + nav
**Goal:** Make the record reachable everywhere a contact appears.
**Requirements:** #56 (link from rows).
**Dependencies:** U3.
**Files:** `apps/web/src/components/MatchQueue.tsx`, `MatchReview.tsx`,
`PipelineView.tsx`, `ContactsB2C.tsx`, `FacilitatorPortal.tsx`, `SiteHeader.tsx`.
**Approach:** Row click → `/contacts/[id]`. Consider folding `/workflow/[id]`
into a tab here (issue #56 offers retiring it) — keep as follow-up if it grows scope.
**Test scenarios:** `Test expectation: none — web harness gap (#31)`; manual.
**Verification:** clicking a row in matches/adopters/facilitators lands on the record.

---

## System-Wide Impact
- **OpenAPI contracts** (`packages/contracts/`): regen after U1, U2, U9, U10, U13.
- **Optimistic locking**: profile/consent/assignment writes deliberately avoid
  `contacts.version` (Decision 1) — verify no match/transition 409 regressions.
- **Intake pipeline** + **`jp-adopt-forms`** (separate repo) coordinate at U10/U12.
- **Web test gap (#31)**: web units have no automated coverage; each carries
  explicit manual/screenshot verification. Consider landing #31's vitest+RTL
  setup first if regression risk is a concern.
- **Drips panel (#55)**: U5 consumes it; ship #55 first or placeholder.

## Risk Analysis & Mitigation
- **Effort vs 1-day target** (high): full track is 3–5 days. *Mitigation:* ship
  Group A first (the genuine 1-day slice); treat B/C as follow-on.
- **Contract drift** (med): forgetting `contracts:generate` breaks CI.
  *Mitigation:* per-unit verification step.
- **Cross-repo coordination** (med): U12 spans `jp-adopt-forms`. *Mitigation:*
  core-side (U10) is independently testable; forms wiring can trail.
- **Migration safety** (low): all additive (new tables / nullable columns); new
  revisions only, never edit applied (AGENTS.md).

## Verification Strategy
- API units: pytest under `apps/api/tests/`; `alembic upgrade head` reaches `0015`.
- Contract: `pnpm contracts:generate` yields no uncommitted diff.
- Web units: manual drive of the running stack (web :3030, bearer dev-local) on
  seeded data, screenshots per unit (web harness gap #31).
- End-to-end: an adoption intake submission persists profile + interests + consent
  and renders on `/contacts/[id]`.
