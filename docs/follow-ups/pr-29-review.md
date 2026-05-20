# PR #29 review follow-ups

PR #29 (`feat(api): Amy-return build — foundation through matching (U1–U6)`) shipped 27 fix commits across 5 fixer passes after 5 review passes. The items below are real findings that were intentionally deferred — they need design decisions, separate scope, or are pre-existing concerns surfaced during review but not regressions.

**Review artifacts** (full per-reviewer detail):
- First pass: `/tmp/compound-engineering/ce-code-review/20260517-005219-1b96bc2f/`
- Second pass: `/tmp/compound-engineering/ce-code-review/20260518-142300-86c6a934/`
- Third pass: `/tmp/compound-engineering/ce-code-review/20260518-161256-73e5b67f/`
- Fourth-pass sanity: `/tmp/compound-engineering/ce-code-review/20260518-202335-c076e824/`
- Fifth-pass sanity: `/tmp/compound-engineering/ce-code-review/20260518-222717-6e21e399/`

These paths are local to Joel's laptop and ephemeral — copy artifacts elsewhere if needed for long-term reference.

---

## Tier 1 — Plan-deferred (lands in later units)

These are explicitly deferred by the build plan; not regressions, just not in this PR's scope.

### #40 — `POST /v1/matches/run/{contact_id}` HTTP endpoint
- **Plan reference:** U7 (staff match queue UI)
- **What's missing:** `match_or_route()` exists as a Python function. No HTTP surface to trigger it. The runbook documents a Python-script workaround.
- **Where it goes:** U7 — add the route alongside `GET /v1/matches/queue` + `POST /v1/matches/{id}/decide`.

### N4 — Magic-link email enqueue: BackgroundTasks → ARQ / Outbox
- **Source:** second-pass review (adv2-002), reliability sanity (R-B5-related)
- **What's wrong:** FastAPI `BackgroundTasks` is in-process. Process death between `db.commit()` and the BackgroundTask firing → token persisted, no email sent, user sees 202 with no signal.
- **Suggested approach** (documented in TODO at `apps/api/src/jp_adopt_api/routers/auth_magic_link.py`):
  - Write an `Outbox` row with `event_type='magic_link.send_requested'` in the same txn as the `magic_link_token` insert.
  - Worker drain consumes Outbox events and calls `ARQ pool.enqueue_job('send_magic_link_email', ...)`.
  - This reuses the existing transactional-outbox primitive — atomicity + retries + dead-letter for free.
- **Prerequisites:** add `arq` as an API dependency, lifespan-managed Redis pool in `apps/api/src/jp_adopt_api/deps.py`, test fixtures, `request_magic_link` signature change.
- **Scope:** ~4 files. Real change, not a fixer change.

---

## Tier 2 — Test-build passes (scope: one PR each)

Real testing gaps that warrant their own focused PR.

### #13 — DB-backed transition coverage (11 + 3 transitions untested)
- **Source:** first-pass testing (T-01), unchanged across passes
- **What's wrong:** `state_machine.py` defines 14 adopter + 4 facilitator transitions. The current test suite exercises 3 of each. Plan called for "every transition."
- **Untested adopter edges:** DRAFT→NEW, NEW→POTENTIAL_ADOPTER, POTENTIAL_ADOPTER→CONTACTED, CONTACTED→ENGAGED, ENGAGED→MATCHED, NEW→MATCHED (Amy shortcut), POTENTIAL_ADOPTER→MATCHED (Amy shortcut), CONTACTED→MATCHED (Amy shortcut), SENT_BACK→MATCHED (re-match), MATCHED→ACTIVE (acceptance), ACTIVE→INACTIVE.
- **Untested facilitator edges:** DRAFT→NEW, NEW→NOT_READY, NOT_READY→READY, any→DO_NOT_ENGAGE.
- **Scope:** ~20 new tests in `test_state_machine.py`. Each test instantiates a Contact at the from-state and calls `transition_adopter` / `transition_facilitator`, asserting state change + audit row + outbox event.

### #28 — Alembic downgrade test harness
- **Source:** first-pass testing (T-03), data-migrations (TG-002)
- **What's wrong:** All 5 new migrations (0003, 0004, 0005, 0006, 0007) ship `downgrade()` functions. No test invokes them. A mis-ordered drop in a downgrade would only surface during an emergency rollback in production.
- **Scope:** Test harness that runs `alembic downgrade base` and `alembic upgrade head` round-trip; add a CI job that runs it. Could live in `apps/api/tests/test_migration_roundtrip.py`.

### #29 — Concurrent-acceptance integration test
- **Source:** first-pass testing (T-04), unchanged
- **What's wrong:** F2 added a savepoint guard for the `uq_match_open_per_interest` race. No test exercises two concurrent `match_or_route` calls for the same interest end-to-end.
- **Now feasible:** The savepoint pattern means the test can confidently assert (a) only one Match row exists, (b) the loser's MatchAttempt rows still persist, (c) the loser's outcome reports `reason='concurrent_conflict_unrecoverable'`.
- **Scope:** ~30 lines in `test_match_algorithm.py`.

---

## Tier 3 — Design decisions (need Amy / Joel input)

### AC-06 — Unified error envelope across `/v1/`
- **Source:** first-pass api-contract (AC-06)
- **What's wrong:** Three different error-envelope shapes coexist in `/v1/`:
  - Intake: `{apiVersion, ok: false, error: {code, message, fields, requestId}}`
  - Magic-link: FastAPI `HTTPException` serialized as `{detail: {code, message}}`
  - Contacts: FastAPI `HTTPException` serialized as `{detail: 'string'}`
- **Decision needed:** Pick one. Intake's shape is the most complete. Apply consistently across all `/v1/` routes, OR formally document the divergence in a `docs/runbooks/api-error-conventions.md`.
- **Scope:** ~6 routers, ~30 LOC + tests.

### AC-09 — camelCase vs snake_case
- **Source:** first-pass api-contract (AC-09)
- **What's wrong:** `IntakeSuccessData` emits camelCase (`submissionId`, `contactId`) via `serialization_alias`. `ContactRead` emits snake_case (`party_kind`, `display_name`). Same `/v1/` namespace, same future clients.
- **Decision needed:** Pick one. jp-adopt-forms mandates camelCase on intake. Apply `serialization_alias` to `ContactRead` fields too, OR document the divergence as permanent.

### AC-07 / AC-08 partial — OpenAPI codegen tooling
- **Source:** api-contract second pass
- **What's wrong:** Runtime `token_type='Bearer'` is `Literal['Bearer']` in Python, but the Literal does NOT propagate to OpenAPI / TS contracts (still typed as `string`). Similarly the magic-link 410 entry conflates `expired` + `already_claimed` under one undiscriminated response.
- **Decision needed:** Either add an `openapi-typescript` transform / post-process step that promotes `Literal` types, or document the contract as runtime-only.

### AC-12 — ContactRead expansion non-additive
- **Source:** api-contract second pass
- **What's wrong:** F21 expanded `ContactRead` from 7 to 13 required fields. Non-additive schema change.
- **Decision:** Already accepted as greenfield (no current consumers). Document in API CHANGELOG if/when one is started.

---

## Tier 4 — Defense-in-depth + edge cases (small fixes, follow-up PR)

### R-B4-1 / adv2-011 — Purge tasks can hold worker past graceful shutdown
- **Source:** fourth-pass sanity (R-B4-1)
- **What's wrong:** `purge_magic_link_rate_limits` + `purge_idempotency_keys` loop with 100ms sleep per 1000-row batch. After a multi-day cron outage accumulating millions of rows, a purge could take 100+ seconds. No explicit `job_timeout` set on the ARQ cron job.
- **Fix:** Set `job_timeout=600` (or similar) on the cron registration; document the worst-case duration.

### R-B2-1 — 1-hour stuck-pending threshold underdocumented
- **Source:** fourth-pass sanity (R-B2-1)
- **What's wrong:** B2 added `state='pending' AND created_at < now() - 1 hour` to the purge predicate. 1 hour is appropriate for HTTP-latency-bounded intake handlers — but the assumption isn't documented in code.
- **Fix:** Add a comment in `worker_settings.py` explaining the threshold.

### R-C3-1 — Purge SQL string drift between worker and API test
- **Source:** fourth-pass sanity (R-C3-1)
- **What's wrong:** C3 test duplicates the worker's purge SQL string verbatim. If the worker SQL is edited, the test passes against its own stale copy. String drift undetected.
- **Fix:** Either install `arq` as an API dev dependency so the worker function can be imported, OR add a string-equality assertion test between the two.

### adv2-005 — `purge_idempotency_keys` can delete cached rows mid-replay at 24h boundary
- **Source:** second-pass adversarial (adv2-005)
- **What's wrong:** A client retrying near the 24h expiry boundary races the purge. If purge wins, cached response is gone and handler reprocesses → double side-effects (duplicate AdopterInterest, duplicate outbox events).
- **Fix:** Extend retention to 48h, OR document as acceptable race (intake idempotency is best-effort, not contract-guaranteed). Currently documented as accepted in B3.

### adv5-001 — ARQ worker crash mid-flight could duplicate `permanent_failure` logs
- **Source:** fifth-pass adversarial (adv5-001)
- **What's wrong:** If a worker crashes mid-flight on the final retry, ARQ may re-run the job after recovery → another `permanent_failure` log fires for the same logical send.
- **Severity:** P4. Log noise only — no duplicate emails (ACS-side idempotency would handle that separately).
- **Fix:** Defer; revisit when log-volume alerting starts firing.

### adv5-003 — Pre-savepoint flush adds N round-trips
- **Source:** fifth-pass adversarial (adv5-003)
- **What's wrong:** F2 added `await session.flush()` before each savepoint. For a contact with 5 interests, that's 5 extra DB round-trips per `match_or_route` call.
- **Severity:** P3 / informational. Sub-millisecond per flush with asyncpg + Postgres. Not a current concern.
- **Fix:** Defer until perf testing shows it matters.

### adv5-004 — ACS validator may diverge from Azure SDK on edge cases
- **Source:** fifth-pass adversarial (adv5-004)
- **What's wrong:** Our F5 parser handles `key=value` pairs, but Azure's SDK may parse duplicate keys, newlines, or quoted values differently. If our validator accepts a string but the SDK rejects it (or vice versa), the production guard is misaligned with runtime.
- **Fix:** Test our validator against the actual Azure SDK's parser; document any divergence.

### adv5-005 — `sys.path` mutation in `conftest.py` creates latent ordering hazard
- **Source:** fifth-pass adversarial (adv5-005)
- **What's wrong:** F1 injected the worker source dir into `sys.path` via `conftest.py` so worker tests can import. If another test (or future test) does its own `sys.path` mutation, ordering effects could surface.
- **Fix:** Move the worker package into a proper editable install (`uv pip install -e ../worker`), OR install `arq` as an API dev dependency so worker is importable normally.

### adv5-006 — `claim_link` catch-all may mask original exception if `db.rollback()` itself raises
- **Source:** fifth-pass adversarial (adv5-006)
- **What's wrong:** F4 added `except Exception: await db.rollback(); raise`. If `db.rollback()` itself raises (broken connection), Python replaces the original exception with the new one.
- **Severity:** P4. Very narrow window. Python's normal exception-chaining preserves the original via `__context__` for log inspection.
- **Fix:** Wrap rollback in try/except, OR rely on `__context__` chain for observability. Defer.

### CORR-3 — B5 silently swallows missing `ctx` keys
- **Source:** fourth-pass correctness
- **What's wrong:** F1 falls back silently if `ctx['job_try']` is missing (e.g., ARQ ever renames the key). Combined with the 202 anti-enumeration envelope, an operator gets zero signal.
- **Fix:** Log a warning when `job_try` is missing from ctx.

### CORR-5 — Purge has no max-iterations safety bound
- **Source:** fourth-pass correctness
- **What's wrong:** B4's batched purge breaks when `rc < batch_size`. Under sustained INSERT load (rows replenishing faster than purge drains), the loop could in theory run for very long.
- **Severity:** P3 / theoretical at current load.
- **Fix:** Add a `max_iterations=1000` safety bound, log a warning if hit.

### CORR-7 — Purge test pollution on re-run
- **Source:** fourth-pass correctness
- **What's wrong:** C3's tests seed rows with `api_key_id='test_purge'`. If a prior run failed before cleanup, leftover rows persist and `rowcount == 2` assertion fails on re-run.
- **Fix:** Add a pre-seed `DELETE` in the test fixture, OR scope `api_key_id` per-run with a UUID.

---

## Tier 5 — Pre-existing (not regressions, flagged for awareness)

### `/openapi.json` exposed unconditionally in production
- **What's wrong:** `/openapi.json`, `/docs`, `/redoc` are all reachable in production. PR #29 added `securitySchemes` to the spec — now reveals the auth model.
- **Severity:** P3 / reconnaissance signal. No secrets exposed.
- **Fix:** Gate spec endpoints on `not settings.is_production`, or behind a separate auth layer.

### Email PII in worker logs (3 sites)
- **What's wrong:** Worker tasks log recipient email at multiple sites (pre-existing pattern, B5 inherited).
- **Severity:** P3 / GDPR-adjacent.
- **Fix:** Hash recipient in logs, OR scope logs to a redacted level.

### Idempotency-Key contract shift undocumented
- **What's wrong:** B7 marked `Idempotency-Key` as `required: true` in OpenAPI. Server-side behavior was already to reject missing key with 400 `idempotency_required`. Generated clients that previously typed it as optional now get a different error code at runtime than the spec implies.
- **Severity:** P4 / documentation gap.
- **Fix:** Add to API CHANGELOG when one is started.

### RateLimitedError echoes user email in 429 detail
- **Source:** fourth-pass security (pre-existing)
- **What's wrong:** `RateLimitedError`'s `str()` includes the requesting email. Logged in 429 responses.
- **Severity:** P4. Not exploitable (caller controls input) but a PII trail.
- **Fix:** Sanitize the exception detail before exposing to clients.

---

## How to use this file

- Each item lists the source review + a suggested next action.
- Closing an item: remove it from this file in the same PR that closes it; reference this file's commit history for the audit trail.
- Adding new items from future review passes: append; don't reorder.
- This file deliberately mirrors the prioritization the review passes produced — Tier 1/2 are "next session" candidates; Tier 4/5 are "when we have a slow afternoon."
