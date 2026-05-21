# Quick start (5 minutes)

Welcome back. Here's what changed while you were on vacation and how
to get the most out of the new system.

## Sign in

Go to **https://crm.joshuaproject.net** (or wherever the SWA points;
see `docs/runbooks/deploy.md`). Click **Sign in** — it'll route you
through Azure B2C with your existing credentials.

If your account doesn't work, ask for a magic link instead: enter
your email on the sign-in page and click "Email me a sign-in link."
You'll get a 15-minute, single-use link in your inbox.

## What you'll see Monday morning

The home page shows three sections:

1. **Today's matches queue** — every recommendation that came in
   overnight. This is where you spend most of your day.
2. **My contacts** — your most-recently-touched contacts (if you
   wear a facilitator hat in addition to staff_admin).
3. **Notifications** (planned for week 2) — anything that needs
   your attention beyond the queue.

The nav has links to **Contacts**, **Add contact**, **Matches**, and
**Facilitator** (the facilitator-portal view; for you it's
read-mostly since you have org membership granted across all orgs).

## The minimum you need to know

| Task | How |
|------|-----|
| Look at today's recommendations | Click **Matches** in the nav |
| Accept a match | Open it, click **Accept** |
| Send one back | Open it, pick a reason code, click **Send back** |
| Pick a different facilitator | Open it, click **Pick** on an alternate |
| Add someone from a phone call | Click **Add contact**, fill the form |
| Mark someone `do_not_engage` | Open their contact, **Workflow** → kind: adopter, to_state: do_not_engage |
| See yesterday's emails | The daily digest in your inbox; it arrives at 9 ET |

## Stuff that's the same as DT

- Contact list at `/contacts`.
- Search by name / email (basic — week 2 adds real search).
- Status field on each contact reflects where they are in the workflow.

## Stuff that's new

- **Matching algorithm**: the system suggests a facilitator based on
  geography, language, FPG coverage, and capacity. You can override
  by sending back or picking an alternate.
- **Triage queue**: adopters who didn't select an FPG land in their
  own queue for manual routing.
- **Drip campaigns**: automated emails fire when contacts transition
  to specific states. The first one (facilitator-welcome) ships ready
  to go; Ben's drip copy gets pasted into the template when ready.
- **Daily digest**: 9 ET email every day with the previous 24h of
  matches.
- **`local_modified_after_import` protection**: anything you edit in
  the new system is safe from re-imports of the same DT data.

## Stuff that's gone

- DT's plugin-driven activity log (replaced by the new `activity_log`
  table with the same data + threading).
- The "everyone sees everything" handoff strip — week 1 keeps your
  full visibility regardless of contact status. Conditional
  field-level RBAC is a v2 conversation pending your compliance
  input.

## When something feels off

Three quick checks:

1. **Hit refresh** — Next.js client-side caching can stick for ~30s.
2. **Open the contact's workflow view** — every state change writes
   an audit row visible there.
3. **Ask Joel** — for anything not in the operator handbook.

## The deeper docs

- `docs/runbooks/operator-handbook.md` — your day-to-day reference.
- `docs/runbooks/daily-digest.md` — how the 9 ET email works.
- `docs/runbooks/drip-engine.md` — how the drip campaigns trigger
  and what variables are available in the templates.
- `docs/runbooks/dt-cutover.md` — Saturday's cutover sequence (what
  Joel ran while you were out).
- `docs/runbooks/matching-algorithm-v1.md` — how the scoring works.

Welcome back. Hit me with questions any time.
