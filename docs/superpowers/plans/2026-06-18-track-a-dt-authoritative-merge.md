# Track A DT-Authoritative Merge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Track A's diagnostics-only/backfill merge with the approved DT-authoritative `duplicate_email` merge: DT overwrites, with skip-open-match / ambiguous-review / most-restrictive-consent carve-outs and durable DT-key resolution.

**Architecture:** Rewrite the write path of `apps/etl/src/jp_adopt_etl/reconcile/track_a_duplicate_email.py`. Extract the pure merge-decision rules into a new sibling `track_a_merge.py` so they unit-test without a DB. The planning/apply/orchestration stays in `track_a_duplicate_email.py`. Remove the `--allow-unsafe-merge` gate. The DT reader stays injectable (mocked in tests); the real `--apply` is operator-run with DT MySQL.

**Tech Stack:** Python, SQLAlchemy 2.0 (sync `Session`), Postgres `ON CONFLICT`, pytest (+ local Postgres on 127.0.0.1:5434), `uv`.

**Spec:** `docs/superpowers/specs/2026-06-18-track-a-dt-authoritative-merge-design.md`. Read it first.

**Read before starting:**
- The existing module: `apps/etl/src/jp_adopt_etl/reconcile/track_a_duplicate_email.py` (this plan modifies it).
- Models: `apps/api/src/jp_adopt_api/models.py` — `Contact` (cols incl. `source_system`/`source_id`/`adopter_status`/`facilitator_status`), `ContactProfile`, `AdopterInterest` (unique `(source_system, source_id)`), `Consent`, `ContactAssignment`, `Match`, `MigrationConflict`, `ActivityLog`, `IdentityLink`.
- `apps/api/src/jp_adopt_api/domain/state_machine.py` — match/status enums to define "open match".
- Mappers: `apps/etl/src/jp_adopt_etl/mappers/contacts.py`, `.../interests.py`, `.../status.py` — what `map_contact` yields and how interests/status map from DT.
- Existing tests: `apps/etl/tests/test_track_a_*` (extend these).

Run all tests from `apps/etl`: `uv run --extra dev pytest <files> -q`. Don't commit DT credentials. No production writes — tests use the injected/mocked DT reader.

---

## File structure

- **New:** `apps/etl/src/jp_adopt_etl/reconcile/track_a_merge.py` — pure rules: descriptive-field merge (DT-wins-when-present), consent most-restrictive, interest union, "open match" predicate input shaping. No SQLAlchemy session use; operates on plain values/dicts so it unit-tests in isolation.
- **Modify:** `apps/etl/src/jp_adopt_etl/reconcile/track_a_duplicate_email.py` — `plan_merges` (add open-match + use pure rules), the `_apply_*` functions (DT-overwrite, child merge, durable keys), `reconcile`/`run`/`main` (drop the gate), module docstring.
- **Modify/extend:** `apps/etl/tests/test_track_a_duplicate_email_mapper.py` (unit), `apps/etl/tests/test_track_a_reconcile_integration.py` (integration), plus new `apps/etl/tests/test_track_a_merge.py` (pure-rule unit).

---

## Task 1: Pure merge-rule module

**Files:** Create `apps/etl/src/jp_adopt_etl/reconcile/track_a_merge.py`; Test `apps/etl/tests/test_track_a_merge.py`.

- [ ] **Step 1: Write failing tests**

```python
from jp_adopt_etl.reconcile.track_a_merge import (
    merge_descriptive, consent_most_restrictive, interests_to_add,
)

def test_dt_overwrites_nonempty_core_value():
    out = merge_descriptive(core={"phone": "111", "origin": "forms"},
                            dt={"phone": "222", "origin": None})
    assert out == {"phone": "222"}            # DT wins where DT has a value

def test_keeps_core_where_dt_empty():
    out = merge_descriptive(core={"country_code": "US"},
                            dt={"country_code": None})
    assert out == {}                          # nothing to change

def test_consent_optout_in_core_wins():
    # core opted out of 'email' => DT 'email' consent must NOT re-enable it
    decision = consent_most_restrictive(core_optouts={"email"}, dt_optouts=set())
    assert "email" in decision.effective_optouts
    assert decision.dt_consents_to_add == set()   # blocked by core opt-out

def test_consent_dt_optout_propagates():
    decision = consent_most_restrictive(core_optouts=set(), dt_optouts={"email"})
    assert "email" in decision.effective_optouts

def test_interests_union_adds_only_missing():
    add = interests_to_add(core_keys={"pid-1"}, dt_keys={"pid-1", "pid-2"})
    assert add == {"pid-2"}
```

- [ ] **Step 2: Run, verify fail** — `uv run --extra dev pytest tests/test_track_a_merge.py -q` → ImportError.

- [ ] **Step 3: Implement `track_a_merge.py`**

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

def merge_descriptive(core: dict[str, Any], dt: dict[str, Any]) -> dict[str, Any]:
    """DT-authoritative field merge: return only the columns to UPDATE.
    DT wins where it has a non-empty value; columns where DT is empty are
    omitted (core value is kept)."""
    changes: dict[str, Any] = {}
    for col, dt_val in dt.items():
        if dt_val in (None, ""):
            continue
        if core.get(col) != dt_val:
            changes[col] = dt_val
    return changes

@dataclass
class ConsentDecision:
    effective_optouts: set[str] = field(default_factory=set)
    dt_consents_to_add: set[str] = field(default_factory=set)

def consent_most_restrictive(*, core_optouts: set[str], dt_optouts: set[str]) -> ConsentDecision:
    """Most-restrictive wins: the union of opt-outs is the effective set, and
    DT consent may only be ADDED for types not opted out in either system."""
    effective = set(core_optouts) | set(dt_optouts)
    return ConsentDecision(effective_optouts=effective, dt_consents_to_add=set())

def interests_to_add(*, core_keys: set[str], dt_keys: set[str]) -> set[str]:
    """Union semantics: DT interests not already on the core contact."""
    return set(dt_keys) - set(core_keys)
```

- [ ] **Step 4: Run, verify pass.** **Step 5: Commit** `feat(etl): pure DT-authoritative merge rules for track A`.

*Note:* `consent_most_restrictive` returns `dt_consents_to_add=set()` for now (additive consent import is out of scope — the safety requirement is to never WEAKEN; the apply step only ensures core opt-outs are preserved). If the build surfaces that DT consents should be additively imported, expand here with a test first.

---

## Task 2: Open-match pre-check + classification

**Files:** Modify `track_a_duplicate_email.py` (`plan_merges`, `MergePlan`); Test `test_track_a_reconcile_integration.py`.

Add a new disposition `skip_open_match`. Before the ambiguity check, query for an **open** `Match` on the merge-target contact; if present, set `status='skip_open_match'`, record the reason, and append (it shows up in the review list). "Open match" = a `Match` row referencing the target whose status is in the open set — confirm the exact values from `Match`/`state_machine.py`; use the not-yet-terminal statuses (e.g. `recommended`, `accepted`), NOT `declined`/`active`/`inactive`.

- [ ] **Step 1: Failing integration test**

```python
def test_contact_with_open_match_is_skipped(pg_session, mock_dt):
    # seed: target contact + a duplicate_email conflict + an OPEN match on target
    ...
    result = reconcile(pg_session=pg_session, mysql_conn=None, dt_reader=mock_dt)
    plans = {p.source_id: p for p in result.planned}
    assert plans["278"].status == "skip_open_match"
    assert plans["278"] in result.to_review or plans["278"] in result.skipped
```

- [ ] **Step 2: Run, verify fail** (currently merges or reviews, not skip_open_match).

- [ ] **Step 3: Implement** — in `plan_merges`, after resolving `target` and before the ambiguity gate:

```python
open_match = pg_session.execute(
    select(Match.id).where(
        Match.adopter_contact_id == target.id,   # confirm FK column name in models.py
        Match.status.in_(OPEN_MATCH_STATUSES),
    )
).first()
if open_match is not None:
    plan.status = "skip_open_match"
    plan.reason = "contact has an open match in core; left for Amy"
    result.planned.append(plan)
    continue
```
Define `OPEN_MATCH_STATUSES` as a module constant. Add `skip_open_match` to `ReconcileResult.skipped`/review surfacing and to `write_review_list` so Amy sees these (include a `disposition` column). Confirm the `Match`→contact FK column name against `models.py` before writing.

- [ ] **Step 4: Run, verify pass.** **Step 5: Commit** `feat(etl): skip contacts with an open core match in track A`.

---

## Task 3: DT-authoritative field + status overwrite

**Files:** Modify `track_a_duplicate_email.py` (`plan_merges` backfill section, `_apply_backfill`→`_apply_overwrite`, `_BACKFILLABLE_FIELDS`).

Replace empty-only backfill with DT-overwrite using `merge_descriptive` (Task 1). Expand the field set to the DT-authoritative descriptive columns AND `adopter_status`/`facilitator_status` (the open-match pre-check already excluded anything live). Keep `email_normalized` out (handled by key adoption + identity link).

- [ ] **Step 1: Failing integration test**

```python
def test_dt_overwrites_nonempty_status_and_fields(pg_session, mock_dt):
    # target has phone='OLD', adopter_status='new'; DT says phone='NEW', status='contacted'
    ...
    reconcile(pg_session=..., mode="production", ...)  # no gate now (Task 6) — until then pass allow flag
    target = pg_session.get(Contact, target_id)
    assert target.phone == "NEW"
    assert target.adopter_status == "contacted"
```

- [ ] **Step 2: Run, verify fail** (current backfill keeps 'OLD'/'new').

- [ ] **Step 3: Implement** — change the planning loop to compute `plan.field_changes = merge_descriptive(core=<target cols>, dt=dt_kwargs)` over:

```python
_MERGE_FIELDS = (
    "display_name", "phone", "origin", "country_code",
    "adopter_status", "facilitator_status",
)
```
Rename `MergePlan.backfill` → `field_changes` (update all refs). Replace `_apply_backfill` with `_apply_overwrite` that issues a single `UPDATE contacts SET <field_changes> WHERE id = target_id` (no `IS NULL` guard — DT is authoritative). Keep status writes here (direct), consistent with the ETL importer; the open-match guard is the safety net.

- [ ] **Step 4: Run, verify pass.** **Step 5: Commit** `feat(etl): DT-authoritative field+status overwrite in track A`.

---

## Task 4: Child-table merge (profile, interests, consent, assignment, activity)

**Files:** Modify `track_a_duplicate_email.py` (`_apply_one` + new `_merge_children`); Test `test_track_a_reconcile_integration.py`.

Per the spec table: **ContactProfile** DT-overwrite (upsert onto target by `contact_id`); **AdopterInterest** union (add DT interests whose key isn't on the target, via `interests_to_add`); **Consent** most-restrictive (never clear/contradict a core opt-out — use `consent_most_restrictive`); **ContactAssignment** DT-replace (the existing `_repoint_history_and_assignment` re-points the dt_import assignment — extend to DT-authoritative replace); **ActivityLog** append (keep existing re-point).

- [ ] **Step 1: Failing tests** (one per child)

```python
def test_interests_unioned(pg_session, mock_dt): ...   # core keeps pid-1, DT adds pid-2 => both present
def test_profile_overwritten_from_dt(pg_session, mock_dt): ...  # DT entity_size wins
def test_core_consent_optout_preserved(pg_session, mock_dt): ...  # core 'email' opt-out still effective post-merge
def test_dt_assignment_replaces(pg_session, mock_dt): ...
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement `_merge_children(pg_session, plan, dt_kwargs, dt_interests)`** called from `_apply_one`. Use `pg_insert(...).on_conflict_do_update` keyed on each child's unique constraint (ContactProfile `uq_contact_profile_contact_id`; AdopterInterest `uq_adopter_interest_source_system_source_id`). For consent, read the target's existing opt-outs, compute `consent_most_restrictive`, and ONLY write rows that do not weaken an existing opt-out (in practice: do not import a DT consent for a type the core contact opted out of). Confirm the consent/opt-out representation in `models.py` (Consent rows vs any suppression table) and shape the read accordingly.

- [ ] **Step 4: Run, verify pass.** **Step 5: Commit** `feat(etl): merge DT child tables (profile/interests/consent/assignment) in track A`.

---

## Task 5: Durable resolution via DT-key adoption

**Files:** Modify `track_a_duplicate_email.py` (`_apply_one`, new `_adopt_dt_keys`); Test `test_track_a_reconcile_integration.py`.

On merge, set the target contact's `source_system='dt'` and `source_id=<conflict.source_id>` so the next sync resolves it by `(source_system, source_id)` (update path) and never re-collides. Then delete the conflict row.

- [ ] **Step 1: Failing test (the durable one)**

```python
def test_durable_resolution_no_reconflict(pg_session, mock_dt):
    reconcile(..., mode="production", ...)
    target = pg_session.get(Contact, target_id)
    assert target.source_system == "dt"
    assert target.source_id == "278"
    # simulate the orchestrator's collision check: a DT contact source_id=278
    # now matches an existing (source_system,source_id) row => update path, not
    # a duplicate_email. Assert no NEW conflict would be recorded for 278.
    existing = pg_session.execute(select(Contact).where(
        Contact.source_system=="dt", Contact.source_id=="278")).scalars().all()
    assert len(existing) == 1
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement `_adopt_dt_keys`** — `UPDATE contacts SET source_system='dt', source_id=:sid WHERE id=:target_id`. Guard against a `(source_system, source_id)` collision with the DT loser row: if a separate loser Contact already holds `('dt', source_id)`, delete/retire the loser first (it was the stub with `email_normalized=NULL`) so the target can adopt the keys without violating `uq` — handle in this task and test it. Call `_adopt_dt_keys` before `_resolve_conflict` in `_apply_one`.

- [ ] **Step 4: Run, verify pass.** **Step 5: Commit** `feat(etl): durable DT-key adoption resolves track A conflicts`.

---

## Task 6: Remove the `--allow-unsafe-merge` gate

**Files:** Modify `track_a_duplicate_email.py` (`reconcile`, `run`, `main`, `_parse_args`, module docstring); Test `test_track_a_reconcile_integration.py`.

The merge is now designed; drop the placeholder gate.

- [ ] **Step 1: Update the gate test** — replace `test_apply_without_override_is_gated` with `test_apply_runs_without_override`:

```python
def test_apply_runs_without_override(pg_session, mock_dt):
    result = reconcile(pg_session=pg_session, mysql_conn=None,
                       dt_reader=mock_dt, mode="production")  # no allow flag
    assert result.to_merge  # merges happened, no RuntimeError
```

- [ ] **Step 2: Run, verify fail** (still raises the gate RuntimeError).

- [ ] **Step 3: Implement** — remove the `allow_unsafe_merge` param from `reconcile`/`run`/`main`, the `if mode == "production" and not allow_unsafe_merge: raise` block, and the `--allow-unsafe-merge` CLI arg. Rewrite the module docstring: drop the `STATUS: DIAGNOSTICS-ONLY` block; document the DT-authoritative behavior, the three carve-outs, and durable key adoption.

- [ ] **Step 4: Run, verify pass.** **Step 5: Commit** `feat(etl): lift track A merge gate — DT-authoritative merge is live`.

---

## Task 7: Full integration sweep + regression

**Files:** `test_track_a_reconcile_integration.py`; verify `test_orchestrator_integration.py` unaffected.

- [ ] **Step 1** Ensure the integration suite covers, end-to-end against local Postgres with the mocked DT reader: clean merge (fields+status overwritten, profile overwritten, interests unioned, assignment replaced, activity appended, DT keys adopted, conflict deleted, single `bulk_imported` event); skip-open-match; ambiguous→review (not merged); core consent opt-out preserved; idempotent re-apply (second `--apply` = no-op); durable resolution (Task 5 test).
- [ ] **Step 2** Run the whole track-A + regression set:

```bash
cd apps/etl && uv run --extra dev pytest \
  tests/test_track_a_merge.py tests/test_track_a_duplicate_email_mapper.py \
  tests/test_track_a_reconcile_integration.py tests/test_orchestrator_integration.py -q
```
Expected: all pass.

- [ ] **Step 3** `uv run ruff check apps/etl/src/jp_adopt_etl/reconcile/` → clean.

- [ ] **Step 4: Commit** `test(etl): full DT-authoritative track A integration sweep`.

---

## Self-review notes (author)

- **Spec coverage:** authority model → Tasks 3+4; skip-open-match → Task 2; ambiguous→review → already exists (kept); consent most-restrictive → Tasks 1+4; durable resolution → Task 5; gate removal → Task 6; dry-run/apply/review-list flow → unchanged scaffolding, swept in Task 7.
- **Ambiguity to resolve during build (verify against `models.py`, don't guess):** the exact `Match`→contact FK column + open-match status set (Task 2); the consent/opt-out representation (Task 4); the loser-row key collision on adoption (Task 5).
- **Out of scope:** orchestrator changes; the live prod `--apply` run.
