# Amy's first-week walkthrough

A guided tour of the JP Adopt staff app for Amy as the first
production operator. Covers everything Amy needs to confidently
review matches, walk facilitator orgs through onboarding, send drip
campaigns, and curate the suppression list during week 1.

This doc complements `user-testing-walkthrough.md` (which targeted
the dev stack). The walkthrough below uses the **production URL**
and **real Entra sign-in**, not `dev-local`.

If a step breaks or doesn't look right, drop the URL and a screenshot
in Slack to Joel — that's the fastest path to a fix.

---

## Production URL

While `adoption.joshuaproject.net` DNS is propagating, use the
Azure Container App FQDN:

**https://jp-adopt-core-web-production.mangodesert-2647616f.centralus.azurecontainerapps.io**

Sign in with your `@joshuaproject.net` account. You already have
`staff_admin` granted, so every page is reachable.

---

## What's where

Top nav links you'll use most:

| Link | What for |
|---|---|
| **Matches** | The daily queue. Decide accept / reassign / send back for each new match. |
| **Adopters** | All people who've adopted an FPG. Search, edit, send email from notes. |
| **Facilitators** | All facilitator-side contacts (people, not orgs). |
| **Campaigns** | Drip email sequences (Adopter sign-up welcome, Facilitator approved, etc.). |
| **Add contact** | Manual intake for someone who emailed / called instead of using the public form. |
| **Admin** | User-role grants — give other staff access. Already gated to you. |
| **Orgs** | Facilitating-org list. Edit capacity, FPG coverage, deactivate. |

The **Suppression** page (`/admin/suppression`) isn't on the nav yet
but is reachable directly. You'll need it if someone says "stop
emailing me."

---

## Workflow 1 — Triage today's matches (5 minutes/day)

1. Open **Matches**.
2. Each row shows the adopter, the recommended facilitator, and a
   score. Click a row to open the review surface.
3. On the review page you'll see:
   - The adopter's selected FPG(s) + country
   - The recommended facilitator and a confidence score
   - "Scored alternates" — other orgs with coverage for this FPG
   - A **single facilitator picker** with three flavors of options:
     - The recommendation (default)
     - Scored alternates (marked "— alternate")
     - Any other assignable org (marked "— manual override")
4. Decide:
   - **Accept** — leaves the recommendation as-is and accepts in
     one click. The facilitator gets the match.
   - **Pick a different org** + Accept — reassigns and accepts in
     one move. Manual overrides are off-ledger (don't bump the
     org's capacity counter) and recorded as overrides for auditing.
   - **Send back** — returns the adopter to the queue. The reason
     field is optional and only prompted on send-back.

Expected: ~80% of matches should be one-click accepts. The override
path is for edge cases (FPG coverage gap, capacity tight on one org,
known partnership).

**If a match looks wrong** (e.g., adopter selected FPG `AAA01` but
recommendation is for an org with no `AAA01` coverage), screenshot
and ping Joel — that's a matching-algorithm bug.

---

## Workflow 2 — Add a facilitator org (rare; ~weekly)

When a new partner org signs up to facilitate FPG adoption:

1. Open **Orgs** → **+ New org**.
2. Fill in:
   - **Name** — the partner's org name
   - **Country code** — 2-letter ISO (US, IN, NG, …)
   - **Capacity total** — how many adopters they'll take on. Start
     conservative; you can raise later. 10 is a sane default if
     they haven't said.
3. After create, you land on the detail page. Add their **FPG
   coverage** (which people groups they can serve):
   - Click **+ Add FPG**, type the ROP3 code, **Add**.
   - One ROP3 per click. The system will 404 on a typo —
     case-insensitive but exact match.
4. The org won't receive matches until both:
   - `Active` (set automatically on create)
   - At least one FPG in coverage

**Don't touch:**
- `Committed` (the capacity counter — set by the matching algorithm)
- `Triage org` (only one allowed in the system; we already have one)

**If you need to add a person to a facilitator org** (so they can
sign in and see their match queue), use the Admin → user-roles UI
for now. A dedicated "add member" UI from the org detail page is on
the follow-up list.

---

## Workflow 3 — Activate a drip campaign (one-time setup)

Drips are email sequences that fire on events (adopter signed up,
facilitator approved). Three campaigns ship pre-loaded but in
**draft** state:

- **Adopter sign-up welcome** — 8 emails over 4 months
- **Facilitator sign-up welcome** — 1 email at sign-up
- **Facilitator post-approval sequence** — 5 emails over 2 months

To activate one:

1. Open **Campaigns**.
2. Click a campaign name → detail page.
3. Click **Preview** on a step to see exactly what recipients will
   see — branded shell, your subject, the body. **Preview every
   step the first time.**
4. If a step looks wrong, ping Joel — body content lives in the
   repo and needs a PR to change.
5. Once all steps look right, click **Activate** at the bottom.
6. The campaign immediately starts processing new sign-ups. Existing
   contacts don't get back-enrolled (intentional — `auto_enroll_existing`
   is false).

**To enroll a specific contact manually** (e.g., a facilitator you
added via Workflow 2 should get the welcome series): open their
contact page → **Manual enroll** → pick the campaign.

**To stop emailing someone:**
- If they reply "unsubscribe": add their email at `/admin/suppression`.
  Their hashed email goes on the suppression list; future drip steps
  skip them silently.
- If a whole campaign needs to stop: open it, click **Pause**.

---

## Workflow 4 — Send a personal email from a contact note

For ad-hoc messages outside drip campaigns (a personal welcome, a
specific follow-up):

1. Open the contact's page (from Adopters / Facilitators / Matches).
2. Scroll to the **Activity** tile.
3. Click **Send email**.
4. Modal opens with subject + body. For facilitators, you can also
   tick "Also send to secondary contact" to CC their backup person.
5. **Send** — delivery is via Azure Communication Services, same
   path as drip steps. The sent message lands in the contact's
   activity timeline as an `email` note for the record.

**If the contact has no email** (intake without one), the **Send
email** button is disabled. Add the email via the contact's profile
edit first.

---

## Workflow 5 — Suppression list

Anyone who replies "unsubscribe", bounces hard, or asks to stop:

1. Open `/admin/suppression`.
2. **Add address** form at the top: paste the email, **Add**.
3. The email is normalized (lowercased, trimmed) and hashed with
   SHA-256 before storage. The raw email is never written to the
   database.
4. To remove someone (they re-opted in): click **Remove** on their
   row.

The drip worker checks this list before every send. A suppressed
contact's drip silently terminates with `exit_reason=suppressed`.

---

## What's intentionally not in your hands (yet)

- **Editing email bodies** — the MJML lives in the repo. Tell Joel
  what to change.
- **Adding new drip campaigns** — you can create them via UI, but
  they need MJML templates that exist on disk. Coordinate with Joel.
- **Bulk import of orgs** — single-org create only in v1.
- **Per-org daily cap on matches** — v2. Capacity is per-org total,
  not rate-limited.

---

## If something breaks

Screenshot the URL + the error message. The error messages on this
app are intentionally specific (e.g., `campaign_has_active_enrollments:
Cannot deactivate — 3 open enrollment(s)`) — paste them verbatim
into Slack so Joel can trace.

Common ones that are not bugs:

| Message | What it means | What to do |
|---|---|---|
| `triage_org_exists` | Tried to create / mark a second triage org | Only one allowed; edit the existing one if needed |
| `capacity_below_committed` | Tried to set capacity_total lower than current committed | Decline some matches first; the algorithm won't release the counter on its own |
| `org_has_open_matches` | Tried to deactivate an org with live matches | Resolve / send back those matches first |
| `Graph user search isn't wired` | (Dev only) Microsoft Graph permission isn't granted | Doesn't appear in production |
| `Contact email is on the suppression list.` | Manual-enroll into a drip for someone on the suppression list | Remove from suppression first if they re-opted in |

---

## Daily / weekly rhythm

- **Daily (5 min):** triage the match queue (Workflow 1)
- **As-needed (~weekly):** add new orgs (Workflow 2), suppress
  unsubscribes (Workflow 5)
- **One-time:** activate campaigns (Workflow 3) once you've previewed
  every step

Joel runs the prod smoke (`scripts/smoke-prod.sh`) before every
release and watches for matching-algorithm regressions. You don't
need to babysit deploys.

---

## See also

- `docs/runbooks/operator-handbook.md` — broader operational policies
- `docs/runbooks/matching-algorithm-v1.md` — how matches are scored
- `docs/runbooks/drip-engine.md` — drip worker internals (background)
- `docs/runbooks/prod-smoke-walkthrough.md` — the deploy smoke (Joel's tool)
