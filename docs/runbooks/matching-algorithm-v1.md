# Matching algorithm — v1 runbook

**Audience:** Amy (operator), Joel (maintainer).
**Code:** `apps/api/src/jp_adopt_api/domain/matching.py`, `matching_config.py`.
**Plan:** `2026-05-15-001-feat-jp-adopt-core-amy-return-build-plan.md` (U6).

## What the algorithm does

For every `AdopterInterest` row attached to a contact, decide one of:

1. **Triage** — the interest goes into Amy's triage queue (a Match row in
   the configured triage facilitating-org with `status='triage'`). This
   happens when:
   - the interest has no `rop3` selected (Amy or the adopter asked for help
     picking an FPG), **OR**
   - the hard filter eliminated every candidate (no covering facilitator
     has capacity, or all candidates were previously excluded).

2. **Recommend** — score every passing candidate, write **one** `Match` row
   with `status='recommended'` pointing at the rank-1 candidate, and
   persist every candidate considered (pass or fail) as a `MatchAttempt`
   row for audit. Top-N alternates live in `match_attempt` with `rank=2/3`
   so the U7 UI can offer "route to a different facilitator" without a
   re-run. (U5's `uq_match_open_per_interest` partial index enforces "at
   most one open Match per interest" across recommended/accepted/active/
   triage; multiple `recommended` rows are not legal.)

Either way, the function flushes but does not commit. The caller (a worker
consuming `submission.received` outbox events, or a router handling a
`POST /v1/matches/run/{contact_id}` admin action) controls the txn boundary.

## Weights — current values + rationale

| Signal             | Weight | Rationale                                                  |
| ------------------ | -----: | ---------------------------------------------------------- |
| `capacity_headroom`|  0.40  | Amy's A5 emphasis on auto-routing reads as "who has room?" |
| `geography`        |  0.30  | Country match is the cheapest, most legible signal         |
| `language`         |  0.15  | Jaccard overlap of language code sets                      |
| `fpg_affinity`     |  0.10  | Binary in v1: covered or not (filtered before scoring)     |
| `theological`      |  0.05  | Stub for v1 — contact has no theological_tags yet          |
| **Total**          | **1.00** | Enforced at module import.                               |

These weights are inferred from Amy's wishlist language, not validated. The
matching weights live in **one place**: `matching_config.py`. To retune:

1. Edit `DEFAULT_WEIGHTS` in `apps/api/src/jp_adopt_api/domain/matching_config.py`.
2. Make sure the values still sum to 1.0 (the module asserts at import — a
   bad tuple will crash the API on next boot, not at request time).
3. Re-run matching against affected contacts via the admin re-match action
   (see "Re-match a contact" below) — historical Match rows are NOT mutated.
4. Commit the change with a one-line message naming the new vs old weights
   so `git log apps/api/src/jp_adopt_api/domain/matching_config.py` reads
   like an audit trail of tuning decisions.

## How to read a MatchAttempt row

Every candidate considered for an interest gets one row in `match_attempt`
regardless of whether they passed the hard filter. Useful columns:

| Column               | Meaning                                                  |
| -------------------- | -------------------------------------------------------- |
| `run_id`             | UUID grouping all attempts from one `match_or_route` call |
| `candidate_facilitator_id` | The facilitating org we scored / filtered            |
| `score`              | Weighted total, NUMERIC(4, 3). NULL when filter failed.   |
| `score_breakdown`    | JSONB with per-signal values                              |
| `filter_results`     | JSONB with `filter_reason`, capacity state, coverage      |
| `rank`               | Final position (1..N) if this attempt was promoted; NULL otherwise |

Common `filter_reason` values: `passed`, `inactive`, `no_capacity`,
`no_coverage`, `excluded_by_previous_send_back`.

## "Why didn't org X match for contact Y?"

```sql
SELECT
    ma.run_id,
    ma.created_at,
    fo.name AS facilitator_name,
    ma.score,
    ma.filter_results ->> 'filter_reason' AS filter_reason,
    ma.score_breakdown
FROM match_attempt ma
JOIN facilitating_org fo ON fo.id = ma.candidate_facilitator_id
WHERE ma.contact_id = $1
  AND fo.name ILIKE $2
ORDER BY ma.created_at DESC;
```

If the org appears in the result set, look at `filter_reason`:
- `passed`: they were scored; check `score` against the top-3 promoted.
- `excluded_by_previous_send_back`: a prior `match.status='sent_back'`
  added them to the exclusion list. Reverse by deleting / withdrawing the
  send-back match row, OR explicitly pass `exclude_facilitator_ids=frozenset()`
  to `match_or_route` on a re-run.
- `no_capacity`: `facilitator.capacity_committed >= capacity_total`.
- `no_coverage`: org isn't covering the contact's selected rop3.

If the org does NOT appear at all, they weren't loaded as a candidate. Most
likely: `facilitating_org.is_triage_org = TRUE` (triage orgs are excluded
from candidate scoring by design — they only receive triage assignments).

## Re-match a contact

This action is wired through `POST /v1/matches/run/{contact_id}` once U7
ships. Manual SQL fallback (e.g. for a one-off retry from psql):

```python
# from apps/api/scripts/, or an ipython session against the API venv
import asyncio, uuid
from sqlalchemy import select
from jp_adopt_api.db import get_session_factory
from jp_adopt_api.models import Contact
from jp_adopt_api.domain.matching import match_or_route

async def main():
    factory = get_session_factory()
    async with factory() as s:
        contact = (await s.execute(
            select(Contact).where(Contact.email_normalized == "jane@example.com")
        )).scalar_one()
        outcome = await match_or_route(s, contact, run_id=uuid.uuid4())
        await s.commit()
        print(outcome)

asyncio.run(main())
```

The function is idempotent in the sense that re-running produces a new
`run_id` and fresh MatchAttempt rows; existing Match rows (recommended,
accepted, etc.) are NOT touched. If you want to start from scratch,
withdraw the existing matches first (`UPDATE match SET status='withdrawn'
WHERE adopter_interest_id IN (...) AND status='recommended'`).

## Deviation from the plan: no-coverage routing

The plan calls for the no-coverage case (FPG selected but no covering
facilitator has capacity) to produce "0 Match rows; logs no_coverage with
the rop3; surfaces in Amy's UI as 'needs manual triage.'"

The implementation routes no-coverage interests to the **triage queue** as
a `Match(status='triage')` row instead — so Amy's UI has one consistent
view for everything that needs human routing. The `adopter_interest.rop3`
field distinguishes the cases: `NULL` means "adopter wants help selecting",
non-NULL means "FPG selected but no fit". Zero `match_attempt` rows for
the (contact, run_id, interest) tuple is also a tell.

If you'd rather have no Match row at all and surface no-coverage via a
separate query, change `_process_interest` to skip the triage insert in the
no-coverage branch and add a query / UI list for "interests with rop3 set
but no Match row in the current run."

## Tiebreaker semantics

When two candidates score equally, the tiebreaker is
`facilitating_org.last_assigned_at ASC` (oldest first, NULL sorts first).
This means a newly seeded facilitator with `last_assigned_at IS NULL`
always wins a tie against any previously-assigned facilitator.

This is deliberate — it surfaces fresh capacity. The downside: if Amy
seeds 10 facilitators with NULL `last_assigned_at` and ties happen, the
final tiebreaker is `str(facilitator.id)` (lexicographic UUID), which is
arbitrary but deterministic. Pick winners in a fairer round-robin by
running a manual `UPDATE facilitating_org SET last_assigned_at = now()`
on accepted matches (U8 already does this when it lands).

## Known limits / v2 candidates

- `theological` weight is 0.05 but the score function always returns 0
  because Contact has no `theological_tags` field. Either add the field and
  wire it up, or drop the weight and redistribute (favor capacity / geo).
- `geography` is country-exact only. Region/continent partial credit and
  time-zone proximity scoring are the obvious v2 refinements.
- The candidate pool query loads ALL facilitators in one shot. At >1000
  orgs this becomes wasteful; gate by country / language in SQL.
- Per-FPG capacity (an org can take 5 adopters total but only 1 per FPG)
  isn't modeled. Add a `facilitator_fpg_coverage.capacity_per_fpg` column
  when needed.
- `theological_concern` reason code exists in the state machine but the
  algorithm has no inverse penalty for orgs that previously sent back for
  that reason. Consider a "recent send-back same FPG/reason" multiplier.
