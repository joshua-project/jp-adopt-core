# Demo Fixes — Batch 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the three ready, self-contained fixes from the 2026-06-18 demo: the facilitator "Matched" pill leak, the MSAL account-picker prompt, and the migration that codifies Amy's joshuaproject.net identity.

**Architecture:** Three independent changes. (1) Extract a pure status-selection helper for contact badges and use it in the contacts list so a facilitator never renders an adopter status. (2) One-line MSAL change to force the account picker. (3) An idempotent Alembic seed revision mirroring the existing `0014`/`0015`/`0028` staff-seed pattern.

**Tech Stack:** Next.js 15 + TypeScript + Vitest/RTL (web); FastAPI + SQLAlchemy 2.0 async + Alembic + pytest (api).

**Source of truth:** `docs/follow-ups/2026-06-18-demo-findings.md` (rows A2, A3, B1).

---

## Task 1: Fix facilitator "Matched" pill leak (B1)

The contacts list cascades: a facilitator with no `facilitator_status`
falls through and renders its stray `adopter_status` with the adopter
label (bogus green "Matched" on a church). Fix by selecting the badge
strictly by `party_kind` via a pure, tested helper.

**Files:**
- Create: `apps/web/src/lib/contactStatus.ts`
- Create: `apps/web/src/lib/__tests__/contactStatus.test.ts`
- Modify: `apps/web/src/components/Contacts.tsx` (import + badge block at lines ~248-256)

- [ ] **Step 1: Write the failing test**

Create `apps/web/src/lib/__tests__/contactStatus.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { contactStatusBadge } from "../contactStatus";

describe("contactStatusBadge", () => {
  it("never leaks adopter_status onto a facilitator", () => {
    expect(
      contactStatusBadge({
        party_kind: "facilitator",
        facilitator_status: null,
        adopter_status: "matched",
      }),
    ).toBeNull();
  });

  it("shows facilitator_status for a facilitator", () => {
    expect(
      contactStatusBadge({
        party_kind: "facilitator",
        facilitator_status: "ready",
        adopter_status: null,
      }),
    ).toEqual({ status: "ready", kind: "facilitator" });
  });

  it("shows adopter_status for an adopter", () => {
    expect(
      contactStatusBadge({
        party_kind: "adopter",
        facilitator_status: null,
        adopter_status: "matched",
      }),
    ).toEqual({ status: "matched", kind: "adopter" });
  });

  it("returns null when the relevant status is unset", () => {
    expect(
      contactStatusBadge({
        party_kind: "adopter",
        facilitator_status: "ready",
        adopter_status: null,
      }),
    ).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm --filter web test contactStatus`
Expected: FAIL — `Cannot find module '../contactStatus'`.

- [ ] **Step 3: Write the helper**

Create `apps/web/src/lib/contactStatus.ts`:

```ts
import type { StatusKind } from "./vocab";

export interface ContactStatusInput {
  party_kind: string;
  adopter_status?: string | null;
  facilitator_status?: string | null;
}

/**
 * Pick the badge to show for a contact, keyed strictly by party_kind.
 * adopter_status and facilitator_status are disjoint enums — a facilitator
 * must never render its (stray) adopter_status, or you get nonsense like a
 * "Matched" pill on an org. Returns null when the relevant status is unset.
 */
export function contactStatusBadge(
  c: ContactStatusInput,
): { status: string; kind: StatusKind } | null {
  if (c.party_kind === "facilitator") {
    return c.facilitator_status
      ? { status: c.facilitator_status, kind: "facilitator" }
      : null;
  }
  if (c.party_kind === "adopter") {
    return c.adopter_status
      ? { status: c.adopter_status, kind: "adopter" }
      : null;
  }
  return null;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm --filter web test contactStatus`
Expected: PASS (4 tests).

- [ ] **Step 5: Use the helper in the contacts list**

In `apps/web/src/components/Contacts.tsx`, add to the imports (next to the
existing `./StatusBadge` import on line 12):

```ts
import { contactStatusBadge } from "../lib/contactStatus";
```

Replace the `badge={...}` block (the `c.party_kind === "facilitator" && …`
ternary, ~lines 248-256) with:

```tsx
                    badge={(() => {
                      const b = contactStatusBadge(c);
                      return b ? (
                        <StatusBadge status={b.status} kind={b.kind} />
                      ) : undefined;
                    })()}
```

- [ ] **Step 6: Run the full web suite + typecheck**

Run: `pnpm --filter web test`
Run: `pnpm --filter web exec tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 7: Commit**

```bash
git add apps/web/src/lib/contactStatus.ts \
        apps/web/src/lib/__tests__/contactStatus.test.ts \
        apps/web/src/components/Contacts.tsx
git commit -m "fix(web): stop facilitator rows rendering a stray adopter status pill"
```

---

## Task 2: Force the MSAL account picker (A3)

`loginPopup` passes no `prompt`, so an existing tenant session signs in
silently with the cached account. This is a single-flag IdP-interaction
change; its real verification is manual (an automated test would only
assert we pass a literal flag to a mocked SDK call, which adds no
confidence). Change + manual verification.

**Files:**
- Modify: `apps/web/src/components/Contacts.tsx:110`

- [ ] **Step 1: Make the change**

In `apps/web/src/components/Contacts.tsx`, change line 110 from:

```ts
      .loginPopup({ scopes })
```

to:

```ts
      .loginPopup({ scopes, prompt: "select_account" })
```

- [ ] **Step 2: Typecheck**

Run: `pnpm --filter web exec tsc --noEmit`
Expected: no errors (`prompt` is a valid field on `PopupRequest`).

- [ ] **Step 3: Manual verification**

With an existing signed-in tenant session in the browser, click sign-in
on the contacts page. Expected: the Microsoft **account picker appears**
instead of silently reusing the cached account.

- [ ] **Step 4: Commit**

```bash
git add apps/web/src/components/Contacts.tsx
git commit -m "fix(web): force MSAL account picker so users can choose the right account"
```

---

## Task 3: Seed Amy's joshuaproject.net identity — migration `0029` (A2)

Codify the direct prod `user_roles` insert into Alembic history and add
the matching `staff_profile` row so the daily digest reaches her JP
account. Idempotent — a no-op against the row already in prod.

**Files:**
- Create: `apps/api/alembic/versions/20260618_0029_seed_amy_jp_tenant_identity.py`
- Create: `apps/api/tests/test_seed_0029_amy_jp_identity.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_seed_0029_amy_jp_identity.py`:

```python
"""0029 seeds Amy Banta's joshuaproject.net identity (role + profile)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

_OID = "77fb39e1-3acd-4012-bd8d-2a2a34534dc1"


@pytest.mark.asyncio
async def test_amy_jp_identity_seeded(session: AsyncSession) -> None:
    roles = (
        await session.execute(
            sa.text(
                "SELECT r.name FROM user_roles ur "
                "JOIN roles r ON r.id = ur.role_id "
                "WHERE ur.user_subject_id = :oid"
            ),
            {"oid": _OID},
        )
    ).scalars().all()
    assert "staff_admin" in roles

    email = (
        await session.execute(
            sa.text(
                "SELECT email_normalized FROM staff_profile "
                "WHERE b2c_subject_id = :oid"
            ),
            {"oid": _OID},
        )
    ).scalar_one_or_none()
    assert email == "amy.banta@joshuaproject.net"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run --extra dev pytest tests/test_seed_0029_amy_jp_identity.py -v`
Expected: FAIL — both assertions fail (no role row joins, profile email is `None`) because `0029` doesn't exist yet.

- [ ] **Step 3: Write the migration**

Create `apps/api/alembic/versions/20260618_0029_seed_amy_jp_tenant_identity.py`:

```python
"""Seed Amy Banta's joshuaproject.net identity (user_roles + staff_profile)

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-18

During the 2026-06-18 demo, Amy signed in with her joshuaproject.net
account — a different Entra OID than her globalspecifics.com identity
seeded in 0014/0015/0028. Her role was granted by a direct INSERT into
``user_roles`` in production to unblock the demo. This revision codifies
that grant in migration history (so a rebuild/restore reproduces it) and
adds the matching ``staff_profile`` row so the daily digest reaches her.

Idempotent: ON CONFLICT DO NOTHING on both inserts — a no-op against the
row already present in production.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic ID metadata
revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OID = "77fb39e1-3acd-4012-bd8d-2a2a34534dc1"  # Amy Banta (amy.banta@joshuaproject.net)
_ROLE = "staff_admin"
_DISPLAY_NAME = "Amy Banta"
_EMAIL = "amy.banta@joshuaproject.net"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO user_roles (user_subject_id, role_id)
            SELECT :oid, id FROM roles WHERE name = :role
            ON CONFLICT (user_subject_id, role_id) DO NOTHING
            """
        ).bindparams(oid=_OID, role=_ROLE)
    )
    op.execute(
        sa.text(
            """
            INSERT INTO staff_profile (
                b2c_subject_id, email, email_normalized, display_name
            )
            VALUES (:oid, :email, :email_norm, :name)
            ON CONFLICT (b2c_subject_id) DO NOTHING
            """
        ).bindparams(
            oid=_OID, email=_EMAIL, email_norm=_EMAIL.lower(), name=_DISPLAY_NAME
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM staff_profile WHERE b2c_subject_id = :oid"
        ).bindparams(oid=_OID)
    )
    op.execute(
        sa.text(
            """
            DELETE FROM user_roles
            WHERE user_subject_id = :oid
              AND role_id IN (SELECT id FROM roles WHERE name = :role)
            """
        ).bindparams(oid=_OID, role=_ROLE)
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && uv run --extra dev pytest tests/test_seed_0029_amy_jp_identity.py -v`
Expected: PASS (the test DB applies migrations to head, including `0029`).

- [ ] **Step 5: Verify the revision chain is linear (single head)**

Run: `cd apps/api && uv run alembic heads`
Expected: exactly one head — `0029`.

- [ ] **Step 6: Commit**

```bash
git add apps/api/alembic/versions/20260618_0029_seed_amy_jp_tenant_identity.py \
        apps/api/tests/test_seed_0029_amy_jp_identity.py
git commit -m "feat(api): seed Amy's joshuaproject.net identity (role + staff_profile) in 0029"
```

---

## Final verification (after all three tasks)

- [ ] `pnpm --filter web test` — green
- [ ] `cd apps/api && uv run --extra dev pytest` — green
- [ ] `git log --oneline -3` shows the three commits
- [ ] Open a PR to `main`; CI runs both suites + the contracts check.

**Deploy note:** none of these help the *current* demo session until they
ship. `0029` is idempotent so applying it to prod (already carrying the
direct insert) is safe; the web fixes deploy with the next web build; the
DNS rebind (E2) is a separate, prerequisite-aware step.

## Not in this batch

Operator/data items (DT backfill, prod operational seeding, DNS rebind,
spreadsheet load, dedup, campaign activation) and features needing their
own brainstorm (delete, upload, template editor, search, matching tuning)
are tracked in `docs/follow-ups/2026-06-18-demo-findings.md`, Phases 2–3.
