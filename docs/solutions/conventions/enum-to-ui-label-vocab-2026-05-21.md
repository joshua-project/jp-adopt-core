---
title: Backend enum values get explicit, kind-aware label tables — not mechanical humanize
date: 2026-05-21
category: conventions
module: web/lib/vocab
problem_type: convention
component: tooling
severity: medium
applies_when:
  - Rendering a backend enum value (status, state, reason code) as visible UI text
  - Adding a new status badge, filter chip, select option, or Kanban column that sources its label from an API enum
  - The same enum value has different human meanings across entity kinds (e.g. do_not_engage for adopters vs facilitators)
  - A mechanical string helper (e.g. underscore-to-space, title-case) is already in place but produces output that feels like machine copy
symptoms:
  - Status labels read as machine output despite a humanize helper being present
  - The same value (e.g. do_not_engage) renders identically for all entity kinds, losing context
  - A local component re-implements a humanize function, shadowing the shared vocab helper
  - Tailwind uppercase class on a badge mangles sentence-case labels written for CRM users
  - Form select options render raw enum values (e.g. capacity_full) because the option text was forgotten
related_components:
  - documentation
  - development_workflow
tags:
  - labels
  - vocab
  - enum-display
  - nextjs
  - frontend
  - status-badge
  - copy
---

# Backend enum values get explicit, kind-aware label tables — not mechanical humanize

## Context

When the adopter and facilitator pipeline pages shipped, the reaction from review was: "the actual labels need to be updated in the UI (right now they are snake case)."

The feedback wasn't literally about underscores appearing on screen. The raw enum values (`potential_adopter`, `do_not_engage`) were being passed through a mechanical helper that replaced `_` with spaces and title-cased each word. The result looked like this:

- `potential_adopter` → "Potential Adopter"
- `do_not_engage` → "Do Not Engage"
- `sent_back` → "Sent Back"
- `capacity_full` → "Capacity Full"

Combined with Tailwind `uppercase` on status badges, the on-screen rendering was even louder: "POTENTIAL ADOPTER", "DO NOT ENGAGE". All-caps, visually aggressive, still semantically wrong.

These labels aren't broken. They're mechanical — the developer's internal data model rendered verbatim rather than a label a product person would write. The actual labels a staff member would use are different: "Needs FPG selection" (not "Potential Adopter"), "Returned to queue" (not "Sent Back"), "Opted out" (not "Do Not Engage"). These explain the stage to a human; the enum names were chosen for database clarity and code legibility, not for display.

The same pattern appeared in form `<select>` elements for send-back and workflow transitions, where raw enum values like `capacity_full` and `geography_mismatch` were rendered directly as option text.

## Guidance

**Use explicit per-kind label tables, not mechanical `_` → space conversion.**

Create a `vocab.ts` module (or equivalent for the framework) holding named record objects — one per enum domain — that map every known value to its display string. The lookup function takes a `kind` parameter because the same enum value can mean different things depending on which entity it belongs to.

```ts
// The mechanical fallback — keep this PRIVATE, never the primary path.
// A value falling through here means a table is missing an entry.
function humanize(s: string): string {
  return s
    .replace(/_/g, " ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

const ADOPTER_STATUS_LABELS: Record<string, string> = {
  potential_adopter: "Needs FPG selection",
  sent_back: "Returned to queue",
  do_not_engage: "Opted out",
  // ...one entry per enum value
};

const FACILITATOR_STATUS_LABELS: Record<string, string> = {
  do_not_engage: "Paused",   // same key, different meaning
  // ...
};

const MATCH_STATUS_LABELS: Record<string, string> = {
  recommended: "Awaiting review",
  triage: "Needs triage",
  // ...
};

const REASON_CODE_LABELS: Record<string, string> = {
  capacity_full: "Facilitator at capacity",
  geography_mismatch: "Geography mismatch",
  // ...
};

export type StatusKind = "adopter" | "facilitator" | "match";

export function humanizeStatus(
  value: string | null | undefined,
  kind: StatusKind = "adopter",
): string {
  if (!value) return "—";
  const table =
    kind === "facilitator" ? FACILITATOR_STATUS_LABELS
    : kind === "match"     ? MATCH_STATUS_LABELS
    :                        ADOPTER_STATUS_LABELS;
  return table[value] ?? humanize(value);
}

export function humanizeReasonCode(value: string | null | undefined): string {
  if (!value) return "—";
  return REASON_CODE_LABELS[value] ?? humanize(value);
}
```

Two design weaknesses worth naming, both of which a code reviewer will flag and which a future iteration of this pattern should address:

1. **`Record<string, string>` discards type safety.** Each table accepts any string key, so a typo (`potental_adopter:`) compiles fine and only surfaces in QA when the mechanical fallback fires. The stronger shape is `Record<AdopterStatus, string>` where `AdopterStatus` is a literal union — that turns "extend the table when adding an enum value" into a compile error.

2. **`kind: StatusKind = "adopter"` makes the silent-wrong-label footgun the default behavior** — exactly what the rest of this doc warns against. The shape that shipped optimizes for back-compat at the cost of safety; a stricter API would require `kind` (no default) so the caller is forced to be explicit, or expose per-kind exports (`humanizeAdopterStatus`, `humanizeFacilitatorStatus`, `humanizeMatchStatus`) so each call site picks the right one by name.

Both are tracked as follow-ups; the table-based convention is the foundation, the type-safety polish builds on it.

**Audit every consumer when the helper signature changes.** Adding `kind` to `humanizeStatus` means every call site must be updated. The places where snake_case labels most commonly leak in this codebase:

- **Status badge components.** A shared `StatusBadge` is easy to update centrally, but must receive `kind` from its parent. Drop Tailwind `uppercase` from the badge wrapper when switching to sentence-case labels — otherwise "Reached out" becomes "REACHED OUT" and undoes the work.
- **Filter chip components** (`StatusFilter`) that render option labels.
- **Kanban column headers** (`KanbanBoard`) that show status names as headings.
- **Form `<select>` / `<option>` elements** — the most common miss. `value=` and `{children}` are separate. Easy to populate one from the label table while leaving the other as the raw enum:

  ```tsx
  // BAD — option text is the raw enum
  {reasonCodes.map((code) => (
    <option key={code} value={code}>{code}</option>
  ))}

  // GOOD — value is raw, text is humanized
  {reasonCodes.map((code) => (
    <option key={code} value={code}>{humanizeReasonCode(code)}</option>
  ))}
  ```

- **Hardcoded lowercase options** like `<option value="adopter">adopter</option>` — search for these explicitly.
- **Mixed-kind contact lists.** When the same row can be an adopter or a facilitator, branch on `party_kind` before calling `humanizeStatus`:

  ```tsx
  badge={
    c.party_kind === "facilitator" && c.facilitator_status ? (
      <StatusBadge status={c.facilitator_status} kind="facilitator" />
    ) : c.adopter_status ? (
      <StatusBadge status={c.adopter_status} kind="adopter" />
    ) : undefined
  }
  ```

- **Local re-implementations of the humanizer.** Grep for `humanize` across components; in this codebase `StatusBadge.tsx` had its own local copy shadowing the shared vocab helper. Delete locals, import from `vocab.ts`.
- **`humanizeStatus(x)` calls with no kind argument.** The default `"adopter"` keeps back-compat but silently produces wrong labels for facilitator and match statuses.

## Why This Matters

**Mechanical conversion strips meaning.** `sent_back` rendered as "Sent Back" tells a staff member that something went back somewhere, but not why the label exists or what they should do next. "Returned to queue" tells them the record is waiting for reassignment. The difference is the product team's mental model encoded as UI copy — and that model lives nowhere in the codebase unless you write it down.

**Context changes meaning.** `do_not_engage` for an adopter means they've opted out of the program. For a facilitator it means they're temporarily paused for new matches. A single mechanical label like "Do Not Engage" applied to both creates confusion at best and a training problem at worst. The `kind` parameter is what prevents the two from colliding.

**Uppercase status labels are hostile.** Screen readers announce all-caps text letter-by-letter in some configurations, or with unnatural stress. Visually they read as warnings or errors regardless of the actual status. Sentence-case with explicit labels is calmer and more readable.

**Label changes are O(1) edits.** When a product person decides "Needs FPG selection" should be "Pending facilitator assignment," there is exactly one line to change in one file. With mechanical conversion scattered across components or hardcoded in multiple `<option>` elements, the same change requires a grep and a multi-file patch, and misses are invisible until someone notices the inconsistency.

## When to Apply

- Any time an enum value from the database or API will be displayed directly to end users (status badges, column headers, filter labels, dropdown options, notification text).
- When the same enum value appears in multiple entity contexts (adopters, facilitators, matches) and the display meaning differs between them.
- When status labels have been defined collaboratively with product or program staff — these definitions belong in a table, not in ad-hoc title-casing.
- When a UI review surfaces "machine-looking" labels — even with no literal underscores, the feeling that labels read like a developer wrote them rather than a product person is the signal.
- When adding a new enum value to an existing domain — extend the table; don't rely on the mechanical fallback becoming the permanent display.

## Examples

**Before — mechanical conversion (filter chips on `/adopters`):**

```
All · New · Potential Adopter · Contacted · Engaged · Matched · Sent Back · Active · Inactive · Do Not Engage
```

With Tailwind `uppercase` applied: `POTENTIAL ADOPTER`, `DO NOT ENGAGE`, `SENT BACK`.

**After — explicit label table:**

```
All · New · Needs FPG selection · Reached out · In conversation · Matched · Returned to queue · Active adoption · Inactive · Opted out
```

Sentence case, semantically rich, screen-reader friendly.

**Before — form option rendering raw enum:**

```tsx
{REASON_CODES.map((r) => (
  <option key={r} value={r}>{r}</option>
))}
// renders: <option value="capacity_full">capacity_full</option>
```

**After — humanized option text, raw value preserved for submission:**

```tsx
{REASON_CODES.map((r) => (
  <option key={r} value={r}>{humanizeReasonCode(r)}</option>
))}
// renders: <option value="capacity_full">Facilitator at capacity</option>
```

Note `value=` keeps the raw enum (correct — that's what gets submitted to the API), while `{children}` uses the display label.

**The full label tables shipped in `apps/web/src/lib/vocab.ts` as of PR #48** (commit `3785990`, 2026-05-21):

| Adopter status | Old (mechanical) | New (explicit) |
|---|---|---|
| `new` | New | New |
| `potential_adopter` | Potential Adopter | Needs FPG selection |
| `contacted` | Contacted | Reached out |
| `engaged` | Engaged | In conversation |
| `matched` | Matched | Matched |
| `sent_back` | Sent Back | Returned to queue |
| `active` | Active | Active adoption |
| `inactive` | Inactive | Inactive |
| `do_not_engage` | Do Not Engage | Opted out |

| Facilitator status | Old | New |
|---|---|---|
| `not_ready` | Not Ready | Onboarding pending |
| `ready` | Ready | Ready for matches |
| `do_not_engage` | Do Not Engage | Paused |

| Match status | Old | New |
|---|---|---|
| `recommended` | Recommended | Awaiting review |
| `triage` | Triage | Needs triage |
| `active` | Active | In progress |
| `sent_back` | Sent Back | Returned |

| Reason code | Old | New |
|---|---|---|
| `capacity_full` | Capacity Full | Facilitator at capacity |
| `geography_mismatch` | Geography Mismatch | Geography mismatch |
| `language` | Language | Language mismatch |
| `theological_concern` | Theological Concern | Theological concern |
| `not_ready` | Not Ready | Adopter not ready |
| `other` | Other | Other (see notes) |

## Related

- `docs/solutions/conventions/alembic-migration-edit-after-apply-2026-05-20.md` — different domain (migration files vs. UI copy), same general theme of "create explicit artifacts instead of relying on mechanical/implicit behavior."
- `apps/web/src/lib/vocab.ts` — the canonical label tables.
- `apps/web/src/components/StatusBadge.tsx` — consumer with `kind` prop.
- `apps/web/src/components/StatusFilter.tsx`, `apps/web/src/components/KanbanBoard.tsx` — same pattern.
- PR #48 on `joshua-project/jp-adopt-core` — the implementation, with full before/after screenshots.
