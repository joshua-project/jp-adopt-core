# Operator handbook

Day-to-day guide for the staff user running Joshua Project Adoption.
Written for Amy + her team — assumes you've signed in via Azure B2C
and reached the staff app.

---

## The five things you'll do most

### 1. Review today's matches

Go to **Matches** in the nav. Each row is a recommendation:

- **Contact** — the adopter's display name
- **rop3** — the FPG (Frontier People Group) code they selected
- **Facilitator** — the org the algorithm picked
- **Top score** — the weighted-sum score (0.000–1.000); higher is better

Click **review →** to see the full breakdown.

### 2. Accept, send back, or route a recommendation

On the match-review page you'll see:

- **Primary recommendation** (highlighted in green) — the rank-1 candidate
- **Alternates** — rank 2 and 3, each with their own score breakdown

Three actions:

| Action | When to use |
|--------|-------------|
| **Accept** | The match looks right. Capacity is reserved on the facilitator org; the contact moves to `matched` status. The facilitator gets notified via the next 9am ET digest. |
| **Send back** | The match is wrong (facilitator is too busy, geography mismatch, language gap). **You must pick a reason code.** The system records the rejection so the next match-run excludes this facilitator. |
| **Route to next alternate** | Pick a different facilitator without rejecting the contact. Click **Pick** on an alternate. |

### 3. Walk a contact through the workflow manually

Sometimes you need to transition a contact outside the match queue —
mark someone `do_not_engage`, fast-forward a draft contact to `new`,
etc.

Go to **/workflow/<contact-id>** (or click "review →" on a non-match
contact in your contacts list). Pick the kind (adopter/facilitator),
the target state, optionally a reason code + notes, and click
"Apply transition."

If the transition isn't legal from the current state, the API returns
409 with `illegal_transition` and the form shows the message.

### 4. Add a contact manually

For phone walk-ins, event referrals, or anyone who didn't come through
the public form. Go to **Add contact** in the nav (or `/contacts/new`):

- **Display name + email** — required.
- **Party kind** — adopter or facilitator. Most manual creates are
  adopters.
- **Origin** — defaults to `manual_entry`. Override if the contact
  came from a specific source (event, partner referral).
- **FPG rop3 codes** — comma-separated. Leave blank if the contact
  hasn't picked an FPG yet; they'll land as `potential_adopter` and
  appear in the triage queue.
- **Facilitator org** — set this if you're pre-matching the contact
  with a specific org. The form creates a Match row in `recommended`
  status; it'll appear in the queue immediately.

### 5. Read the daily digest

At 9am Eastern every day, you'll get an email titled "JP Adoption —
\<date\> digest" listing every match recommended or accepted in the
last 24 hours. Facilitators receive a separate per-org digest with
only their matches.

If you didn't get a digest you expected, check:
1. Was there activity in the last 24h? `SELECT COUNT(*) FROM match
   WHERE recommended_at > now() - interval '24 hours' AND status IN
   ('recommended', 'accepted');`
2. Is your Contact row's `email_normalized` populated?
3. Did the worker run? Check `digest_run` for today's `window_start`.

---

## Cadence

What to look at and when. Items marked **(Amy)** are UI-only;
**(Operator)** items need `az` / `psql` / 1Password access.

### Every weekday morning (Amy, ~5 min)

- [ ] Open the **Matches** page. Triage anything in the recommended /
  triage queue.
- [ ] Skim the 9am digest email. Any matches that should have been
  auto-routed but landed in triage?
- [ ] Skim **Add contact** sources for the last 24h — any walk-ins or
  partner referrals you need to enrich?

### Weekly (Amy, ~15 min)

- [ ] Review contacts stuck in `recommended` > 7 days. Either accept,
  send back, or contact the facilitator.
- [ ] Review contacts stuck in `triage` > 7 days. Either match
  manually or mark `do_not_engage`.
- [ ] Walk through the past week's `do_not_engage` transitions. Any
  trends (same reason code repeating)?

### Weekly (Operator, ~10 min)

- [ ] Run the health checks below (`/healthz`, `/readyz`, outbox
  backlog, worker tick gap).
- [ ] Scan deploy history: `gh run list --workflow Deploy --limit 10`.
  Any failures or skipped jobs?
- [ ] Check Azure Postgres metrics: storage % used, connection count,
  CPU. The portal blade lives in `rg-jp-shared-production`.

### Monthly (Operator, ~30 min)

- [ ] Run the deploy smoke against production:
  `scripts/smoke-local.sh` against the prod FQDN (read-only checks).
- [ ] Verify the daily digest fired every weekday for the prior month
  (`SELECT window_start, status FROM digest_run WHERE status != 'sent'
  ORDER BY window_start DESC;`).
- [ ] Rotate the `WEBHOOK_HMAC_SECRET` if any downstream consumer
  changed.

### Quarterly (Operator, ~1 hr)

- [ ] **Backup-restore drill** per `docs/runbooks/postgres-backup-restore.md`.
  This is non-negotiable — a backup you've never restored is not a backup.
- [ ] Review `migration_conflicts` table for unresolved entries from
  any ETL runs that have happened since the last review.
- [ ] Re-confirm the production firewall allowlist — any IPs that
  should no longer be there?
- [ ] Update this handbook with anything you've learned that the next
  oncall would benefit from.

---

## When things go wrong

### "I sent a match back and now the contact has the same facilitator again"

The exclusion list filters by facilitator org, not the specific match
row. If `match_or_route` is rerun for a contact whose only matching
facilitator was the one you sent back, the algorithm has nowhere else
to go and re-recommends the same org. Two options:

1. **Override the matching algorithm** — manually create a Match row
   pointing at a different org (via the manual-create form, or by
   sending back again and explicitly picking a different alternate
   from `/v1/matches/run/<contact_id>` results).
2. **Add more facilitator coverage** — extend the
   `facilitator_fpg_coverage` table so more orgs cover the same FPG.
   Talk to the team about who can take on that FPG.

### "A facilitator is missing from the digest"

The facilitator's user needs three things to receive a digest:

1. A Contact row with `email_normalized` populated.
2. A `facilitator_org_membership` row connecting their B2C subject to
   the facilitating org.
3. Their org must have had at least one new match in the digest
   window.

Use `POST /v1/admin/facilitator-memberships` (or have a staff_admin
do it for you) to grant the membership.

### "The matches queue is empty but contacts are coming in"

Check that the matching algorithm ran. New contacts trigger
`match_or_route` via the intake → outbox → worker chain, but if the
worker is down or the outbox is backed up, recommendations don't land.

```sql
SELECT COUNT(*) FROM outbox WHERE processed_at IS NULL;
-- Should be small (0-10). If it's 100+, the worker is stuck.

SELECT COUNT(*) FROM contacts WHERE adopter_status = 'new' AND created_at > now() - interval '1 hour';
-- New contacts in the last hour
```

If the queue is genuinely empty, you can force a match run for a
specific contact:

```bash
curl -X POST https://<api-fqdn>/v1/matches/run/<contact-id> \
  -H "Authorization: Bearer <your-token>" \
  -H "Content-Type: application/json" \
  -d '{"force": true}'
```

### "A staff member's account doesn't work after cutover"

DT password hashes are intentionally not migrated (they're PHPass; we
can't use them). Every DT staff user gets a magic-link reset on their
first sign-in attempt:

1. They go to the staff app sign-in page.
2. They enter their email address.
3. The magic-link side-car sends them a 15-min sign-in link.
4. They click the link and they're in.

If they don't receive the email, check the worker logs for
`magic_link.email.permanent_failure` — the email may have been
suppressed or the ACS quota may be exhausted.

### "I made an edit but the next ETL ran clobbered it"

It shouldn't. Every contact you edit in the new system gets
`local_modified_after_import = true`, and the ETL's
`ON CONFLICT DO UPDATE WHERE local_modified_after_import = false`
skips your edits.

If you genuinely see overwrites, check:

```sql
SELECT id, display_name, local_modified_after_import, updated_at
FROM contacts
WHERE source_system = 'dt' AND id = '<id>';
```

If `local_modified_after_import` is `false` after your edit, there's a
bug in the PATCH path. File a ticket.

---

## Operator-only procedures

These sections need `az` CLI, `psql`, and 1Password access. Amy does
not run these; Joel (or whoever is operator-of-record) does.

### Health checks

Quick commands to verify the system is healthy. Run these as the first
thing during any incident triage.

```bash
# 1. API liveness + the deployed SHA
API_FQDN=$(az containerapp show \
  --name jp-adopt-api \
  --resource-group rg-jp-adopt-prod \
  --query "properties.configuration.ingress.fqdn" -o tsv)
curl -fsS "https://${API_FQDN}/healthz"
curl -fsS "https://${API_FQDN}/readyz"
# Expect both: {"status":"ok"|"ready","sha":"…"} — and the SHA matches
# the latest main commit you expected to be live.

# 2. Outbox backlog (worker draining?)
DB_URL=$(op item get "Adopt Core - Production" \
  --vault "JP Adopt Platform" \
  --account joshuaproject.1password.com \
  --fields database-url --reveal)
docker run --rm -i postgres:16-alpine psql "$DB_URL" -c "
  SELECT
    COUNT(*) FILTER (WHERE processed_at IS NULL)        AS pending,
    COUNT(*) FILTER (WHERE processed_at IS NOT NULL)    AS processed,
    MAX(emitted_at)                                      AS latest_emit,
    MAX(processed_at)                                    AS latest_drain,
    NOW() - MAX(processed_at)                            AS drain_lag
  FROM outbox;"
# pending: should be < 50. drain_lag: should be < 1 minute.

# 3. Drip + digest tick freshness
docker run --rm -i postgres:16-alpine psql "$DB_URL" -c "
  SELECT 'drip'   AS run, MAX(window_start) AS last_window FROM drip_run
  UNION ALL
  SELECT 'digest' AS run, MAX(window_start) FROM digest_run;"
# drip last window: should be within the last ~15 minutes.
# digest last window: should be today (if past 9am ET) or yesterday.

# 4. Recent deploys
gh run list --workflow Deploy --limit 5 \
  --json status,conclusion,createdAt,headSha --jq '.'
```

If any of the above is unhealthy, jump to the matching section in
`docs/runbooks/` — `drip-engine.md`, `daily-digest.md`, `deploy.md`.

### Admin tasks (no UI for these yet)

| Task | How |
|---|---|
| Mint a new intake API key | UI: `/admin/intake-keys` (#116). One-time plaintext shown on creation; record in 1Password immediately. |
| Rotate an existing intake API key | Mint a new key, hand it to the forms repo, deploy forms, then revoke the old key from `/admin/intake-keys`. |
| Grant `staff_admin` to a new user | Direct SQL: `INSERT INTO user_roles (user_b2c_subject_id, role_id) VALUES ('<sub>', '00000003-0000-0000-0000-000000000001') ON CONFLICT DO NOTHING;` |
| Add a facilitator org membership | UI: facilitator-org admin (#57) — pick org → Add member → enter B2C sub. |
| Add FPG coverage for an org | UI: facilitator-org admin → Coverage tab. |
| Suppress a contact from drips | Manual transition to `do_not_engage` via `/workflow/<id>`. Optional: add to `suppression_list` for hard email block. |
| Force a re-match for a contact | `curl -X POST https://${API_FQDN}/v1/matches/run/<contact-id> -H "Authorization: Bearer <token>" -d '{"force":true}'` |

### Production access patterns

Read-only access (the only kind you should default to):

```bash
# Get the runtime user URL (NOT the migrator URL — migrator has DDL):
DB_URL=$(op item get "Adopt Core - Production" \
  --vault "JP Adopt Platform" \
  --account joshuaproject.1password.com \
  --fields database-url --reveal)
# database-url is the migrator URL. For runtime read-only queries,
# substitute the user before piping into psql:
RUNTIME_URL=$(echo "$DB_URL" | sed 's|jp_adopt_migrator|jp_adopt|')
RUNTIME_PW=$(op item get "Adopt Core - Production" \
  --vault "JP Adopt Platform" \
  --account joshuaproject.1password.com \
  --fields runtime-password --reveal)
RUNTIME_URL=$(echo "$RUNTIME_URL" | sed "s|:[^@]*@|:${RUNTIME_PW}@|")
docker run --rm -i postgres:16-alpine psql "$RUNTIME_URL"
```

Firewall: production Postgres is not on the public allowlist by
default. Add your IP temporarily via:

```bash
MY_IP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create \
  --resource-group rg-jp-shared-production \
  --name jp-postgresql-production \
  --rule-name "operator-$(whoami)-$(date +%Y%m%d)" \
  --start-ip-address "$MY_IP" \
  --end-ip-address "$MY_IP"
```

**Remove the rule when done** — see `docs/runbooks/secret-rotation.md`
for the firewall cleanup pattern. Persistent operator rules are a
config-drift smell.

For SSH-into-container debugging (`az containerapp exec`), see
`docs/runbooks/deploy.md` § "Live debugging."

---

## Companion runbooks

Every operational topic has a dedicated runbook. The handbook above is
the index; the runbook is the depth.

| Runbook | When to open |
|---|---|
| `docs/runbooks/local-dev.md` | Setting up your machine; running the stack locally. |
| `docs/runbooks/quick-start.md` | First-week onboarding for a new operator. |
| `docs/runbooks/deploy.md` | Anything about the deploy pipeline; revision restarts; smoke tests. |
| `docs/runbooks/secret-rotation.md` | When any secret changes (DB password, ACS, magic-link key, intake key, OIDC creds). |
| `docs/runbooks/postgres-backup-restore.md` | Backup posture, restore drills, real-incident recovery. **Run a drill before any cutover-day work.** |
| `docs/runbooks/dt-cutover.md` | The DT → core data cutover sequence. |
| `docs/runbooks/dt-cron-sync.md` | Mirror-sync from forms; troubleshooting fpg-cache imports. |
| `docs/runbooks/forms-data-import.md` | Backfilling historical form submissions. |
| `docs/runbooks/drip-engine.md` | Authoring a drip campaign; debugging non-firing enrollments. |
| `docs/runbooks/daily-digest.md` | Why the 9am email didn't fire; replay procedure. |
| `docs/runbooks/matching-algorithm-v1.md` | How the score is computed; how to retune weights. |
| `docs/runbooks/multi-idp-b2c.md` | Adding or troubleshooting an identity provider. |
| `docs/runbooks/magic-link-side-car.md` | When B2C sign-in isn't practical; how the side-car works. |
| `docs/runbooks/dns-rebind.md` | Repointing `adoption.joshuaproject.net` between SWA and Container App. |
| `docs/runbooks/api-external-false.md` | Tightening the API ingress posture after the forms cutover. |
| `docs/runbooks/etl-postgres-role-split.md` | Why we have separate `jp_adopt` (runtime) and `jp_adopt_migrator` (DDL) roles. |
| `docs/runbooks/user-testing-walkthrough.md` | End-to-end smoke that exercises every staff workflow. |
| `docs/runbooks/prod-smoke-walkthrough.md` | Post-deploy production verification. |
| `docs/runbooks/amy-walkthrough.md` | Amy's first-week orientation script. |

If you're solving a problem and there's no runbook for it, write one
as you go. Past learnings live in `docs/solutions/` (organized by
category with frontmatter); the next operator will find it via grep.

---

## Glossary

- **Adopter** — someone who's signed up to support a people group.
- **Facilitator** — a partner organization (Frontier Alliance, etc.)
  that runs the on-the-ground adoption work.
- **FPG** — Frontier People Group. Identified by a 3-letter `rop3`
  code (e.g. `AAA01`).
- **Match** — a recommendation pairing an adopter's FPG interest with
  a facilitator org. States: `recommended` → `accepted` → `active`
  → (eventually) `completed`. Also: `sent_back`, `declined`, `triage`.
- **Triage** — adopters with no FPG selection land here; the
  triage_facilitator role member routes them by hand.
- **Drip** — automated email sequence triggered by a status change
  (e.g. facilitator-welcome after their first accepted match).
- **Outbox** — the transactional event log every state change writes
  to. The worker drains it and delivers webhooks + drives the drip
  engine + the daily digest.
- **B2C** — Azure Active Directory B2C, the identity provider you
  sign in through. Magic-link sign-ins are a side-car when B2C isn't
  practical (no Microsoft account, etc.).

---

## Who to call

- **Joel** — for code bugs, missing data, unexpected behavior.
- **Amy** — for product questions ("should this contact be
  facilitator A or B?").
- **JP IT** — for Azure tenant / DNS / B2C issues.

---

## Operational logs (manual append)

Tracked in this section: significant operational actions Joel + Amy
took outside the app. Append to the bottom; never reorder.

### YYYY-MM-DD — example entry

- **What:** Rotated `INTAKE_API_KEYS` via `docs/runbooks/secret-rotation.md`.
- **Why:** Old key compromised (accidentally pasted in a Slack DM).
- **Who:** Joel.
- **Verification:** New key works in jp-adopt-forms; old key rejected
  by `/v1/intake/adoption`.
