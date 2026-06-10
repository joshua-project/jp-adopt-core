---
title: Postgres advisory-lock concurrency guard for ETL crons
date: 2026-06-10
module: apps/etl
problem_type: architecture_pattern
component: background_job
severity: medium
applies_when:
  - "A scheduled job (cron, ARQ periodic task, Container Apps Job) has a fire interval shorter than its worst-case runtime"
  - "Concurrent invocations would conflict on shared state (upserts, outbox rows, optimistic-locked rows)"
  - "Runs share a Postgres database AND a SQLAlchemy engine pool that may reuse TCP connections"
  - "Operators need to inspect lock state via `pg_locks.objid`"
related_components:
  - database
  - tooling
tags:
  - etl
  - postgres
  - advisory-lock
  - concurrency
  - cron
  - sqlalchemy
  - pg-locks
---

# Postgres advisory-lock concurrency guard for ETL crons

## Context

The hourly DT→Postgres ETL cron runs on an Azure Container Apps Job
firing at `:00` UTC. When an hour-N run overruns its window, the
scheduler will fire hour-N+1 anyway. Without a guard, two `run_etl()`
invocations race the same `INSERT … ON CONFLICT` workflow against the
same source watermark — at best wasted work, at worst conflicting
upsert cycles on partial unique indexes (e.g.
`uq_contacts_source_system_source_id`), version-column races on
optimistic-locked rows (`Contact.version`), and double-emission to the
outbox table for downstream webhooks.

The fix is a Postgres **session-scoped advisory lock** acquired on the
orchestrator's Postgres session. The hour-N+1 invocation calls
`pg_try_advisory_lock`, sees `false`, logs, and returns `{}` cleanly.
The scheduler retries on the next hour.

Implementation lives in `apps/etl/src/jp_adopt_etl/orchestrator.py`.
The shape generalizes to any cron with overlap risk, but the safe
version has several non-obvious requirements beyond "wrap the body in
`pg_try_advisory_lock`."

## Guidance

### 1. Derive the lock key from a stable seed at module load

Hand-typed int64 literals are a footgun: large numbers are
unverifiable by inspection, and typos look fine. (PR #123's first cut
had a hand-typed value that was off by ~422 trillion and silently
"worked" — the lock just didn't match the documented derivation,
which confused the post-merge correctness review.) Compute the key at
import time from a SHA-256 of a namespaced byte-seed and freeze with
`Final[int]`:

```python
import hashlib
from typing import Final

# Postgres advisory-lock key for serializing dt-etl runs (concurrency
# guard against hour-N runs that overrun into hour-N+1's schedule).
# Derived at module load from a namespaced seed so the value is
# self-verifying and any future operator can re-derive it. Stable across
# deploys (seed never changes); visible in `pg_locks.objid` for operator
# inspection. To reproduce: `int.from_bytes(hashlib.sha256(b"jp_adopt_dt_etl")
# .digest()[:8], "big", signed=True)`.
_ETL_ADVISORY_LOCK_KEY: Final[int] = int.from_bytes(
    hashlib.sha256(b"jp_adopt_dt_etl").digest()[:8], "big", signed=True
)
```

Properties to preserve:
- **Namespaced seed.** Different crons that share a database must not
  collide. Pick a unique identifier per cron.
- **First 8 bytes, signed.** Postgres advisory keys are `bigint`
  (signed 64-bit). `signed=True` matches.
- **`Final[int]`** communicates "do not re-derive at call sites."
- **Docstring includes the reproduction command** so a reviewer or
  operator can verify the value matches the seed without booting
  Python.

### 2. Pair `pg_try_advisory_lock` with an explicit release helper

Session-scoped advisory locks are released by Postgres when the
underlying TCP connection closes. In a SQLAlchemy app this is **not
enough**: `engine.dispose()` or session-close returns the connection
to the engine's pool — the TCP connection itself stays open and can
be picked up by the next session (next test, next run in the same
process). A lock acquired session-scoped on a pooled connection can
leak across runs that share the pool.

The defense is an explicit unlock helper invoked in `finally`,
structured so it cannot mask the caller's exception:

```python
def _release_etl_lock(session: Session) -> None:
    """Release the dt-etl advisory lock. Best-effort: logs and rolls back
    on failure so the caller's original exception (if any) still
    propagates. Idempotent — Postgres returns false from
    `pg_advisory_unlock` for an already-released key.
    """
    try:
        session.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": _ETL_ADVISORY_LOCK_KEY},
        )
        session.commit()
    except SQLAlchemyError:
        logger.warning(
            "dt-etl: advisory unlock failed; lock will release on "
            "connection close.",
            exc_info=True,
        )
        try:
            session.rollback()
        except SQLAlchemyError:
            logger.warning(
                "dt-etl: rollback after failed unlock also failed.",
                exc_info=True,
            )
```

Why each piece:
- **`try: … except SQLAlchemyError:`** around unlock+commit so an
  unlock failure during a *successful* run downgrades to a log line
  rather than replacing the success outcome with an exception.
- **`except SQLAlchemyError:` is narrow on purpose.** Anything that
  isn't a SQLAlchemy error (e.g. `KeyboardInterrupt`, `SystemExit`)
  should still bubble.
- **Rollback is itself guarded.** If the session is already aborted,
  rollback can fail too — log and continue rather than overwrite the
  caller's original error.
- **Idempotent.** `pg_advisory_unlock` returns `false` if the key
  isn't held; not an error.

### 3. `try/finally` placement: acquire-then-IMMEDIATELY-try

The single most common mistake: putting setup work between
`pg_try_advisory_lock` and the `try:`. If that setup raises, the lock
leaks (held until the session — or the pooled TCP connection —
closes).

Correct shape: the `if not locked: return {}` guard returns before
the `try:`; the `try:` block opens on the very next line so every
subsequent code path is covered by the `finally:
_release_etl_lock(...)`.

```python
async def _drive() -> dict[str, dict[str, int]]:
    results: dict[str, dict[str, int]] = {}
    try:
        with SessionLocal() as pg_session, mysql_engine.connect() as mysql_conn:
            locked = pg_session.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _ETL_ADVISORY_LOCK_KEY},
            ).scalar()
            if not locked:
                logger.warning(
                    "dt-etl: another run already holds the advisory lock; "
                    "exiting cleanly. The next scheduled hour will retry."
                )
                return {}
            # Lock acquired — finally below releases it on every exit
            # path (success or exception).
            try:
                capture = (
                    _capture_dry_run_pre_state(pg_session)
                    if mode == "dry_run"
                    else _DryRunCapture()
                )
                # ... all per-table imports, commits, dry-run replay ...
                pg_session.commit()
            finally:
                _release_etl_lock(pg_session)
    finally:
        mysql_engine.dispose()
        pg_engine.dispose()
    return results
```

Two `finally` levels are doing distinct jobs:

- **Inner `finally: _release_etl_lock(...)`** wraps every line that
  runs after the lock is acquired. `_capture_dry_run_pre_state` lives
  *inside* the inner `try:`, not before it. The bug shape to avoid:

  ```python
  # BUG SHAPE — DO NOT WRITE THIS
  if not locked:
      return {}
  capture = _capture_dry_run_pre_state(pg_session)  # ← if this raises, lock leaks
  try:
      ...
  finally:
      _release_etl_lock(pg_session)
  ```

- **Outer `finally: engine.dispose()`** wraps the entire `with
  SessionLocal()` block. Without it, a re-raised exception from inside
  the `with`-block exits the function via the exception path and the
  engines never get `dispose()`d, leaking the pool. Putting
  `dispose()` after the `with`-block (not in a `finally`) would only
  run on the success path.

### 4. Test the contention path against a real database

The point of the lock is failure-mode behavior. Unit tests with mocks
don't exercise Postgres's advisory-lock semantics — you need an
integration test that holds the lock on one connection and invokes
the function under test to verify it returns cleanly:

```python
def test_concurrent_run_yields_empty_dict(monkeypatch, pg_engine, pg_session):
    from jp_adopt_etl.orchestrator import _ETL_ADVISORY_LOCK_KEY, run_etl

    # Hold the lock on a SEPARATE connection that simulates a long
    # in-progress run. Acquired here, NOT released by the SUT.
    with pg_engine.connect() as held:
        held.execute(
            text("SELECT pg_advisory_lock(:k)"),
            {"k": _ETL_ADVISORY_LOCK_KEY},
        )
        try:
            mock = _MockedDtSource()
            _patch_dt_source(monkeypatch, mock)
            _open_engine_returns_pg(monkeypatch, pg_engine)

            result = run_etl(
                mysql_url="mysql+pymysql://ignored",
                postgres_url=ETL_TEST_DATABASE_URL,
                tables=["contacts"],
                mode="production",
                watermark=None,
            )
            assert result == {}
        finally:
            held.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": _ETL_ADVISORY_LOCK_KEY},
            )
            held.commit()
```

Critical: the held connection must be **separate** from the SUT's
session. Connection pools can hand the SUT the same connection that's
holding the lock; advisory locks are session-, not transaction-scoped,
and re-entrant in the same session — so the SUT would `pg_try_advisory_lock`
true on a connection that already holds the key and would NOT exercise
the contention path. The test's own `finally` must release the lock to
keep the test database usable across runs in the same pytest session.

### 5. Operator escape hatch

Session-scoped locks release on TCP connection close, but a hard
process kill plus a long TCP-keepalive timeout can strand the lock
for tens of minutes. Operators need a manual release path:

```sql
-- Identify holders
SELECT pid, locktype, objid
FROM pg_locks
WHERE locktype = 'advisory' AND objid = <key>;

-- Option A: terminate the holder; lock releases when its connection closes
SELECT pg_terminate_backend(<pid>);

-- Option B: force-unlock by running unlock as the holder
SELECT pg_advisory_unlock(<key>)
FROM pg_stat_activity
WHERE pid IN (
    SELECT pid FROM pg_locks
    WHERE locktype = 'advisory' AND objid = <key>
);
```

The long-term fix is configuring TCP keepalives on the Postgres
client connection so dead holders are evicted within seconds. Filed
as #125; the manual paths above are the bridge.

## Why This Matters

- **Duplicate work is the optimistic outcome.** The realistic outcome
  of two concurrent ETL runs is partial-unique-index conflicts on
  `ON CONFLICT` upserts, version-column races on optimistic-locked
  rows, and double-emission to the outbox for downstream webhooks.
- **Crash-safety is the reason to prefer advisory locks over a
  `running` status row.** A status-row pattern (`UPDATE etl_state SET
  running=true ...`) requires a recovery path when the holder crashes
  without clearing it. Session-scoped advisory locks release on
  connection death, no manual recovery required — except for the
  TCP-stranding case, which is bounded by keepalive config.
- **Connection pooling makes "rely on session close" insufficient.**
  This is the part most people miss. Pools reuse TCP connections;
  "session close" in SQLAlchemy is "return to pool," not "close the
  TCP socket." The explicit unlock is what makes the pattern correct
  across runs that share a process.
- **Placement of `try:` and `finally:` is load-bearing.** Lock-leak
  bugs from `try/finally` placement do not surface in tests that run
  one ETL invocation cleanly — they surface in production after a
  flaky network or a `KeyboardInterrupt` during initialization.

## When to Apply

Apply this pattern when **all** of:

- A scheduled job has a fire interval that can be shorter than its
  worst-case runtime.
- Concurrent invocations would conflict on shared state (upserts,
  outbox rows, optimistic-locked rows, external API calls with rate
  caps).
- Postgres is already in the dependency graph of the job (otherwise
  add the dependency only if you must — for non-Postgres jobs, prefer
  Redis SETNX or a real distributed lock).

Apply **only the lock-key derivation pattern** (sec. 1) even when the
rest doesn't apply, if you need any Postgres advisory lock at all.

Do **not** apply this when:

- The job is truly idempotent and concurrent-safe. The complexity
  isn't justified.
- You need fairness or queueing — advisory locks are
  first-acquired-wins; later contenders just bounce. Use a real queue.
- The job runs in multiple processes that don't share a Postgres
  database. Use Redis or a coordinator.

## Examples

### Generalized template for a new cron

Adapt by changing the seed identifier and the inner body. The shape
(key derivation, helper, double try/finally with the guard returning
before the inner `try:`) is what you preserve.

```python
import hashlib
from typing import Final
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# 1) Key derivation at module load
_DAILY_DIGEST_LOCK_KEY: Final[int] = int.from_bytes(
    hashlib.sha256(b"jp_adopt_daily_digest").digest()[:8], "big", signed=True
)

# 2) Release helper, error-swallowing so it can't replace caller exceptions
def _release_digest_lock(session: Session) -> None:
    try:
        session.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": _DAILY_DIGEST_LOCK_KEY},
        )
        session.commit()
    except SQLAlchemyError:
        logger.warning("daily-digest: advisory unlock failed", exc_info=True)
        try:
            session.rollback()
        except SQLAlchemyError:
            logger.warning(
                "daily-digest: rollback after failed unlock also failed",
                exc_info=True,
            )


def run_daily_digest() -> dict:
    engine = create_engine(POSTGRES_URL, future=True)
    SessionLocal = sessionmaker(engine, expire_on_commit=False)
    try:
        with SessionLocal() as session:
            # 3) Acquire-and-guard
            locked = session.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _DAILY_DIGEST_LOCK_KEY},
            ).scalar()
            if not locked:
                logger.warning("daily-digest: another run holds the lock; exiting")
                return {}
            # 4) try: IMMEDIATELY after the guard — nothing between guard and try
            try:
                return _do_digest_work(session)
            finally:
                _release_digest_lock(session)
    finally:
        # 5) Outer finally so dispose() runs on exception paths too
        engine.dispose()
```

### Before/after on the inner placement bug

```python
# BEFORE — lock leaks if _capture_pre_state raises
locked = session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": KEY}).scalar()
if not locked:
    return {}
capture = _capture_pre_state(session)   # ← raises here → lock held until conn close
try:
    do_work(session, capture)
finally:
    _release_lock(session)

# AFTER — every path between acquire and session-exit runs the finally
locked = session.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": KEY}).scalar()
if not locked:
    return {}
try:
    capture = _capture_pre_state(session)
    do_work(session, capture)
finally:
    _release_lock(session)
```

### Before/after on `engine.dispose()` placement

```python
# BEFORE — exception from inside the `with` skips dispose()
def run():
    engine = create_engine(URL)
    SessionLocal = sessionmaker(engine)
    with SessionLocal() as session:
        do_work(session)         # ← raises → engine leaks
    engine.dispose()             # never reached on exception path

# AFTER — outer try/finally guarantees dispose
def run():
    engine = create_engine(URL)
    SessionLocal = sessionmaker(engine)
    try:
        with SessionLocal() as session:
            do_work(session)
    finally:
        engine.dispose()
```

## Related

- `docs/solutions/architecture-patterns/batch-importer-over-async-intake-helpers-2026-05-29.md`
  — same `apps/etl` module, layered underneath this lock pattern.
  The batch importer documents the in-process transactional shape
  (SAVEPOINT + dedup set + batch commit); the advisory lock wraps
  that shape with run-level serialization. The two are complementary,
  not overlapping.
- `docs/solutions/integration-issues/pymysql-url-ssl-mode-query-param-2026-06-09.md`
  — same cron, same deploy.yml surface. A first-fire failure mode
  worth pattern-matching against when introducing any new
  `DATABASE_URL` to the cron (see #127 for the role-split pre-flight
  follow-up).
- `docs/runbooks/dt-cron-sync.md` — operational runbook for the
  hourly cron this lock guards.
- `docs/runbooks/etl-postgres-role-split.md` — least-privilege role
  runbook landed alongside this pattern in PR #123.
- PR #123 — original implementation and review thread.
- Issue #124 — `?running=true` filter on `/v1/admin/etl-runs` to
  expose lock state to agents.
- Issue #125 — TCP keepalive config to close the SIGKILL stale-lock
  window.
- Issue #126 — tests for the advisory-lock failure paths (unlock
  failure, capture failure).
