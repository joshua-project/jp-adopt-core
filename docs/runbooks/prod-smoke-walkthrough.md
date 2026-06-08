# Production smoke walkthrough

End-to-end check that **intake → match → accept → drip enroll → email** works on production. Run this before every release (Phase 1 kickoff: blocker 3).

Two ways to run:

- **Scripted:** `BEARER='<token>' scripts/smoke-prod.sh` — 10 checkpoints, ~30 seconds. Reusable. See script header for usage.
- **Manual:** the walkthrough below. Slower but better for first-time validation and for screen-recording an Amy training session.

The scripted and manual versions cover the same checkpoints. Pick scripted when you're verifying a deploy; pick manual when you're validating that the **UI** flows match what a real operator does.

---

## Prerequisites

1. **Bearer token** — grab from your prod browser session:
   - Sign into the prod staff app (production URL — see `docs/runbooks/deploy.md` for the current ACA FQDN while DNS catches up)
   - DevTools → Application → Session Storage → find the MSAL access-token entry → copy the `secret` field
2. **Active drip campaign** — at least one campaign must be in `status='active'` on prod. If none exists:
   - Visit `/campaigns` → "+ New campaign"
   - Add a step with a known template (e.g., the seeded `facilitator_welcome.mjml`) and `delay_days=0`
   - Click **Activate**
3. **An email address you control** — the smoke contact's email determines where the drip lands. Default in the script is `smoke+<uuid>@example.com` (won't deliver). Override with `SMOKE_EMAIL=you@yourdomain.com` to actually verify delivery.

---

## Manual walkthrough (10 checkpoints)

### 1. API up

Visit `<prod-url>/api/healthz` directly. Expect a JSON `{"status":"ok",...}`.

**Fail mode:** 5xx or HTML — API container is down or the ingress is misrouted.

### 2. DB reachable

Visit `<prod-url>/api/readyz`. Expect 200.

**Fail mode:** 503 — Postgres is unreachable from the API container. Check Azure Database for Postgres status and the API's connection string.

### 3. Auth + queue read

Visit `/matches` in the staff app. Expect the queue table to render (even if empty).

**Fail mode:** redirect to `/signin` or 401 — your bearer is stale or you don't have the role. If you can sign in but the queue is empty, that's fine — it just means no contacts are pending review.

### 4. Drip campaigns visible

Visit `/campaigns`. Expect at least one active campaign in the list with a green "active" badge.

**Fail mode:** no active campaigns — activate one before continuing (see Prerequisites #2).

### 5. Intake creates a contact

Visit `/contacts/new` and create a contact with:
- **Display name:** `Smoke Prod <timestamp>` (e.g., `Smoke Prod 2026-06-07 14:32`)
- **Email:** an inbox you control
- **Party kind:** Adopter
- **Country:** any
- **FPG selection:** any one FPG

Expect to land on the contact detail page with the new contact's profile.

**Fail mode:** form error or 5xx — check the API logs for the intake handler.

### 6. Matching runs

On the contact detail page, click **Run match** (or POST `/v1/matches/run/{contact_id}` directly). Expect a "matches found" toast or visible match candidates.

**Fail mode:** 0 matches and no candidates — the FPG you picked has no facilitator coverage. Either pick a covered FPG or activate a facilitator for that FPG first.

### 7. Match appears in queue

Visit `/matches`. Expect your smoke contact at or near the top of the queue.

**Fail mode:** smoke contact not in the queue — check that the match was created (`GET /v1/contacts/{id}/matches`); if it exists but isn't surfacing, the queue filter may be wrong.

### 8. Manual drip enrollment

On the contact detail page (or `/contacts/{id}`), open the **Drip enrollments** tile and click **Manual enroll**. Pick the active campaign from the dropdown and confirm.

Expect the tile to refresh and show the new enrollment with `state="active"`.

**Fail mode:** inline error in the modal — most likely the contact is already enrolled, on the suppression list, or in `do_not_engage`. The error surface is the same as the API's `ManualEnrollResponse.reason`.

### 9. Enrollment + events visible

On the same tile, click the disclosure triangle on the new enrollment row. Expect at least one event eventually (`step_sent` after the worker tick).

If you don't see events immediately, wait ~1 minute and refresh — the worker tick is once per minute, and the first send fires only after `delay_days` has elapsed (which is 0 for our test step).

**Fail mode:** no events after 5 minutes — check the worker logs (Azure Container Apps → worker → log stream) for ACS send errors. Common causes: ACS credentials misconfigured, template missing, suppression list hit.

### 10. Soft-retire the smoke contact

On the contact detail page, click **Transition** and pick state `do_not_engage`. Add a reason note (e.g., "prod smoke retirement").

This stops the contact from appearing in real matching and prevents further drip sends.

**Why this matters:** the smoke contact stays in the database but doesn't pollute Amy's queue. The smoke contacts naming convention (`Smoke Prod <timestamp>`) makes them grep-able if you ever want to hard-delete them via a DB session.

### Manual verify: email lands in inbox

Check the inbox of the email you used in step 5. Expect the drip email subject from your active campaign's step 0 within ~2 minutes after step 8 enrollment.

**If the email never arrives:**
- Worker tick may be paused — check the worker container's recent logs
- ACS credentials may be missing — check the API env vars
- The address may be on the suppression list — check `/admin/suppression`

---

## What "pass" looks like

| Checkpoint | Pass signal |
|---|---|
| 1 | `/healthz` returns 200 + `{"status":"ok"}` |
| 2 | `/readyz` returns 200 |
| 3 | `/matches` page renders for the signed-in admin |
| 4 | At least one active campaign visible on `/campaigns` |
| 5 | New contact created with the smoke name |
| 6 | Match endpoint returns 200 with ≥1 candidate |
| 7 | Smoke contact visible in the match queue |
| 8 | Manual enroll modal succeeds (reason=`created`) |
| 9 | Enrollment recorded; events list populated within ~2 min |
| 10 | Contact's adopter status flipped to `do_not_engage` |
| Manual | Drip email delivered to inbox |

If all 10 pass and the email arrives, prod is healthy end-to-end.

---

## Cleanup

The smoke contact stays in production by design — transitioning to `do_not_engage` is a "logical delete" that exits it from future matching and drip enrollment. If you want to hard-delete:

```sql
-- via psql on the prod DB
DELETE FROM contacts WHERE display_name LIKE 'Smoke Prod %';
```

Active enrollments tied to those contacts will cascade per the existing foreign key behavior; outbox rows referencing them will become orphaned but harmless (the worker idempotently skips them).

---

## See also

- `scripts/smoke-prod.sh` — the scripted equivalent
- `scripts/smoke-local.sh` — the dev-stack version with full DB assertions
- `docs/runbooks/deploy.md` — release process; this runbook is a post-deploy gate
- `docs/runbooks/drip-engine.md` — drip worker internals
