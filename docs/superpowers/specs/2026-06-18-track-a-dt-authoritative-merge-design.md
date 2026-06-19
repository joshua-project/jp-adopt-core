# Track A — DT-authoritative `duplicate_email` merge — Design

**Status:** Approved design (brainstorm 2026-06-18). Supersedes the diagnostics-only Track A shipped in PR #142.

**Context:** 210 `duplicate_email` migration conflicts: a person exists as both a jp-adopt-forms-seeded **core contact** (live) and a legacy **Disciple.Tools (DT) contact**. The DT contact was diverted to `migration_conflicts` (never inserted into `contacts`), so **core holds only the forms contact row**; the authoritative DT data lives in legacy DT MySQL. DT is the record of truth — Amy curates contacts there — so the merge applies DT data **onto** the existing core contact. See [[dt-reconciliation-merge-policy]] and `docs/follow-ups/2026-06-18-demo-findings.md`.

The shipped Track A (`apps/etl/src/jp_adopt_etl/reconcile/track_a_duplicate_email.py`) is diagnostics-only with a hard `--allow-unsafe-merge` gate. This design replaces its merge logic and **removes that gate**.

## Goal

For each clean `duplicate_email` conflict, merge the authoritative DT contact onto the live core contact (DT wins), durably so the hourly cron stops re-detecting it — without clobbering live core matching or weakening consent. Ambiguous and live-match cases go to Amy, not auto-merged.

## Authority model (decided)

**Full DT overwrite of descriptive data + workflow status**, with three carve-outs:
1. **Open core match → skip + flag.** Any contact with an open `match` row is left untouched and routed to Amy's review list (protects in-core triage from a stale DT status reset).
2. **Ambiguous identity → skip + flag.** Same email but DT name vs core name mismatch (shared/family inbox) is not auto-merged; goes to Amy's review list. Amy-approved cases merge on a later run.
3. **Protected contact → skip + flag (operator decision 2026-06-18).** A contact is PROTECTED and is never overwritten — skipped to Amy's review list — when its core `adopter_status == 'do_not_engage'` **OR** core `facilitator_status == 'do_not_engage'` **OR** `Contact.local_modified_after_import is True`. These are the opt-out / most-restrictive signals: `do_not_engage` is an explicit human "do not contact" disposition, and `local_modified_after_import` means staff edited the contact in core after import. This mirrors the ETL importer's own guard (`orchestrator.py` upsert: `where=Contact.local_modified_after_import.is_(False)`, which records a `local_modified_after_import` conflict for Amy instead of overwriting). The merge applies the most-restrictive interpretation: when in doubt, do not clobber a human's edit or opt-out — route it to Amy.

   Consent is still handled most-restrictively as a child-table rule (an opt-out in DT *or* core stays opted-out; DT may only *add* consent, never weaken a core opt-out).

## Merge rules (per data category)

| Data | Rule |
|---|---|
| Descriptive fields (display name, phone, country_code, origin, profile fields) | DT overwrites where DT has a value; keep core value only where DT is empty |
| `adopter_status` / `facilitator_status` | DT overwrites (direct write, consistent with the ETL importer). The open-match pre-check has already excluded anything live. |
| Consent | Most-restrictive of DT vs core (opt-out in either wins) |
| ActivityLog | Append DT history (additive) |
| AdopterInterest (FPG) | Union — add DT's, keep core's |
| ContactAssignment | DT-authoritative — replace with DT's |
| Drip enrollments, `b2c_subject_id`, matches | Untouched (core-only concepts DT has no view of) |

## Durable resolution (decided)

On merge, set the core contact's `source_system='dt'` and `source_id=<dt id>`. The next hourly sync then finds the contact by `(source_system, source_id)` (update path) instead of by email — no collision, no new conflict — and the contact stays DT-authoritative going forward. Then delete the `migration_conflicts` row. **No orchestrator change required.**

## Execution flow

1. **Dry-run (default):** read all `duplicate_email` conflicts → re-read each DT contact from legacy MySQL → classify each as `clean-merge` / `skip-open-match` / `ambiguous-review` / `skip-other`. No writes (an `etl_run` audit row with rows_out=0 only). Emit an **Amy review list** (CSV/JSON) of the ambiguous + open-match cases with enough context to decide.
2. **Amy review:** marks ambiguous cases same-person (approve) or different-person (leave). Approval feeds the next apply.
3. **Apply (`--apply`, operator):** for `clean-merge` + Amy-approved cases, perform the merge per the rules above — one transaction per contact, bulk writes via `outbox_suppressed`, adopt DT keys, delete the conflict row.

## Access & safety

- **DT MySQL is 1Password-gated** → the real `--apply` runs operator-led from Joel's machine (DT creds + prod DB firewall). Agents/CI never connect; tests mock the DT reader.
- Dry-run is the default; `--apply` is explicit. The temporary `--allow-unsafe-merge` gate is **removed**.
- Enforced invariants in code: skip-open-match, most-restrictive consent, ambiguous→review-only, DT-key adoption + conflict-row deletion.
- Bulk writes go through `outbox_suppressed` (single `bulk_imported` summary), consistent with the ETL.

## Idempotency

Re-running `--apply` is a no-op: merged contacts are now `source_system='dt'` and no longer surface as conflicts; their conflict rows are deleted; field overwrites are deterministic; child upserts use ON CONFLICT. A partial failure rolls back per contact (single transaction each), so a re-run is clean.

## Components / file structure

- `apps/etl/src/jp_adopt_etl/reconcile/track_a_duplicate_email.py` — rewrite the merge path; keep the existing dry-run/diagnostics + review-list scaffolding; remove the `--allow-unsafe-merge` gate.
- `apps/etl/src/jp_adopt_etl/reconcile/track_a_merge.py` (new, if the above grows too large) — the pure merge-rule logic (field overwrite, consent most-restrictive, interest union, classification), separated from I/O so it is unit-testable in isolation.
- `apps/etl/src/jp_adopt_etl/dt_source.py` — the gated single-contact reader (`fetch_contact`) already added in #142; reuse.
- Tests under `apps/etl/tests/`.

## Test plan

**Unit (pure, no DB):** classification (clean / skip-match / ambiguous / other); name-mismatch heuristic; consent most-restrictive; descriptive field overwrite (DT-wins-when-present); interest union; DT-key adoption.

**Integration (local Postgres, mocked DT reader):**
- clean merge end-to-end: DT overwrites descriptive + status, children merged (activity appended, interests unioned, assignment replaced), keys adopted (`source_system='dt'`, `source_id` set), conflict row deleted, single `bulk_imported` event.
- skip-open-match: a contact with an open `match` is untouched and appears in the review list.
- ambiguous: DT/core name mismatch → not merged → in review list.
- consent: a core opt-out is preserved despite DT consent.
- interest union: both DT and core interests present after merge.
- idempotent re-apply: a second `--apply` is a no-op.
- **durable resolution:** simulate the next sync by the DT `source_id` and assert no new `duplicate_email` conflict is recorded.

## Out of scope

- Tracks B (`assignee_no_subject`) and C (`fpg_not_found`) — shipped in #142.
- Changes to the orchestrator's conflict-detection hot path (the key-adoption approach avoids needing them).
- The actual production `--apply` run (operator-led, separate from this build).

## Open questions

None blocking. Defaults adopted during brainstorm: interests = union (not DT-replace); assignments = DT-replace.
