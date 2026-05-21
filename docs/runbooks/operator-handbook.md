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
