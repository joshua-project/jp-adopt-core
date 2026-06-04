---
date: 2026-06-04
topic: drips-ui
---

# Drips UI (#55)

## Summary

A four-surface staff UI in `apps/web` that closes the curl-only gap on drip
campaigns: a list page (with create / activate / pause), a detail page
(meta + step add/delete), a per-contact drips panel (enrollments + manual
enroll), and a suppression admin page. Five small API additions go alongside.

---

## Problem Frame

The drip engine — campaigns, steps, enrollment auto-drain on outbox events,
sending via ACS — has been in production since U10. Staff cannot reach any of
it from the staff app: campaign authoring, manual enrollment, suppression,
and even campaign listing all require `curl` against `docs/runbooks/drip-engine.md`.
That friction was surfaced as a real gap during the 2026-05-23 QA walkthrough
and is on the Phase 1 "strongly recommended" list because Amy will reach for
it the first time she wants to enroll a freshly-matched facilitator in the
"Facilitator welcome" drip without paging engineering.

The pain has two shapes: (1) **operational** — Amy cannot do drip work
without an engineer in the loop; (2) **safety** — the only feedback for a
mistyped MJML template filename is a silent send-failure later when the step
fires. Both compound as more campaigns land.

---

## Actors

- **A1. Staff manager** (`staff_admin` or `adoption_manager`): authors and
  manages campaigns, enrolls contacts, manages suppression.
- **A2. Facilitator-role staff** (`facilitator`): views the per-contact drips
  panel as read-only context; cannot enroll, cannot edit campaigns, cannot
  reach the suppression admin page.
- **A3. Drip worker** (ARQ): out of scope to change, but its existing
  behavior — enroll on outbox events, send due steps via ACS, skip suppressed
  emails — anchors the UI's contracts.

---

## Key Flows

- **F1. Enroll a contact in a campaign from the record page**
  - **Trigger:** A1 is on `/contacts/[id]` for a freshly-matched contact.
  - **Actors:** A1.
  - **Steps:** A1 opens the contact's drips panel → picks an active campaign
    from the dropdown → confirms → API returns the new enrollment → the
    panel refreshes to show the new active enrollment with its current step.
  - **Outcome:** The contact has an active enrollment; the next due step
    fires on the worker's normal cadence.
  - **Covered by:** R5, R6, R7, R12.

- **F2. Author a new campaign from scratch**
  - **Trigger:** A1 is on `/campaigns` and clicks "+ New campaign".
  - **Actors:** A1.
  - **Steps:** Form for name / description / trigger_type /
    trigger_event_type / precedence → submit → land on the new
    `/campaigns/[id]` in `draft` → add steps one at a time (template picked
    from a dropdown, subject + delay_days + send time) → click Activate
    when ready.
  - **Outcome:** A new active campaign exists; matching outbox events will
    enroll contacts.
  - **Covered by:** R1, R2, R3, R8, R10, R11.

- **F3. Suppress an email and prove it sticks**
  - **Trigger:** A1 receives a spam complaint or an unsubscribe request via
    out-of-band channel.
  - **Actors:** A1.
  - **Steps:** A1 visits `/admin/suppression` → enters the address in the
    add form → confirms → the row appears in the list with hash + reason +
    date.
  - **Outcome:** The drip worker will skip future sends to that address; if
    the same address is re-added the API returns the existing row idempotently.
  - **Covered by:** R13, R14, R15.

---

## Requirements

**Campaigns list and creation (surface 1)**
- R1. `/campaigns` lists all non-archived campaigns with: name,
  trigger_event_type, status badge (`draft` / `active` / `paused` /
  `archived`), and last activity timestamp.
- R2. Inline **Activate** and **Pause** action buttons on each row act on the
  matching backend endpoints; the row's badge updates after the call resolves.
- R3. A **"+ New campaign"** button opens a form for: name, description
  (optional), `trigger_type`, `trigger_event_type`, `precedence`. On success
  it routes to `/campaigns/[id]` in `draft` for step authoring.

**Campaign detail (surface 2)**
- R4. `/campaigns/[id]` shows meta (name, description, trigger event type,
  status, version) editable inline via PATCH, plus the campaign's steps in
  position order with `subject`, `delay_days`, `mjml_template_name`,
  `send_at_hour`, `send_at_minute`.
- R8. **Add step** opens a form; `mjml_template_name` is a `<select>` of
  filenames the worker actually has available (sourced from a new
  `GET /v1/drips/templates`).
- R9. **Delete step** removes by position with a confirm step.
- R10. **Activate / Pause / Archive** controls on the detail page mirror the
  list-page actions and the API; the page state reflects the new status.

**Per-contact drips panel (surface 3)**
- R5. `/contacts/[id]` gains a **Drips** tile that lists the contact's active
  enrollments (campaign name, current step position, `last_step_sent_at`) and
  a collapsed history of `step_sent` / `paused` / `exited` events per
  enrollment.
- R6. A **Manual enroll** action on that tile shows a dropdown of active
  campaigns (`paused` and `draft` excluded) and posts to `POST
  /v1/drips/campaigns/{id}/enroll`. The form surfaces server-side errors
  (e.g. duplicate enrollment) inline; success refreshes the tile.
- R7. `/workflow/[id]` gains a small **read-only** drips summary: "active in
  N drips" (or "none"); no manage controls.
- R12. The per-contact panel uses a new
  `GET /v1/contacts/{id}/enrollments` endpoint that returns the contact's
  enrollments and their events in one call (no per-campaign fan-out from
  the UI).

**Suppression admin (surface 4)**
- R13. `/admin/suppression` lists existing suppression entries (hash, reason,
  created date) paginated, sourced from a new `GET /v1/suppression-list`.
- R14. An **Add by email** form posts to a new `POST /v1/suppression-list`;
  the server normalizes + hashes the email, persists, and returns the row.
  Re-adding the same email returns the existing row idempotently (no error).
- R15. A **Remove** action on each row calls a new
  `DELETE /v1/suppression-list/{email_hash}` and updates the table in place.
- R16. `/admin/suppression` is gated to `staff_admin` **and** `adoption_manager`
  (the standard staff set), not just `staff_admin`.

**Cross-cutting (all surfaces)**
- R17. A **Campaigns** entry is added to the top nav next to **Adopters** /
  **Facilitators**.
- R18. All status and reason rendering goes through `vocab.ts` humanizers; a
  new `StatusKind = "campaign"` label table is added for the campaign
  statuses with the existing `STATUS_TONE` colors (active=green, draft=slate,
  paused=amber, archived=rose).
- R19. Existing UI primitives are reused: `DataTable`, `StatusBadge`,
  `StatusFilter` (where filtering applies), `EmptyState`, `LoadingRows`.

---

## Acceptance Examples

- **AE1. Covers R3, R10.** Given the user is on `/campaigns` and clicks
  "+ New campaign", when they submit a valid form, the new campaign lands at
  `/campaigns/[id]` with status `draft`. Then when they click **Activate**
  on the detail page, the badge flips to `active` without a page reload.

- **AE2. Covers R5, R6.** Given a contact with **zero** active enrollments,
  the drips tile shows the empty state and the **Manual enroll** dropdown
  contains only campaigns currently in `active` status. Selecting one and
  confirming results in exactly one new active enrollment listed, with its
  current step at position 0.

- **AE3. Covers R6.** Given a contact already enrolled in campaign X, when
  the user manually enrolls them in X again, the API responds with the
  duplicate-enrollment error and the UI surfaces it inline without dropping
  the user's selection.

- **AE4. Covers R8.** Given the dropdown of MJML templates includes
  `facilitator-welcome.step-0.mjml`, when the user adds a step and chooses
  that template, the step is persisted. The list never exposes a free-text
  field for the template name.

- **AE5. Covers R14.** Given an email is already on the suppression list,
  when the user re-adds the same address, the API returns the existing row
  and the table shows no duplicate.

- **AE6. Covers R7.** Given a contact has no enrollments, the
  `/workflow/[id]` drips summary reads "none"; there is no manage control on
  this page.

- **AE7. Covers R16.** Given a `facilitator`-role staff member who is **not**
  also `adoption_manager` or `staff_admin`, when they request
  `/admin/suppression`, they receive a 403 (or equivalent UI block).

---

## Success Criteria

- Amy can do every drip operation listed in `docs/runbooks/drip-engine.md`
  (create campaign, author steps, activate / pause, manually enroll a
  contact, add / remove suppression) without `curl`, in one staff session.
- A typo in an MJML template name is no longer reachable from the staff UI;
  the dropdown is the only path.
- A downstream agent (`/ce-plan` consumer) does not need to invent product
  behavior or scope — every required surface, action, and gating rule is
  named in this doc, and the only API additions are the five listed.

---

## Scope Boundaries

- **Surface 5 (MJML preview / "render template with sample context")** — the
  issue's explicit v2 stretch.
- **Rich authoring UX** — MJML editor, drag-drop step reorder, A/B testing.
- **Bounce-handler integration** — `hard_bounce` → auto-suppress; tracked
  separately.
- **One-click unsubscribe** — RFC 8058 `List-Unsubscribe` endpoint.
- **Per-enrollment pause API** — the `paused` state exists on `Enrollment`
  but no API mutates it; no UI for per-enrollment pause.
- **Auto-exit enrollment when an email is suppressed mid-campaign** — the
  worker already skips suppressed sends silently; no behavior change here.
- **Web test framework** (Vitest + RTL) — that's #31; this work ships with
  the same `Test expectation: none — #31` annotation we used on the
  match-review and contact-email components.

---

## Key Decisions

- **Wide first cut.** Ship all four surfaces (including campaign creation)
  in one PR rather than slicing across two. Rationale: closing the curl
  dependency partially leaves staff with a worse-of-both-worlds tool — the
  UI implies completeness it doesn't have.
- **Drips panel in two places.** Full panel on `/contacts/[id]`, read-only
  summary on `/workflow/[id]`. Rationale: the record page is the canonical
  contact surface (where the email composer lives); the workflow page is
  action-focused and benefits from a no-management drip signal without
  duplicating management UI.
- **Template name as a dropdown over free-text or server-side validation.**
  Rationale: a typo today turns into a silent send-failure later; surfacing
  the known-good set at compose time is cheaper than diagnosing later.
- **Suppression gated to the standard staff set, not just `staff_admin`.**
  Rationale: suppression is an operational task (spam complaints, manual
  unsubscribes), not a privileged admin function. Matches the gating on
  contacts / drips / manual contacts.

---

## Dependencies / Assumptions

- The backend's drip engine, suppression list, and ACS-send path do not
  change for this work; only five new endpoints are added (one templates
  list + four already named in #55: per-contact enrollments + suppression
  GET/POST/DELETE).
- MJML templates live in a discoverable directory the worker can enumerate
  for `GET /v1/drips/templates`; if that scan needs config (env var or
  similar), planning will surface it.
- `vocab.ts` is the source of truth for human-readable labels; adding a
  `StatusKind = "campaign"` table follows the convention in
  `docs/solutions/conventions/enum-to-ui-label-vocab-2026-05-21.md`.
- The web has no test framework yet (#31); per-component test expectation
  is `none — #31`, mirroring the convention used on match-review and the
  email composer in PR #94.

---

## Outstanding Questions

- None at brainstorm time. Any open implementation details (e.g., template
  discovery mechanism, pagination defaults on the suppression list) belong
  to `/ce-plan`.
