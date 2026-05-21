# Daily digest runbook (U11)

Sends a 9am ET roll-up of the last 24 hours of recommended/accepted
matches. Two recipient cohorts:
- **Staff** (`staff_admin` + `adoption_manager`): every match in the window.
- **Facilitators**: only their org's matches.

A facilitator who is also staff receives only the staff digest (no
duplicates).

## Architecture

```
Worker cron: send_daily_digest (every 10 minutes)
  │
  ├─ Gate 1: is current Eastern time in [09:00, 09:30)?  → no? return
  ├─ Gate 2: does digest_run already exist for today's
  │           window in status='sent' or 'empty'?  → yes? return
  │
  ▼
build_digest_for_window([yesterday 09:00 ET, today 09:00 ET))
  │
  ├─ Query: Match.recommended_at in window AND status in ('recommended','accepted')
  ├─ Group: staff → all matches; per-org facilitators → their org's matches
  │
  ▼
For each plan: render daily-digest.mjml, send via ACS, write digest_recipient row
```

## Send window

The cron registration triggers every 10 minutes. The task checks the
local Eastern hour internally and only fires the actual send when
`hour == 9 AND minute < 30`. This handles:
- Daylight Saving Time transitions (the helper computes EST/EDT from the date).
- Worker restarts mid-window (the digest_run idempotency check picks
  up the existing row).
- Multiple worker replicas (the digest_run insert + the recipient
  unique index converge on one row each).

## Configuring ACS

Same pattern as the drip engine (U10). Set on the worker via env:

```
ACS_CONNECTION_STRING="endpoint=https://...;accesskey=..."
ACS_SENDER_ADDRESS="no-reply@joshuaproject.net"
```

In dev (unset connection string), the worker logs the recipient + subject
and writes a `DigestRecipient.status='sent'` row anyway so the audit
trail looks the same as production.

## Authoring the template

`apps/api/email-templates/daily-digest.mjml` is the only template. It
receives:
- `recipient_kind` — `all_staff` / `adoption_manager` / `facilitator`
- `match_count` — int
- `matches` — list of dicts: `contact_display_name`, `rop3`,
  `facilitator_name`, `status`, `recommended_at` (ISO 8601 string)

Branching on `recipient_kind` lets you ship one template that reads as
"Today's matches across..." for staff and "Your org's matches today"
for facilitators.

## Manual replay

When you need to resend a specific day's digest (e.g. a missed delivery):

```sql
-- Find the run
SELECT id, window_start, status, recipient_count
FROM digest_run
ORDER BY window_start DESC
LIMIT 7;

-- Reset its status so the next worker tick recomputes
UPDATE digest_run SET status='pending', ended_at=NULL
WHERE id = '<run-uuid>';

-- Or delete the row entirely if you want a fresh recipient set
DELETE FROM digest_recipient WHERE digest_run_id = '<run-uuid>';
DELETE FROM digest_run WHERE id = '<run-uuid>';
```

The next 09:00-09:30 ET worker tick will pick up the missing run.

## Audit

```sql
-- Yesterday's recipients and counts
SELECT recipient_address, recipient_kind, match_count, status, sent_at
FROM digest_recipient dr
JOIN digest_run r ON r.id = dr.digest_run_id
WHERE r.window_start = (
  SELECT MAX(window_start) FROM digest_run
);

-- Failed sends in the last 7 days
SELECT recipient_address, error, sent_at
FROM digest_recipient
WHERE status = 'failed'
  AND created_at IS NULL  -- created_at is sent_at on failures; check the error column
ORDER BY digest_run_id DESC
LIMIT 20;
```

## Failure modes

| Symptom | Diagnosis | Recovery |
|---------|-----------|----------|
| No digest sent today | Worker not running OR no matches in window OR clock skew | Check worker logs for `digest.tick`; check `digest_run.status='empty'` |
| Same recipient gets two digests | Should be impossible — `uq_digest_recipient_run_address` partial unique index | File a bug |
| Template missing error in logs | `daily-digest.mjml` not on disk | Add the file; the next tick will pick it up |
| Wrong send hour after DST transition | The Eastern-hour helper miscalculated | Run `_eastern_now(datetime.now(UTC)).hour` in a Python REPL with `from jp_adopt_worker.tasks.send_daily_digest import _eastern_now`; file a bug if it disagrees with `date(1)` on the worker container |
| Facilitator missing from digest | No `facilitator_org_membership` row for that B2C subject, OR no Contact row exposing email | Add the membership via `POST /v1/admin/facilitator-memberships` and confirm the user has a Contact row with `email_normalized` set |

## Out of scope in v1

- Time-of-day customization per recipient (everyone gets 09:00 ET).
- Localization (English only).
- ACS volume warm-up (the plan caps initial daily volume at the
  current ACS new-domain quota; a ramp script lands in the deploy
  unit if needed).
- Per-recipient unsubscribe (the email body links to nothing; v2 adds
  one-click unsubscribe).

## Testing

```bash
cd apps/api && uv run --extra dev pytest tests/test_digest.py -q
```

6 tests cover: grouping (staff vs facilitator), empty-window early
return, window boundary exclusion, render with kind-based framing,
idempotent re-run, DST helper.
