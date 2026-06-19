# Demo findings & punch list — 2026-06-18 (Amy session)

Consolidated from the live demo triage and the Monologue note
*"Troubleshooting Login & Data Sync Issues"* (2026-06-18, ~08:41 CT).
Master tracker for everything surfaced; execution is sequenced in the
roadmap at the bottom.

## The root cause behind most of this

**CORRECTED 2026-06-18 (verified against prod admin API + a read-only DB
count).** An earlier draft of this section claimed the DT backfill never
ran and drips were never seeded — both wrong. What's actually true:

- **DT sync IS running and the historical import largely landed** —
  1,165 hourly runs; prod `contacts` by `source_system`: **dt=249**,
  forms=198, staff_seed=1 (total 448). Not a missing import.
- **The real gap is a silent conflict backlog** (errors=0, so it looked
  clean): **210 `duplicate_email`** (DT contacts colliding with forms
  contacts → DT history not merged), **246 `assignee_no_subject`**
  (assignments referencing unmapped DT user handles), **12
  `fpg_not_found`**. This is the Phase-2 reconciliation work below.
- **Drip campaigns ARE seeded** — all 3 (Adopter 8-step, Facilitator
  post-approval 5-step, Facilitator welcome 1-step) with every step +
  template ref — but all in `status='draft'`. "Templates not loaded" =
  not activated + content not yet adjusted, **not** a seeding gap.

Verify anytime with the firewall-free admin API
(`/v1/admin/migration-conflicts/summary`, `/v1/admin/etl-runs`,
`/v1/drips/campaigns`). The definitive `source_system` split needs a
read-only DB count (operator-run).

---

## A. Auth & access

### A1 — Amy login: Entra "assignment required" block ✅ RESOLVED in demo
- **Symptom:** `AADSTS50105` — `amy.banta@joshuaproject.net` blocked; the
  Enterprise App `jp-adopt-core-web` (client `3a6d7ff8-fb64-48df-9302-6a236a194db5`)
  has *Assignment required = Yes* and she wasn't assigned. No token issued.
- **Fix applied:** Amy assigned to the app in Entra + her JP OID
  (`77fb39e1-3acd-4012-bd8d-2a2a34534dc1`) inserted directly into
  `user_roles` as `staff_admin`. Sign-in confirmed working.
- **Earlier red herrings:** the account-picker/SSO theory and the
  app-layer `user_roles` theory were both wrong — the block was one layer
  up, in Entra, before any token.

### A2 — Codify Amy's JP identity in a migration (`0029`) 🔜 READY
- **Why:** the `user_roles` row was a direct prod INSERT — outside Alembic
  history, lost on any DB rebuild/restore, and she has no `staff_profile`
  row so the **daily digest won't reach her JP account**.
- **Do:** new revision `0029` seeding `user_roles` + `staff_profile` for
  OID `77fb39e1-…`, idempotent (no-op against the existing row).
- **Plan:** `docs/superpowers/plans/2026-06-18-demo-fixes-batch-1.md` Task 3.

### A3 — MSAL silent-SSO: no account picker 🔜 READY
- **Symptom:** `loginPopup({ scopes })` (`apps/web/src/components/Contacts.tsx:110`)
  passes no `prompt`, so an existing tenant session signs in silently with
  the cached account — no chance to pick a different one.
- **Fix:** add `prompt: "select_account"`. Plan Task 2.

---

## B. Web UI bugs

### B1 — Facilitator shows a bogus "Matched" pill 🔜 READY
- **Example:** *Elkin Valley Baptist Church* (a facilitator) shows green
  "Matched" although `matched` isn't a valid facilitator status.
- **Cause:** `Contacts.tsx:248-256` cascades — if a facilitator has no
  `facilitator_status` it **falls through and renders its stray
  `adopter_status`** with the adopter label. `PipelineView.tsx:86` does it
  correctly (picks the field by `party_kind`). Two layers: the UI cascade
  **and** facilitators carrying an `adopter_status` they shouldn't.
- **Fix:** branch on `party_kind`, never cross-render. Plan Task 1.

### B2 — Matching algorithm too "picky" 📋 NEEDS PLAN
- Not pulling enough matches; queue is near-empty. Needs tuning against
  real data — see `docs/runbooks/matching-algorithm-v1.md`. Own plan +
  data review before changing thresholds.

### B3 — Drafts-link bug ✅ FIXED (verify)
- Links sent to users with drafts pointed at a generic "people" page
  instead of their specific form → users thought progress was lost.
  Reported fixed in the meeting; **add a regression test + verify in prod.**

---

## C. Data & sync (operator-led)

### C1 — DT `disciple.tools` backfill ⏳ IN PROGRESS
- Backfill failed; being reloaded. Run the full re-baseline (no
  `--watermark`) per `docs/runbooks/dt-cron-sync.md:68`. Verify
  `source_system='dt'` count climbs and `migration-conflicts/summary`
  is sane.

### C2 — Seed operational data into prod 📋 ROOT-CAUSE FIX
- Drip campaigns + steps (and any other `seed-local.sh` data the app
  needs) don't exist in prod. Decide the mechanism: a real seed migration
  vs. a documented operator runbook. **This unblocks C-, drip-, and
  digest-class symptoms together.**

### C3 — Load legacy spreadsheet adopters ⏳ WEEKEND
- Old spreadsheet adopter data to be loaded (operator, over the weekend).
  401 total adopters today incl. test accounts.

### C4 — Facilitator duplicates 📋
- 18 new facilitator entries with suspected duplicates — needs a dedup
  pass / merge.

---

## D. New features (each needs its own brainstorm + plan)

### D1 — Delete contacts 📋 SCOPE CLARIFIED
- From the meeting: **spam → hard-deletable; hostile → mark
  `do_not_engage`** (don't delete). So this is a *narrow* hard-delete for
  spam plus surfacing the existing `do_not_engage` transition — not a
  generic delete. Mind the outbox/audit/DT-history blast radius for hard
  delete. Brainstorm scope (which entity, who's authorized, cascade) first.

### D2 — Spreadsheet upload for contacts 📋
- Bulk-add adopters/facilitators via upload (complements manual add).

### D3 — Email template editor 📋 (reportedly in progress)
- In-app editing of email content (turnaround times, etc.) so campaigns
  can be adjusted without a deploy. Templates currently ship as files in
  `apps/api/email-templates/`.

### D4 — Quick search bar 📋
- Fast contact lookup for navigation.

---

## E. Infra / ops

### E1 — Email campaigns: adjust templates + activate 📋
- Campaigns "ready but not active" pending template content tweaks.
  Facilitators: 1 welcome email. Adopters: 7-step campaign. Preview every
  step (`/v1/drips/campaigns/{id}/steps/{pos}/preview`) before activating.

### E2 — DNS rebind 📋
- `…mangodesert-2647616f.centralus.azurecontainerapps.io` →
  **`adopt.joshuaproject.net`**. Update the SPA app-reg redirect URIs
  (`/auth/callback`, `post_logout_redirect_uris`) to the new host or
  sign-in breaks again (`docs/runbooks/multi-idp-b2c.md`).

---

## Roadmap

**Phase 0 — done / in-flight:** A1 (login) ✅ · B3 (drafts) ✅ · C1 (DT reload) ⏳

**Phase 1 — ship now (small, code; this branch):** A2 `0029` · A3
`select_account` · B1 match-pill leak.
→ `docs/superpowers/plans/2026-06-18-demo-fixes-batch-1.md`

**Phase 2 — data & ops (operator-led, this weekend):** C1 verify · C2
prod seed · C3 spreadsheet adopters · C4 dedup · E1 activate campaigns ·
E2 DNS rebind.

**Phase 3 — features (brainstorm → plan each):** D1 delete · D2 upload ·
D3 template editor · D4 search · B2 matching tuning.
