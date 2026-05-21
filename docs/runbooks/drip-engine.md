# Drip engine runbook (U10)

In-app drip campaigns + ARQ-scheduled send via Azure Communication
Services. Triggered by outbox events; idempotent; can be paused and
edited mid-flight without disturbing in-flight enrollments.

## Concepts

- **Campaign**: top-level marketing program. Status one of
  `draft|active|paused|archived`. `trigger_event_type` names an outbox
  event_type (e.g. `jp.adopt.v1.match.accepted_by_facilitator`); when
  the worker's drip drain sees that event for a contact, it enrolls
  the contact in this campaign.
- **CampaignStep**: ordered step inside a campaign. `delay_days` is the
  offset from the prior step (or enrollment for position 0).
  `mjml_template_name` references a file in `apps/api/email-templates/`.
  `send_at_hour` / `send_at_minute` define when in the day the step
  actually goes out (v1 uses UTC; per-contact timezone is a follow-up).
- **Enrollment**: per-(campaign, contact) state row. `campaign_version`
  is pinned at enroll-time so mid-flight edits don't change behavior
  for already-enrolled contacts.
- **EnrollmentEvent**: append-only log of `step_sent`, `send_failed`,
  `paused`, `resumed`, `exited`, etc.
- **SuppressionList**: emails the engine must never send to. Keyed by
  SHA-256 hash of the normalized email so the table holds no raw PII.

## Architecture

```
Outbox event with contact_id
        │
        ▼
drain_drip_enrollments (cron, every 10s offset)
  • SELECT FOR UPDATE SKIP LOCKED on outbox rows
    where drip_processed_at IS NULL
  • For each event:
    - if event_type='jp.adopt.v1.contact.do_not_engage':
        exit_enrollments_for_contact()
    - else: enroll_on_event() for every active campaign whose
        trigger_event_type matches
  • Stamp drip_processed_at

send_drip_step (cron, every 10s offset from drain)
  • claim_due_steps() — SELECT FOR UPDATE SKIP LOCKED on enrollment
    JOIN campaign_step where step is due
  • For each due step:
    - re-check do_not_engage / suppression / send window
    - render MJML template with Jinja2
    - send via ACS (dev fallback: log only)
    - log EnrollmentEvent
    - advance_enrollment (or mark completed)
```

Both drains are idempotent. The partial unique index on
`enrollment(campaign_id, contact_id)` WHERE state IN
(pending,active,paused) makes the enroll path race-safe; the FOR UPDATE
SKIP LOCKED on the send path prevents two workers from sending the
same step twice.

## Authoring a campaign (week 1)

There's no authoring UI in week 1. Campaigns are created via the API
(or a seed script):

```bash
# 1. Create the campaign
curl -X POST http://127.0.0.1:8000/v1/drips/campaigns \
  -H "Authorization: Bearer dev-local" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Facilitator welcome",
    "trigger_type": "event",
    "trigger_event_type": "jp.adopt.v1.match.accepted_by_facilitator",
    "auto_enroll_existing": false
  }'
# → returns {"id": "<campaign_id>", "status": "draft", ...}

# 2. Add steps (one POST per step)
curl -X POST http://127.0.0.1:8000/v1/drips/campaigns/<campaign_id>/steps \
  -H "Authorization: Bearer dev-local" \
  -H "Content-Type: application/json" \
  -d '{
    "position": 0,
    "delay_days": 0,
    "mjml_template_name": "facilitator-welcome.step-0.mjml",
    "subject": "Welcome to Joshua Project Adoption",
    "send_at_hour": 9
  }'

# 3. Activate
curl -X POST http://127.0.0.1:8000/v1/drips/campaigns/<campaign_id>/activate \
  -H "Authorization: Bearer dev-local"
```

The campaign now triggers automatically on every matching outbox
event. To pause: `POST .../pause`. To edit a step: `DELETE .../steps/0`
then re-`POST`.

## Adding a template

1. Drop a new `.mjml` file in `apps/api/email-templates/`.
2. Reference it from a `CampaignStep.mjml_template_name`.
3. The worker loads the file at send time. If the file is missing, the
   enrollment exits with `EnrollmentEvent(event_type='send_failed_template_missing')`.

Available Jinja2 variables in templates:
- `contact_display_name`
- `contact_email`
- `campaign_name`
- `step_position`

Extending the variable set is a one-line change in
`apps/api/src/jp_adopt_api/domain/drips.py`
(`render_step_html` context dict in the worker call site).

## ACS configuration

Set on the worker via env:

```
ACS_CONNECTION_STRING="endpoint=https://...;accesskey=..."
ACS_SENDER_ADDRESS="no-reply@joshuaproject.net"
```

In dev (unset connection string), the worker logs the recipient +
subject and treats the send as successful. This is intentional so the
state machine still advances during local development without an ACS
account.

## Pause / resume

```bash
# Pause an entire campaign (no new enrollments + no step sends for
# existing enrollments)
curl -X POST .../v1/drips/campaigns/<id>/pause -H "Authorization: ..."

# Resume by activating again
curl -X POST .../v1/drips/campaigns/<id>/activate -H "Authorization: ..."
```

There is no per-enrollment pause endpoint in week 1; the
`enrollment.state='paused'` field exists but no API surface mutates
it yet. Bounce-handler integration (which would set this to `paused`
for soft bounces) is deferred to a follow-up.

## Suppression

A row in `suppression_list` blocks future sends to that email forever
until removed manually. The hash form means you cannot look up an
email by browsing the table; pass the email through
`jp_adopt_api.domain.drips.email_hash()` (or run
`SELECT encode(sha256(lower('email@example.com')::bytea), 'hex')`) to
find a specific row.

Add reasons:
- `manual` — staff suppression
- `hard_bounce` — ACS Event Grid will populate this in U10's
  follow-up bounce handler
- `unsubscribed` — RFC 8058 List-Unsubscribe endpoint (TBD)
- `spam_complaint` — ACS feedback loop integration (TBD)

## do_not_engage handling

When a contact transitions to `do_not_engage`, the state machine emits
`jp.adopt.v1.contact.do_not_engage` via the outbox. The drip drain
recognizes this event type and exits every open enrollment for that
contact in one transaction.

## Failure modes

| Symptom | Diagnosis | Recovery |
|---------|-----------|----------|
| Enrollment row created but no step sent | Send window not yet open (send_at_hour > current UTC hour); ACS not configured (dev fallback returns no error) | Wait for the next tick; check logs for `drip.email.dev_fallback` |
| `EnrollmentEvent(event_type='send_failed_template_missing')` | Template file not on disk | Add the .mjml file to `apps/api/email-templates/`; the enrollment has already exited — re-enroll the contact manually if needed |
| Many `send_failed` events for the same enrollment | Transient ACS error | ARQ retries via the cron tick; if persistent, investigate ACS quota / DNS |
| Enrollment stuck in `state='active'` for hours | Send-at-hour gate never satisfied (e.g. set to 25 — but CHECK constraint blocks that) | Check the step's `send_at_hour` value; if intentional, increase the daily cron coverage |
| Duplicate enrollment for the same contact | Should be impossible thanks to `uq_enrollment_open_per_campaign_contact` | If it occurs, file a bug — the partial unique index is the contract |
| Campaign edited but old enrollments still using old behavior | Working as designed: `enrollment.campaign_version` pins to the version at enroll time | To force re-enrollment under the new version, exit the old enrollment manually and trigger the event again |

## Out of scope in v1

- Per-contact timezone resolution (v1 uses UTC for `send_at_hour`).
- Bounce handling (ACS Event Grid integration).
- One-click unsubscribe (RFC 8058 List-Unsubscribe header).
- Per-contact daily marketing cap (1 marketing email/day per the plan).
- Authoring UI (MJML preview, send-test).
- Auto-enroll-existing back-fill (the `auto_enroll_existing` flag is on
  the schema but no endpoint does the bulk back-fill yet).
- Pause/resume per enrollment (paused state exists; no API mutates it).

## Testing

```bash
cd apps/api && uv run --extra dev pytest tests/test_drips.py -q
```

19 tests cover suppression, enrollment idempotency, do_not_engage
exit, step-due query (delay_days respected, paused/exited rows
skipped), enrollment completion, full HTTP CRUD (create, activate,
pause, manual enroll), and the full worker tick (matched→enroll→
step-0 send→completed).
