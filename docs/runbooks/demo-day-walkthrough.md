# Demo-day walkthrough — 2026-06-19 (Amy session)

A targeted script for tomorrow's production-readiness session with
Amy. Distinct from [`amy-walkthrough.md`](./amy-walkthrough.md), which
is her first-week reference; this doc is the **one-time gate** that
declares Amy can do her job in core and DT can be retired on
schedule.

For the underlying detail on any workflow below, jump to
[`amy-walkthrough.md`](./amy-walkthrough.md).

---

## Production-state snapshot (verified 2026-06-18 ~04:35 UTC)

Read once before the call so nothing surprises you.

| Item | State |
|---|---|
| Alembic head | `0028` |
| `staff_profile` rows | 2 — Joel + Amy, `digest_opt_in=true`, `status=active` |
| Daily digest pipeline | Reads from `staff_profile` (post-#138) |
| DT cron sync | Hourly, **`0 errors` across 1,095 historical runs**; most recent execution `2026-06-18T04:00 UTC` succeeded |
| `contacts` total | 446 (248 `dt`, 197 forms, 1 `staff_seed`) |
| `adopter_interest` | 6,744 rows |
| `match` (status='recommended') | **1** — there is a match in Amy's queue waiting for her to triage |
| `facilitating_org` | 3 (one is the triage org) |
| `campaign` | 3, all `draft`. None will fire until activated. |
| `migration_conflicts` | 466 — 245 `assignee_no_subject`, 209 `duplicate_email`, 12 `fpg_not_found`. Informational; not blocking. |

The DT sync is producing real, complete, hourly-fresh data. The 248
`source_system='dt'` contacts already in core means the demo is
**against actual production data**, not a seeded sandbox.

---

## Pre-flight (15 min before the call)

Run these from Joel's machine. None of these is optional.

```bash
# 1. Confirm prod API + web are healthy.
API_FQDN=$(az containerapp show \
  --name jp-adopt-api \
  --resource-group rg-jp-adopt-core-production \
  --query "properties.configuration.ingress.fqdn" -o tsv)
curl -fsS "https://${API_FQDN}/healthz"
curl -fsS "https://${API_FQDN}/readyz"
# Expect 200 from both; sha matches the latest main commit.

# 2. Smoke the deploy end-to-end.
BEARER='<paste-fresh-token>' \
  API_URL=https://${API_FQDN} \
  SMOKE_EMAIL=joel@joelbcastillo.com \
  scripts/smoke-prod.sh
# Expect all 12 checks pass.

# 3. Spot-check the ETL cron's last hour.
az containerapp job execution list \
  --name jp-adopt-etl-cron-production \
  --resource-group rg-jp-adopt-core-production \
  --query '[0].{name:name,status:properties.status,start:properties.startTime,end:properties.endTime}' \
  -o table
# Expect Status=Succeeded, started_at within the last hour.
```

If any of the above fails, **stop and fix before starting the demo**.
Don't show Amy a broken surface — file the failure and reschedule.

---

## Demo agenda (45 minutes, in order)

### 0. Setup (2 min)

- Joel shares the staff app URL:
  `https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io`
- Amy opens it in Chrome / Edge (NOT Safari for the demo — fewer
  one-off MSAL quirks).
- Both sign in via Entra with `@joshuaproject.net` / `@globalspecifics.com`.

### 1. Tour the nav (5 min)

Walk Amy through the top nav at a high level — what each link is for.
Reference [`amy-walkthrough.md` § "What's where"](./amy-walkthrough.md#whats-where)
verbatim. No clicking yet.

### 2. The match queue (10 min) ⭐ headline workflow

This is the daily job. There is **one match in `recommended` status
right now** — perfect for a live demo.

1. Open **Matches**.
2. Show Amy the row. Click into the review page.
3. Walk through the review UI — adopter info, recommended org,
   scored alternates, the override picker.
4. **Do not accept the match during the demo** unless Amy and Joel
   agree it's the right call. If accepting, follow
   [`amy-walkthrough.md` Workflow 1](./amy-walkthrough.md#workflow-1--triage-todays-matches-5-minutesday).
5. If sending back, the reason field is optional — explain that.

What to point out:
- The recommendation comes from real DT data (the adopter is one of
  the 248 imported contacts; the FPG selections are theirs).
- Scoring is per-FPG coverage + capacity. Amy will see this dozens of
  times a day; this is the heart of her job.

### 3. Browse contacts (5 min)

- Open **Adopters**. Show that the list paginates, filters, and is
  searchable. 401 adopters; mostly DT-imported.
- Open a couple of contacts. Show profile + interests + activity
  timeline. The activity log carries DT-imported notes for old
  contacts, which is the point of the migration's fidelity.
- Open **Facilitators**. Same idea — 44 rows.

### 4. Orgs (5 min)

- Open **Orgs**. Three orgs, one is the triage org.
- Click into one. Show capacity + FPG coverage tabs.
- **Don't create a new org during the demo.** Reference Workflow 2
  in [`amy-walkthrough.md`](./amy-walkthrough.md#workflow-2--add-a-facilitator-org-rare-weekly)
  for what that flow looks like.

### 5. Drip campaigns — preview only (10 min)

- Open **Campaigns**. Three campaigns, all `draft`.
- Click into "Adopter sign-up welcome." Show the steps.
- **Preview every step.** This is critical — Amy needs to read the
  email bodies before she activates anything. The preview shows the
  rendered MJML in the same shell recipients will see.
- **Do not activate any campaign during the demo.** Activation is
  one-way and starts sending real email. The activation decision is
  Amy's, made after she has confidence in the content. Park it as a
  post-demo todo.

### 6. (Optional, time permitting) Suppression (3 min)

- Open `/admin/suppression`. Empty list.
- Show the add-address form. Explain that suppression is hash-keyed —
  the raw email is never written to the DB.
- Don't add anyone during the demo.

### 7. Wrap-up + post-demo todos (5 min)

What Amy walks away ready to do:
- Triage tomorrow's match (and every day after).
- Activate campaigns once she's comfortable with the previewed
  content.
- Add new facilitator orgs as they sign up.

What Joel commits to before next sync:
- Fix any P0/P1 surfaced in the session.
- Address any matching-algorithm anomaly Amy flagged on the queue.
- Land DNS rebind (Gate B step 3) so the URL becomes
  `adoption.joshuaproject.net`.

---

## What you'll be watching during the demo

| Signal | Reaction |
|---|---|
| Page takes > 2s to render | Note the page, take a screenshot, move on. Investigate post-demo via `gh run list --workflow Deploy` + ACA latency dashboard. |
| Specific error message in the UI | Paste the exact text into a note. The error messages are intentionally specific — Joel can trace from the string. |
| MSAL sign-in fails for Amy | Most likely a redirect-URI mismatch. Verify `https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io/auth/callback` is on the SPA app reg. See [`multi-idp-b2c.md`](./multi-idp-b2c.md). |
| Match preview renders but accept fails | Most likely an optimistic-lock collision (`Contact.version` mismatch). Reload + retry; if it persists, it's a bug. |
| Browser console shows network errors | Watch network tab. If `/v1/*` calls return 401, the bearer expired — Amy needs to sign in again. If 5xx, screenshot the response + the request ID header. |

---

## Acceptance criteria — does Amy clear Gate A?

Exit Gate A from the [cutover master plan](../cutover-master-plan.md)
when **all** of these are true after the session:

- [ ] Zero P0 / P1 bugs surfaced and unresolved.
- [ ] Amy can triage a match end-to-end (accept, reassign, or send
      back) without operator help.
- [ ] Amy has previewed every step of at least one drip campaign and
      knows what activation will do.
- [ ] At least one real outside-email form submission has landed
      cleanly through the bridge in the last 7 days.
- [ ] Amy says she is confident she can do this Monday morning on her
      own.

If any of those misses, **don't claim Gate A is closed** — schedule
the fix and a follow-up session.

---

## See also

- [`amy-walkthrough.md`](./amy-walkthrough.md) — the reference for any
  workflow above.
- [`user-testing-walkthrough.md`](./user-testing-walkthrough.md) —
  what we walked on the dev stack before this session.
- [`prod-smoke-walkthrough.md`](./prod-smoke-walkthrough.md) — the
  pre-flight smoke detail.
- [`operator-handbook.md`](./operator-handbook.md) — broader ops
  policies and admin tasks.
- [`cutover-master-plan.md`](../cutover-master-plan.md) — the gate
  this session closes.
