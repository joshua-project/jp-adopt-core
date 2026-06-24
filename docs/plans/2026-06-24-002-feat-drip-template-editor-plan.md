---
title: "feat: Drip email template editor — in-app body authoring, preview, test-send, activation"
type: feat
status: completed
created: 2026-06-24
depth: standard
origin: docs/brainstorms/2026-06-24-drip-template-editor-requirements.md
related_issues: ["#150"]
---

# feat: Drip email template editor

Lets Amy (non-technical `adoption_manager` / `staff_admin`) author each drip
step's **body** (rich text), **subject**, and **send timing** in-app, preview
it, send herself a test, and flip a campaign `draft → active` — with the
branded shell, unsubscribe, and `{{ }}` personalization protected from her.
Origin `docs/brainstorms/2026-06-24-drip-template-editor-requirements.md` is
the source of truth for product behavior; this plan defines **how**.

This is an **enhancement, not a build**. The campaign authoring UI already
exists (`apps/web/src/components/CampaignDetail.tsx`, 815 lines) with step
add/edit/reorder, a working iframe **preview**, and **activate/pause/archive**
buttons; the render path already composes body-into-shell via Jinja
`{% extends "_base.html.jinja" %}`. The work is: (1) a new `body_html` column
seeded from the current template files, (2) render-from-DB, (3) a rich-text
body editor + merge-token picker replacing the template-name dropdown, (4) a
test-send endpoint, and (5) server-side sanitization.

Backend-first sequencing: schema + render + endpoints + one contracts
regeneration land before the UI consumes them.

---

## Problem Frame & Scope

The 3 drip campaigns (Adopter welcome 8-step, Facilitator post-approval
5-step, Facilitator sign-up welcome 1-step) are seeded but stuck in
`status='draft'` because their content and timing need Amy's review, and the
only way to edit content today is to hand-edit MJML/Jinja files in
`apps/api/email-templates/` referenced by each step's `mjml_template_name`.
Amy is non-technical and must never see or be able to break MJML, branding, or
`{{ }}` syntax (origin: Actor).

### In scope

- **Body authoring** — rich text (headings, bold, italic, links, bulleted/
  numbered lists) per step, stored as sanitized HTML in the DB.
- **Subject + timing** — already editable via the existing `StepEditForm`
  (`delay_days`, `send_at_hour`, `send_at_minute`); this plan keeps that and
  removes the template-name dropdown.
- **Merge-token picker** — an "Insert" control that places `{{ contact_display_name }}`
  as an atomic, uncorruptable chip. One token today, built to extend.
- **Preview** — already exists (iframe `srcDoc`); extended to render the
  DB body into the shell. Add the missing `contact_email` to the preview
  context so preview matches send.
- **Test-send** — new: Amy sends a step to herself (default: her own
  `staff_profile.email_normalized`).
- **Activation** — already exists (`activate_campaign`); no change needed
  beyond confirming the 3 seeded campaigns can be activated once edited.
- **Content seed** — a migration imports each step's current `{% block body %}`
  copy from its `*.mjml` file into `body_html`.

### Out of scope (deferred → GH #150)

- Image uploads / embedding in the body.
- Full layout / branding control (a true email builder).
- Per-step draft vs published versioning. **Decision: live body edits apply to
  all future sends, including already-enrolled people's upcoming steps**
  (confirmed with user 2026-06-24). Send-time renders live `body_html`; steps
  are not version-pinned per enrollment, so no snapshotting work is needed.
- A/B testing.
- General marketing-email platform (origin: Outside this product's identity).

---

## Key Decisions

1. **Storage: a new nullable `body_html` Text column on `campaign_step`**, not
   a content table. The brainstorm rules out versioning, so a separate table
   buys nothing. `mjml_template_name` is **kept** (relaxed to nullable) as a
   fallback so the seed migration is safe and reversible. Render prefers
   `body_html` when non-null, else the file. (origin: Open Question — storage
   shape.)

2. **Render-from-DB via `env.from_string`.** When `body_html` is set,
   `render_step_html` wraps it as
   `{% extends "_base.html.jinja" %}{% block body %}<body_html>{% endblock %}`
   and renders through the existing `FileSystemLoader` (so `extends` still
   resolves the shell). The shell stays a code-managed file. (See origin
   note: "changes WHERE content comes from.")

3. **Editor: Tiptap v3** (`@tiptap/react@3.27.x`) as a `dynamic(ssr:false)`
   client component with `immediatelyRender:false`, a trimmed StarterKit
   (headings/bold/italic/link/lists only), and a **hand-rolled Tailwind
   toolbar** (avoid Tiptap's React-18-only UI Components kit). Chosen because
   `getHTML()` returns clean semantic HTML as the canonical persistence format
   (Lexical/Slate are JSON-first), and ProseMirror's atomic-node primitive is
   exactly what the uncorruptable merge-token needs. Verify the React 19
   peer-dep status at build time (runtime is fine; pnpm may warn).

4. **Merge token: a custom `atom: true` inline node** that serializes to the
   literal `{{ contact_display_name }}` in `getHTML()`. Atomic = the chip
   deletes whole or not at all; the user cannot backspace into and corrupt the
   `{{ }}`. The stored HTML therefore already contains the literal token, which
   Jinja substitutes at render. (origin: Open Question — merge-token set.)

5. **Sanitize on save with `nh3`** (Rust/ammonia binding; `bleach` is
   deprecated). Tight allowlist: `h1 h2 h3 p strong b em i a ul ol li br`,
   `a` limited to `http/https/mailto` + `rel="noopener noreferrer"`. The DB is
   the trust boundary; render treats body as trusted (`| safe`). **`{{ }}`
   tokens are plain text and pass `nh3.clean()` untouched — sanitize BEFORE
   Jinja render, never after.** (origin: convention reminder — safety.)

6. **Plain-text via `html2text`** off the *rendered* HTML (tokens already
   substituted), replacing the current regex tag-strip which drops link URLs
   and list structure.

7. **Test-send reuses the worker ACS sender**, dispatched via
   `BackgroundTasks` mirroring `apps/api/src/jp_adopt_api/routers/auth_magic_link.py:110`.
   A test-send is not a contact state change → no outbox row (consistent with
   AGENTS.md "never call an email client directly from a handler" — it calls
   the shared worker send helper, not a raw client).

8. **No `Campaign.version` bump on body edits.** Body edits must reach
   in-flight enrollments (decision above); since send-time reads live
   `body_html` regardless of version, the existing `_bump_version_if_published`
   behavior on step edits is left as-is — it does not gate body delivery. A
   unit test documents this so the coupling is intentional, not accidental.

---

## Requirements Traceability

| Origin requirement | Addressed by |
|---|---|
| Amy edits body (rich text) + subject + timing, no MJML | U5 (editor), U6 (token picker), existing `StepEditForm` |
| Content moves into DB, seeded from current copy | U1 (column), U2 (seed migration) |
| Merge fields via insert-token picker, `{{ }}` unbreakable | U6 (atom node) |
| Preview body-in-shell with sample data | U3 (render), U4 (preview/context), existing `PreviewModal` |
| Test send to herself | U4 (endpoint), U7 (worker inline send), U5 (button) |
| Plain-text auto-derived | U3 (`html2text`) |
| Activation draft→active in UI | existing `activate_campaign` + UI (verify only, U8) |
| Live edits → future sends incl. in-flight, no versioning | U3 render-from-live-DB; Decision 8 |
| Branded shell + unsubscribe stay intact | U3 (shell unchanged), U9 (sanitize allowlist) |

---

## Implementation Units

Sequencing: U1 → U2 → U3 → U4 (backend) → contracts regen → U5/U6 (UI, parallel-safe
after contracts) → U7 (worker) interleaves with U4 → U8/U9 verification. U9
(sanitize) is part of U4's save path but called out for its own tests.

---

### U1: `body_html` column on `campaign_step`

**Goal:** Add nullable `body_html` Text to `campaign_step`; relax
`mjml_template_name` to nullable. New Alembic revision only — never edit `0010`.

**Files:**
- Create: `apps/api/alembic/versions/20260624_0032_campaign_step_body_html.py`
  (down_revision = current head `20260624_0031`)
- Modify: `apps/api/src/jp_adopt_api/models.py` (`CampaignStep`, ~line 1277) —
  add `body_html: Mapped[str | None]`; make `mjml_template_name` nullable.

**Approach:** `op.add_column("campaign_step", sa.Column("body_html", sa.Text(), nullable=True))`
and `op.alter_column("campaign_step", "mjml_template_name", nullable=True)`.
Downgrade drops the column and (best-effort) restores NOT NULL.

**Patterns to follow:** any recent single-column migration under
`apps/api/alembic/versions/`; AGENTS.md "never edit an applied migration."

**Test scenarios:**
- Migration applies cleanly on a DB at `0031` and `alembic downgrade -1`
  reverses the column add (happy path + reversibility).
- Model round-trips a `CampaignStep` with `body_html=None` and with a value.

**Verification:** `cd apps/api && uv run --extra dev alembic upgrade head` then
`alembic downgrade -1` then `upgrade head`; `uv run --extra dev pytest` green.

---

### U2: Seed `body_html` from current template files

**Goal:** Populate `body_html` for every existing step from the
`{% block body %}` content of its `mjml_template_name` file, so Amy starts from
the existing wording. Idempotent (only where `body_html IS NULL`).

**Files:**
- Modify: `apps/api/alembic/versions/20260624_0032_campaign_step_body_html.py`
  (same revision as U1 — column add + data seed in one revision).

**Approach:** Extract the inner-`{% block body %}…{% endblock %}` text from each
of the 14 template files (8 `adopter-welcome.step-*`, 5 `facilitator-approved.step-*`,
1 `facilitator-welcome.step-0`) and inline those strings as **literals in the
migration** (do not read files at migration runtime — prod containers don't all
ship `email-templates/`; the worker image does but the migrate step may not).
`UPDATE campaign_step SET body_html = :body WHERE mjml_template_name = :name AND body_html IS NULL`.
The extraction is a one-time authoring step done while writing the migration
(read each file, copy the block body verbatim into the migration as a literal).

**Patterns to follow:** `apps/api/alembic/versions/20260624_0031_backfill_dt_active_not_matched.py`
for a data-mutation migration with a no-op-safe downgrade.

**Test scenarios:**
- After upgrade, each seeded step's `body_html` is non-null and contains the
  expected token (e.g. adopter steps contain `{{ contact_display_name }}`).
- Re-running the UPDATE is a no-op (idempotency — `body_html IS NULL` guard).
- A step with a pre-existing `body_html` is not overwritten.

**Verification:** apply on a seeded DB (`scripts/seed-local.sh` first), then a
SQL check that `body_html` is populated and renders (feed into U3 preview).

**Execution note:** characterization-first — capture each file's current block
body verbatim before inlining, so the seed reproduces today's emails exactly.

---

### U3: Render `body_html` into the shell + `html2text` plain-text

**Goal:** Teach `render_step_html` to render a DB body fragment into the
branded shell, and derive plain-text with `html2text`. Unify the merge-field
context so preview and send agree.

**Files:**
- Modify: `apps/api/src/jp_adopt_api/domain/drips.py` (`render_step_html`, ~line 453)
- Modify: `apps/api/pyproject.toml` — add `html2text` (and `nh3`, used in U9).
- Test: `apps/api/tests/` — new `test_render_step_body_html.py` (mirror any
  existing `render_step_html` test; search `tests/` for current coverage).

**Approach:**
- Add a `body_html: str | None` parameter (or read from the step). When set:
  `env.from_string("{% extends '_base.html.jinja' %}{% block body %}" + body_html + "{% endblock %}").render(**ctx)`
  — keep the existing `FileSystemLoader` so `extends` resolves. Else fall back
  to `env.get_template(template_name)`.
- Consider relaxing `StrictUndefined` to a non-raising undefined **for the body
  path only**, OR validate tokens on save (U9) so unknown tokens never reach
  render. Plan choice: validate on save (tighter); keep `StrictUndefined` so a
  typo'd token is a loud failure in test/preview, not a silent blank in prod.
- Replace the regex plain-text derivation with `html2text` run on the
  **rendered** HTML (tokens already substituted). Configure to suppress
  markdown emphasis artifacts; preserve link URLs and list bullets.
- Define a single `build_step_context(contact_display_name, contact_email,
  campaign_name, step_position)` helper and use it from preview, send, and
  test-send so all three pass identical keys (fixes the existing preview-misses-
  `contact_email` mismatch).

**Patterns to follow:** existing `render_step_html` body; the worker context at
`apps/worker/src/jp_adopt_worker/tasks/send_drip_step.py:160`.

**Test scenarios:**
- Body-from-DB renders inside the shell (asserts shell markers — JP header,
  footer `{{ current_year }}` resolved — AND the body text are both present).
- `{{ contact_display_name }}` in `body_html` substitutes to the sample name.
- Fallback: a step with `body_html=None` still renders from its file.
- Plain-text from `html2text` preserves a link URL (`<a href>` → text + URL)
  and list bullets, where the old regex dropped them.
- Preview and send contexts expose identical keys (guard against re-divergence).
- `StrictUndefined`: an unknown `{{ token }}` raises (documents the loud-fail).

**Verification:** `uv run --extra dev pytest tests/test_render_step_body_html.py -v`.

---

### U4: API — body edit + send-test endpoint

**Goal:** Accept `body_html` on step create/patch (sanitized — U9); add
`POST /v1/drips/campaigns/{id}/steps/{position}/send-test`; surface a
merge-token list for the picker.

**Files:**
- Modify: `apps/api/src/jp_adopt_api/routers/drips.py` — schemas
  `CampaignStepIn` (~:61), `CampaignStepPatch` (~:72), `CampaignStepRead` (~:94),
  `StepPreviewResponse` (~:170); new `send_test` handler; new `GET .../merge-tokens`
  (or a static constant exposed in `StepPreviewResponse`).
- Modify: router docstring (`drips.py:14`) — it's stale ("no preview/send-test").
- Test: `apps/api/tests/` — extend the drips router test (search for the
  existing `test_drips*`/`preview` test file).

**Approach:**
- Add `body_html: str | None` to `CampaignStepIn`/`Patch`/`Read` and
  `StepPreviewResponse`. Relax `mjml_template_name` from `min_length=1` required
  to optional when `body_html` is provided (validator: at least one of the two).
- `send-test`: body `{to_email?: EmailStr}`; default to the caller's
  `staff_profile.email_normalized`. Render via U3, then
  `background_tasks.add_task(send_drip_test_inline, ...)` (U7). 202 Accepted.
  Guard `_drips_dep` (already `require_role(*STAFF_ROLES)` = Amy's roles).
- Expose the allowed merge tokens (today: `contact_display_name` with a
  friendly label "Recipient name") so the UI picker and server validator share
  one source.

**Patterns to follow:** `preview_step` (`drips.py:659`) for render reuse;
`auth_magic_link.py:110` for the background-task inline-send pattern;
`extra="forbid"` on inputs, `from_attributes=True` on reads.

**Test scenarios:**
- PATCH a step with `body_html` persists sanitized HTML; response echoes it.
- Create a step with `body_html` and no `mjml_template_name` succeeds; with
  neither fails 422.
- `send-test` with no `to_email` enqueues a send to the caller's email; with an
  explicit `to_email` uses it; returns 202.
- `send-test` as a non-staff role → 403.
- `merge-tokens` returns `contact_display_name` with its label.

**Verification:** `uv run --extra dev pytest tests/<drips test> -v`; then
`pnpm contracts:generate` (U-contracts) and commit the artifact.

---

### U-contracts: Regenerate `@jp-adopt/contracts`

**Goal:** Single contracts regeneration after U1–U4 land the API surface.

**Files:**
- Modify: `packages/contracts/**` (generated), `apps/api/openapi.json`.

**Approach:** `pnpm openapi:export && pnpm contracts:generate`. Per the
asdf/pnpm friction noted in AGENTS.md and prior sessions, if pnpm's node
resolution fails, run the real CLI entry via node 22 (export:
`uv run python -m jp_adopt_api.scripts.export_openapi`; generate:
`openapi-typescript` + `scripts/post-process.mjs`). Commit the artifact or CI
fails the "contracts artifact must be committed" check.

**Verification:** `git diff --exit-code packages/contracts` is clean after
commit; web typechecks against the new `paths`.

---

### U5: Web — rich-text body editor in `StepEditForm`

**Goal:** Replace the template-name `<select>` in the step editor with a Tiptap
body editor; keep subject/delay/hour/minute; add a "Send test" button.

**Files:**
- Create: `apps/web/src/components/RichTextEditor.tsx` (Tiptap client component)
- Modify: `apps/web/src/components/CampaignDetail.tsx` (`StepEditForm` ~:305,
  `PreviewModal` ~:463 unchanged but now fed DB body)
- Modify: `apps/web/src/lib/api-client.ts` — add `sendTestStep` (mirror
  `previewCampaignStep` ~:520); `patchCampaignStep` already carries `body_html`
  via regenerated types.
- Modify: `apps/web/package.json` — add `@tiptap/react`, `@tiptap/pm`,
  `@tiptap/starter-kit`, `@tiptap/extension-link`.
- Test: `apps/web/src/components/__tests__/CampaignDetail.test.tsx` (extend) and
  `__tests__/RichTextEditor.test.tsx` (new).

**Approach:**
- `RichTextEditor` is `'use client'`, loaded via `dynamic(() => …, {ssr:false})`,
  `useEditor({ immediatelyRender: false, … })`, trimmed StarterKit (disable
  image, codeBlock, blockquote, horizontalRule, strike), plus Link. Hand-rolled
  Tailwind toolbar: H1/H2/H3, bold, italic, link, bullet list, ordered list,
  and the token-insert button (U6). `value`/`onChange` over `getHTML()`.
- `StepEditForm`: swap the template dropdown for `<RichTextEditor>`; submit
  `body_html` through `patchCampaignStep`. Keep subject + timing fields.
- "Send test" button calls `sendTestStep`; show a success/error toast.
- Status labels stay on `humanizeStatus(value, "campaign")` — do not hand-roll.

**Patterns to follow:** existing `StepEditForm`/`PreviewModal` in
`CampaignDetail.tsx`; the iframe `srcDoc` preview is the only existing
HTML-injection site and is reused unchanged.

**Test scenarios (vitest + RTL):**
- Editor renders, toolbar bold toggles `<strong>` in `getHTML()` output.
- `StepEditForm` submit calls `patchCampaignStep` with the edited `body_html`.
- "Send test" calls `sendTestStep` with the step's campaign/position; success
  toast on 202, error toast on failure.
- Loading the form seeds the editor with the step's existing `body_html`.

**Verification:** `pnpm --filter web test`; manual: edit a step body, Preview
shows it in the shell, Send test arrives.

---

### U6: Merge-token atom node + insert picker

**Goal:** A Tiptap inline `atom:true` node that renders as a chip in the editor
and serializes to literal `{{ contact_display_name }}`; an "Insert field"
toolbar control.

**Files:**
- Create: `apps/web/src/components/editor/MergeToken.ts` (Tiptap Node extension)
- Modify: `apps/web/src/components/RichTextEditor.tsx` (register node + button)
- Test: `apps/web/src/components/__tests__/MergeToken.test.tsx`

**Approach:** Node `{ name: 'mergeToken', inline: true, group: 'inline',
atom: true, selectable: true }` with a `name` attribute. `renderHTML` emits the
literal `{{ <name> }}` text (Decision 4, option 1) so stored HTML feeds Jinja
directly; `parseHTML`/NodeView renders a styled non-editable chip with the
friendly label. Insert button reads the U4 `merge-tokens` list (one entry
today). Keep an `ALLOWED_TOKENS` constant so adding token #2 is one line.

**Test scenarios:**
- Inserting the token yields `{{ contact_display_name }}` in `getHTML()`.
- Backspace adjacent to the chip deletes the whole token (atomic), never a
  partial `{{ contact_display_nam`.
- Loading body HTML containing `{{ contact_display_name }}` re-parses to a chip
  (round-trip) — OR, if option-1 literal serialization means it loads as text,
  assert it still serializes back identically (document whichever round-trip
  shape the implementation takes).

**Verification:** `pnpm --filter web test`.

---

### U7: Worker — inline test-send helper

**Goal:** A one-shot send helper the API background task calls, reusing the ACS
sender so test emails go out the same path as real drips.

**Files:**
- Modify: `apps/worker/src/jp_adopt_worker/tasks/send_drip_step.py` — extract/
  expose `send_drip_test_inline(to_email, subject, html, plain)` reusing
  `_send_via_acs` (~:52). (Or a thin module the API imports.)

**Approach:** Factor the ACS-send tail of `send_drip_step` into a reusable
helper; `send_drip_test_inline` renders (or receives rendered html/plain from
U4) and calls `_send_via_acs`. Honor the dev fallback (no ACS connection string
→ log + success) so local test-send works without Azure.

**Patterns to follow:** `_send_via_acs` and the send tail of `send_drip_step`.

**Test scenarios:**
- `send_drip_test_inline` with ACS unset hits the dev-fallback (logs, returns
  success) — no network.
- It calls `_send_via_acs` with the rendered subject/html/plain (mock ACS,
  assert args).

**Verification:** `uv run --extra dev pytest` (worker tests, if present) or the
API test exercising the background task.

---

### U8: Activation verification (no new code expected)

**Goal:** Confirm the existing `activate_campaign` flow works end-to-end for a
campaign whose steps now carry `body_html`, and that the 3 seeded campaigns can
go `draft → active` from the UI.

**Files:** none expected; if a gap surfaces (e.g. activate rejects steps with
null `mjml_template_name` now that body is primary), patch `activate_campaign`
(`drips.py:423`).

**Test scenarios:**
- Activate a draft campaign whose steps have `body_html` and null
  `mjml_template_name` succeeds (regression guard for the U1 nullable relax).
- Activate with zero steps still 409s (unchanged).

**Verification:** existing drips activation test still green; add the
null-template-name case.

---

### U9: Server-side HTML sanitization on save

**Goal:** Sanitize `body_html` with `nh3` on every create/patch, against a
tight allowlist; ensure `{{ }}` tokens survive.

**Files:**
- Modify: `apps/api/src/jp_adopt_api/routers/drips.py` (sanitize in the
  create/patch handlers, or a `domain` helper `sanitize_body_html`)
- Modify: `apps/api/pyproject.toml` — `nh3` (added in U3's dependency edit)
- Test: `apps/api/tests/test_sanitize_body_html.py`

**Approach:** `sanitize_body_html(raw)` → `nh3.clean(raw, tags={...13 tags},
attributes={"a": {"href","title"}}, url_schemes={"http","https","mailto"},
link_rel="noopener noreferrer", clean_content_tags={"script","style"})`. Call
it in the create and patch paths **before** persisting. Optionally validate any
`{{ token }}` against `ALLOWED_TOKENS` and reject unknown tokens (keeps
`StrictUndefined` render safe).

**Test scenarios:**
- `<script>alert(1)</script>` is stripped (and its content dropped).
- `<a href="javascript:…">` href is removed; `http/https/mailto` survive with
  `rel="noopener noreferrer"`.
- Allowed tags (`h1-h3,p,strong,em,a,ul,ol,li,br`) pass through.
- **`{{ contact_display_name }}` is byte-identical after `clean()`** (the
  load-bearing token-survival test).
- Disallowed tag (e.g. `<table>`, `<img>`) is removed but inner text kept.
- (If token validation added) unknown `{{ bogus }}` → 422 on save.

**Verification:** `uv run --extra dev pytest tests/test_sanitize_body_html.py -v`.

---

## Deferred to Implementation

- **Exact `html2text` config flags** to suppress markdown artifacts — tune
  against the seeded bodies during U3.
- **Tiptap React-19 peer-dep handling** — whether a pnpm `peerDependencyRules`
  override is needed; resolve at U5 install time against `@tiptap/react@3.27.x`.
- **Token round-trip shape** (U6) — whether stored literal `{{ }}` re-parses to
  a chip on load or loads as text that re-serializes identically; pick the
  simpler robust option during U6 and lock the test to it.
- **CSS inlining for email clients** — the research flags that bare semantic
  tags (`<h1>`, `<a>`) render with ugly client defaults in Gmail/Outlook. The
  current shell already hand-builds chrome; decide during U3 whether to add
  inline styles for body tags (or a `css-inline` pass over the composed email).
  Low-risk to defer to a follow-up if the seeded bodies look acceptable — log
  it rather than silently skipping. **Track as a note on #150 if deferred.**

## Risks

- **Seed fidelity (U2):** inlining 14 block bodies as literals is manual; a
  copy error ships wrong copy. Mitigation: characterization-first capture +
  per-campaign render assertions; Amy reviews in preview before activating.
- **Sanitizer vs. tokens (U9):** the one true correctness risk is `nh3`
  mangling `{{ }}` or sanitize running after Jinja. Mitigation: the
  byte-identical token test + the "sanitize before render, never after"
  ordering encoded in U3/U9 tests.
- **Tiptap + React 19 (U5):** peer-dep friction; runtime is fine. Mitigation:
  headless editor + own toolbar (skip the React-18 UI kit),
  `immediatelyRender:false`, `dynamic ssr:false`.

## Verification (whole feature)

1. `cd apps/api && uv run --extra dev pytest` green (render, sanitize, router,
   migration).
2. `pnpm --filter web test` green (editor, token, step form).
3. `git diff --exit-code packages/contracts` clean after commit.
4. Manual on a seeded local stack: open a campaign → edit a step body (insert
   the recipient-name token, bold, a link, a list) → Preview shows it in the
   branded shell with sample data → Send test arrives in inbox → activate the
   campaign (`draft → active`). Repeat for the 1-step facilitator-welcome.
5. `scripts/smoke-local.sh` still passes its 12 checkpoints.
