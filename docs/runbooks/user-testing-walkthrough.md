# User testing walkthrough

A 10-minute guided click-through of the new staff app. Designed to be
shared with Amy and 1-2 test facilitators while production infra is
still being provisioned.

Replace `<DEV_URL>` below with whatever URL Joel sends you (will look
like `http://jbc-dev-mac.atlas-temperature.ts.net:3000` if you're on
the tailnet, or `http://localhost:3000` if you're testing on the dev
machine directly).

---

## Before you start

Confirm you can reach the home page:

> [`<DEV_URL>`](#)

You should see a "JP ADOPT" header with nav links: **Contacts**,
**Add contact**, **Matches**, **Facilitator**.

If the page doesn't load, ping Joel — most likely either the laptop
running the stack is offline, or you're not on the tailnet.

### Signing in

If you see a sign-in screen with a dev-token textbox:
- Paste `dev-local` and submit. You're in as a test staff user with
  full permissions.

If you see a Microsoft sign-in prompt:
- Use your normal JP staff account. You'll land on the home page.

If neither happens (no sign-in shown), B2C isn't wired up for this
test build — you're already signed in implicitly.

---

## 1. Review the match queue (~2 min)

> **Goal**: see what staff sees first thing in the morning.

1. Click **Matches** in the top nav.
2. You should see at least three rows — `Alice Adopter`,
   `Bob Adopter (multi-FPG)`, and `Carol Adopter (no FPG)`.
3. Each row shows: the contact name, the FPG (rop3 code) they signed
   up for, the recommended facilitator org, and a top match score.
4. Click **review →** on `Alice Adopter`.

You'll land on the match-review page. It shows:
- The primary recommendation in a highlighted card.
- The score breakdown (geography, language, capacity, etc.).
- Two alternate recommendations below.

### What to try

- **Accept** the recommendation. The contact moves to `matched`
  status; capacity is reserved on the facilitator org.
- **Send back** with a reason code. The contact returns to the queue
  with that facilitator excluded from future runs.
- **Pick** an alternate facilitator. Same as accept but on the
  rank-2 or rank-3 choice instead.

After accepting, go back to **Matches** — Alice should no longer be
in the pending queue.

---

## 2. Add a contact by hand (~2 min)

> **Goal**: test the "phone walk-in" / event referral flow.

1. Click **Add contact** in the top nav.
2. Fill in:
   - **Display name**: `Test Walk-in` (or whatever you like)
   - **Email**: `walkin+<your-initials>@example.com`
   - **Party kind**: `adopter`
   - **Origin**: leave as `manual_entry`
   - **Country code**: `US`
   - **FPG rop3 codes**: `AAA01` (or leave blank for triage flow)
3. Click **Create contact**.

You should see a green success banner with the new contact's UUID.

### Verify

Go to **Matches**. A new recommendation should appear within ~30
seconds (the matching algorithm runs in the background when contacts
are created).

---

## 3. Walk a contact through the workflow (~2 min)

> **Goal**: confirm staff can change a contact's status without
> waiting for the matching system.

1. Click **Contacts** in the nav.
2. Pick any contact you want to mark inactive. Click into it.
3. On the contact detail page, find the **Workflow** controls.
4. Pick:
   - **Kind**: `adopter`
   - **To state**: `do_not_engage`
   - **Reason code**: `other`
   - **Reason text**: "Testing the workflow flow"
5. Click **Apply transition**.

The contact's status updates immediately. If you've configured the
drip engine and they had an active enrollment, that enrollment will
also exit on the next worker tick (every 10s).

---

## 4. Facilitator view (~1 min)

> **Goal**: see what a facilitator user sees in their own portal.

1. Click **Facilitator** in the nav.
2. This will probably be empty for you — the `dev-local` test user
   is staff-shaped, not facilitator-shaped. That's expected.
3. To actually see content here, sign out and back in as one of the
   test facilitator accounts Joel set up (`alice+example@example.com`
   or `bob+frontier@example.com`).

In a facilitator session you'd see:
- A list of contacts assigned to your org.
- For each, an **Accept** button (escalates `matched` → `active`).
- A **Decline** button with a reason code.

---

## 5. What you won't see today

These are deliberately not wired up in the local-dev stack:

- **Real emails** (magic-link sign-ins, drip campaign sends, daily
  digests). The worker logs them but doesn't actually deliver. We'll
  wire ACS Email up in production.
- **The 9am ET daily digest**. The worker only fires it during the
  09:00-09:30 ET window. To test off-hours, ask Joel.
- **B2C sign-in**. The local stack accepts the `dev-local` shortcut
  bearer; production uses Azure B2C with full IdP routing.
- **External webhook deliveries** from the outbox. The integration
  webhook URL isn't set in dev.

---

## What to report

Anything that:
- **Looks wrong** — typos, broken layouts, confusing labels, fields
  that don't make sense in your context.
- **Doesn't work** — buttons that 500, pages that hang, errors that
  show internal codes.
- **Surprised you** — behavior that was different from how you'd
  expect, even if it's technically correct.

Drop notes in Slack DM to Joel, or open a quick GitHub issue at
[joshua-project/jp-adopt-core](https://github.com/joshua-project/jp-adopt-core/issues).

Screenshots help a lot. So does the URL of the page where you saw
the problem.

---

## If you get stuck

| Symptom | Try |
|---------|-----|
| Page doesn't load at all | Are you on the tailnet (or LAN if Joel sent a 10.x / 192.168.x URL)? `ping <DEV_URL>`. |
| Sign-in textbox missing | Paste `dev-local` anyway — clicking submit may work even without a visible input. If not, ping Joel. |
| "401 Unauthorized" anywhere | Your bearer token expired. Reload the page, sign in again. |
| Match queue empty | The dev DB might've been reset. Joel can re-seed by running `scripts/seed-local.sh`. |
| Browser shows "ERR_CONNECTION_REFUSED" | The stack on Joel's machine is down. Slack him. |
| Anything else | Take a screenshot, paste the URL, send to Joel. |

---

## What's next

When you finish your pass:
- Drop your impressions in Slack (the rough ones — "what felt off"
  is exactly what we need, not formal bug reports).
- Joel will fold them into the production deploy plan over the next
  few days.

Thanks for testing. This catches things real users will hit *before*
real users do.
