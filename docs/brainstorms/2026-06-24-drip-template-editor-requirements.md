---
title: Drip campaign / email template editor — requirements
status: ready-for-planning
created: 2026-06-24
---

# Drip campaign / email template editor — requirements

## Problem Frame

The 3 drip campaigns (Adopter welcome 8-step, Facilitator post-approval 5-step, Facilitator sign-up welcome 1-step) are seeded but stuck in `status='draft'` because their email content + send timing need adjusting, and there is no way for Amy (non-technical adoption manager) to do that — content lives as MJML/Jinja files in `apps/api/email-templates/` referenced by each step's `mjml_template_name`. Amy needs to author the email content and activate the campaigns herself, without touching code or MJML.

## Actor

- **Amy / adoption staff** (`adoption_manager` / `staff_admin`) — authors drip email content, sets timing, previews, and activates campaigns. Non-technical: must never see or be able to break MJML, branding, or `{{ }}` personalization syntax.

## Decisions (resolved in brainstorm)

- **Editing scope = full content authoring, but body-only in a fixed shell.** Amy edits each step's **body** with a rich-text editor (headings, bold, italics, links, lists) + the **subject line** + the **send timing** (the inter-step delay / "turnaround times" she flagged). The branded **shell** (logo, header, footer, unsubscribe) stays **code-managed** and is not editable.
- **Content moves into the DB**, seeded from the current email-template file copy so Amy starts from the existing wording, not a blank editor.
- **Merge fields via an insert-token picker.** Personalization tokens (e.g. adopter name, FPG, links) are inserted through a controlled UI control so the `{{ }}` syntax can't be broken or mistyped. The available token set is per campaign/step audience.
- **Preview** — Amy can preview the rendered email (body merged into the branded shell, with sample data) before activating.
- **Test send** — Amy can send a test of a step to herself for confidence before going live.
- **Plain-text** is auto-derived from the rich HTML (matches today's `render_step_html` behavior). No separate MJML compilation — the engine already treats templates as HTML-with-Jinja-placeholders.
- **Activation** — Amy flips a campaign `draft → active` from the UI (the 3 seeded campaigns being the immediate target).
- **Live editing model = edit anytime; applies to future sends.** A live campaign stays editable; edits take effect for sends *after* the edit. Already-sent emails are unchanged. No per-step draft/published versioning.

## Success Criteria

- Amy can open a campaign, edit each step's body (rich text) + subject + timing, preview it, send herself a test, and activate it — entirely in the app, no MJML or code.
- The 3 seeded campaigns can be reviewed, adjusted, and activated by Amy.
- Personalization still renders correctly (tokens preserved); branded shell + unsubscribe stay intact and consistent.
- Editing a live campaign's content changes only future sends.

## Scope Boundaries

### Deferred for later (tracked as GitHub issues)
- **Image uploads / embedding** in the body (needs hosting + email-safe rendering).
- **Full layout / branding control** (a true email builder; deliverability + brand risk).
- **Per-step draft vs published versioning** (explicit publish step).
- **A/B testing** of subject/content.

### Outside this product's identity
- Not a general marketing-email platform; scoped to the adoption drip campaigns.

## Dependencies / Notes

- Drip steps already carry `mjml_template_name`, `subject`, `delay_days`, `send_at_hour`, `send_at_minute`; campaigns carry `status`. The worker (ARQ) sends steps via `render_step_html` (treats the template as HTML-with-Jinja). The editor changes WHERE content comes from (DB vs file) and adds the authoring/preview/activation UI; the send path stays.
- Convention reminders for planning: outbox on state changes, contracts must be regenerated/committed, new Alembic revision for any schema (content storage), enum→label vocab for any UI labels.

## Open Questions (for planning)

- Storage shape for editable content (a content column/table per step vs a templates table) — planning decision.
- The exact merge-token set + how it's surfaced — planning decision.
- Migration: how to import the current file content into the DB seed.
