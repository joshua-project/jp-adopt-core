---
title: "refactor: people-group identity (rop3 → people_id3 + forms-cache as source of truth)"
type: refactor
status: active
created: 2026-05-28
depth: standard
related_issues: ["#73", "#74"]
related_prs: ["#71", "jp-adopt-forms#149"]
---

# refactor: people-group identity (rop3 → people_id3 + forms-cache as source of truth)

Re-key jp-adopt-core's people-group identity from `rop3` to `people_id3` and
make jp-adopt-forms's `fpg_cache` the single source of truth for JP
people-group reference data (gaining a `rop3` column so external-org
integrations on the forms side still have it). The translation layer
introduced by U12 (PR #71's `_resolve_rop3` + the `JOSHUA_PROJECT_API_KEY`
external dependency) goes away; core mirrors the forms cache via a thin HTTP
export endpoint instead of calling the JP API directly.

**Why now.** Core's `fpg` is greenfield (5 demo + 67 synthetic rows, no real
contacts/interests/matches), so re-keying is a destructive drop-and-resync —
no value backfill, no production data risk. Every real contact that lands
makes this more expensive.

**What this is not.** Not a re-litigation of the U12 design — PRs #71 / #149
are the correct bridge and stay shipping. This is the follow-up that removes
the bridge mechanics once the better path is in.

---

## Problem Frame

`rop3` (Registry of Peoples code) is used as the canonical people-group
identifier in jp-adopt-core: PK of the `fpg` table, FK on every
`adopter_interest`, and a composite PK column on `facilitator_fpg_coverage`.
The matching algorithm (`_fpg_affinity_score`) uses it as a pure opaque
exact-match join key with no hierarchy or semantics in the value (verified at
`apps/api/src/jp_adopt_api/domain/matching.py`).

Everywhere else — jp-adopt-forms's public forms + cache, Disciple.Tools's
form-submission JSON, and the Joshua Project API itself — uses `people_id3`.
`rop3` and `people_id3` are 1:1 across countries (verified live: ROP3=100425
↔ PeopleID3=10375 in both Sri Lanka and Pakistan). Core is the only system
keying on `rop3`.

The U12 work (PRs #71 / #149) bridged the gap with a `people_id3 → rop3`
lookup through a populated `fpg.people_id3` column. That works, but it makes
permanent two things that should be temporary:

1. A translation layer on every form submission (`_resolve_rop3` in
   `apps/api/src/jp_adopt_api/routers/intake.py`).
2. An external runtime dependency on the Joshua Project API in core (the
   `apps/api/src/jp_adopt_api/scripts/sync_fpg.py` script + the
   `JOSHUA_PROJECT_API_KEY` config), required only to populate the lookup
   table.

It also leaves the deferred DT contact-import ETL ("U13 cutover", flagged in
`apps/etl/src/jp_adopt_etl/orchestrator.py` lines 399-403) blocked on the same
`rop3` resolution — DT carries `people_id3` in `fpg_submission_data`, not
`rop3`, so an ETL keyed on `people_id3` would be a straight copy.

---

## Scope Boundaries

**In scope:**
- Adding a `rop3` column to jp-adopt-forms's `PeopleGroup` cache so the cache
  becomes the single source of truth for both identifiers.
- Replacing core's `fpg.rop3` PK with `people_id3` PK; renaming the FK
  columns and composite PK on `adopter_interest` and
  `facilitator_fpg_coverage`.
- Renaming `rop3` → `people_id3` throughout the matching domain, intake
  schemas, read surfaces, ETL, and web client.
- Replacing core's JP-API sync script with a forms-cache mirror via a new
  HTTP export endpoint on jp-adopt-forms.
- Removing `JOSHUA_PROJECT_API_KEY` and `_resolve_rop3` from core.
- Bumping the outbox event `schema_version` to `jp.adopt.v2` (payload shape
  changes in `fpg_selections`).

**Out of scope (deferred to follow-up work):**
- The rest of the DT U13 cutover beyond replacing `rop3` references in the
  ETL with `people_id3`. Building the actual contact-import resolver path is
  separate.
- Product surfacing of `rop3` (this stays data-layer only in forms).
- Backward-compat aliasing on `FpgInterestIn` — PRs #71 / #149 are the
  bridge; this plan is a clean break (see Key Technical Decisions).

**Out of scope (non-goals):**
- Re-litigating PRs #71 / #149. They merge first, serve as the bridge, and
  stay in the history; this plan removes the bridge mechanics they
  introduced.
- Changing what people-group reference data core stores (still
  `people_id3`, name, country_code, frontier — the field set is unchanged,
  only the key column changes).

---

## Key Technical Decisions

### KTD-1. `jp-adopt-forms`'s `fpg_cache` is the single source of truth

The cache already mirrors the Joshua Project API for ~17K people groups
(populated by `apps/forms-cache/...` via `jp-adopt-forms/src/lib/jp-api.ts`).
Adding `rop3` to it makes the cache the natural single point where both
identifiers (and the per-people-group metadata both repos need) live. Core's
`fpg` table becomes a thin mirror of the cache, not an independent JP-API
consumer. **Rationale:** one cache instead of two; one external dependency
(forms → JP) instead of two; the only place that needs to talk to the JP API
is the system that already does.

### KTD-2. HTTP export endpoint for forms → core sync (not cross-DB)

Core pulls cache data via a new `GET /api/v1/people-groups/export` endpoint
on jp-adopt-forms (bearer-token auth, JSON response, ~3500 rows). **Not**
a cross-database read. **Rationale:** services communicate via APIs; no
shared DB credentials across deployment boundaries; reuses the forms repo's
existing api-key/scope infrastructure (`api-key.ts`, `api-scopes.ts`); easy
to test, version, and rotate independently. Alternative considered:
cross-DB read with a read-only connection string — faster to build but
couples deployment topology and bypasses the natural service boundary.

### KTD-3. Clean break on `FpgInterestIn` (no soft alias)

The intake schema drops the `rop3` field entirely; `people_id3` is required.
**Rationale:** PRs #71 / #149 are the bridge during the transition, so a
permanent legacy-alias field would be dead code from the moment this plan
lands. Forms client (jp-adopt-forms#149) and core's `_resolve_rop3` go away
in lockstep when the forms PR for this plan deploys before core's.

### KTD-4. Destructive migration (drop and re-sync)

The migration that re-keys `fpg`/`adopter_interest`/`facilitator_fpg_coverage`
deletes all existing rows before altering the schema. **Rationale:** prod is
empty; the 5 demo + 67 synthetic `fpg` rows in local/dev DBs are not worth
preserving. Re-running the forms-cache mirror after migration restores the
real reference data (3,481 frontier groups). Avoids a fragile rop3→people_id3
value-translation step in the migration itself.

### KTD-5. Bump outbox `schema_version` to `jp.adopt.v2`

The `submission_received` outbox event's `fpg_selections` payload changes
shape (`rop3` field removed, `people_id3` becomes the identifier).
**Rationale:** outbox payloads are the contract with downstream subscribers
(drip engine, future analytics). A version bump is the honest signal even
though no production subscriber currently parses the rop3 field. Greenfield
is the cheapest time to bump.

### KTD-6. Drop `rop3` from core entirely (no retained nullable column)

`fpg.rop3` is removed, not kept as a nullable non-key attribute.
**Rationale:** the forms cache holds `rop3` for any external-org integration
that needs the Registry-of-Peoples code; core never talks to those external
orgs directly. Keeping `rop3` on core's `fpg` would invite drift between
the two caches and re-introduce the question of which is authoritative.

### KTD-7. Two PRs, sequenced at merge time

PR A (jp-adopt-forms): U1 + U2 — add `rop3` to cache + export endpoint.
PR B (jp-adopt-core): U3 – U8 — the re-key and the mirror sync.
PR A must merge and deploy before PR B's mirror sync can run **against a
live forms endpoint**.

**Sequencing is enforced at merge time, not at code time.** Both PRs can be
written and opened in a single pass — PR B's code is correct against the
spec regardless of whether PR A is deployed. Local testing of PR B's mirror
sync uses a mocked forms endpoint (or the forms branch running locally), not
the deployed forms instance. The human merger orders the merges (A then B)
and runs the post-deploy live sync.

**Rationale:** decouples code-time work (autonomous) from
merge/deploy-time work (human-gated); cursor-agent or any unattended runner
can open both PRs without waiting for a deploy cycle in between. PR A is
independently shippable (adds a column + endpoint; no consumer until PR B).

---

## High-Level Technical Design

The shape of the change after this plan lands:

```
┌─────────────────────────┐
│  Joshua Project API     │  (source of truth for all PG data)
└──────────┬──────────────┘
           │ jp-api.ts (existing)
           ▼
┌─────────────────────────────────────┐
│  jp-adopt-forms                     │
│    fpg_cache (PeopleGroup)          │  ← gains `rop3` column (U1)
│    + people_id3, rop3, name,        │
│      country, country_code, ...     │
│                                     │
│  GET /api/v1/people-groups/export   │  ← new endpoint (U2)
└──────────┬──────────────────────────┘
           │ HTTP, bearer auth
           ▼
┌─────────────────────────────────────┐
│  jp-adopt-core                      │
│    fpg (PK: people_id3) ← thin      │  ← re-keyed (U3)
│    mirror of forms cache            │
│                                     │
│  scripts/sync_fpg.py (rewritten)    │  ← pulls from forms, not JP (U7)
│  adopter_interest.people_id3        │  ← FK to fpg.people_id3 (U3)
│  facilitator_fpg_coverage           │  ← composite PK uses people_id3 (U3)
│    .people_id3                      │
│  matching.covered_people_id3s       │  ← mechanical rename (U4)
└─────────────────────────────────────┘
```

This illustrates the intended approach and is directional guidance for
review, not implementation specification.

---

## Implementation Units

### U1. Add `rop3` column to forms `PeopleGroup` cache

**Target repo:** jp-adopt-forms (PR A, lands first)

**Goal:** Forms cache stores `rop3` alongside `people_id3` so it can serve
both identifiers to internal callers and external-org integrations.

**Files:**
- `prisma/schema.prisma` — add `rop3 String? @map("rop3")` to `PeopleGroup`
- `prisma/migrations/<new>_add_rop3_to_people_group/migration.sql` — column
  add, nullable (existing rows backfilled by next refresh)
- `src/lib/jp-api.ts` — extend `JpPeopleGroupRow` and `ApiPgRow` types;
  update `normalizeRow` to extract `row.ROP3`
- `src/lib/__tests__/jp-api.test.ts` (if exists; otherwise inline in U1's
  test scenarios)

**Approach:**
- Nullable column add — no risk to existing rows.
- The JP API already returns `ROP3` in `/v1/people_groups.json` responses
  (verified live: 5/5 sample rows had it). Normalizer extracts and stores it.
- Existing scheduled `/api/cron/refresh-fpg` job repopulates the full cache
  on next run, filling `rop3` for every row. No separate backfill script.
- Verify after deploy + one refresh cycle that `SELECT COUNT(*) FROM fpg_cache
  WHERE rop3 IS NULL` returns 0.

**Patterns to follow:**
- Existing column-add migration: `prisma/migrations/20260514000000_add_people_group_jp_api_columns/migration.sql`
- Normalizer pattern: `src/lib/jp-api.ts:195-272` (the `normalizeRow` function and `ApiPgRow` row-shape type)

**Test scenarios:**
- `normalizeRow` extracts `ROP3` when present in the source row (assert
  `result.rop3 === "100425"` for the sample row shape).
- `normalizeRow` returns `null` for `rop3` when source row omits `ROP3`
  (forward-compatible with partial API responses).
- Existing normalizer tests (people_id3, name, country, frontier, etc.) all
  still pass — adding the field doesn't break existing extraction.

**Verification:**
- *Autonomous:* migration applies cleanly to a local Postgres; `\d fpg_cache`
  shows the new `rop3` column (nullable); normalizer tests pass; running
  `jp-api.ts`'s `fetchAllPeopleGroupsFromApi` against a mocked JP API
  response produces rows with `rop3` populated.
- *Human-gated (post-deploy):* after the migration deploys and one
  refresh-fpg cycle runs in staging, `fpg_cache.rop3` is non-null for every
  frontier row.

---

### U2. People-group export endpoint on jp-adopt-forms

**Target repo:** jp-adopt-forms (PR A, lands first; depends on U1)

**Goal:** A read-only HTTP endpoint that jp-adopt-core's mirror sync calls
to refresh its `fpg` table. Returns one row per `people_id3` (cache rows
collapsed by people identity, since core's `fpg` is across-countries).

**Files:**
- `src/app/api/v1/people-groups/export/route.ts` (new)
- `src/app/api/v1/people-groups/export/__tests__/route.test.ts` (new)
- `.env.example` — add `CORE_EXPORT_API_KEY` (or reuse existing api-key
  infrastructure — see Approach)

**Approach:**
- `GET /api/v1/people-groups/export?frontier_only=true`
- Bearer auth. Reuse the forms repo's existing scoped api-key system
  (`api-key.ts`, `api-scopes.ts`) by adding a new scope like
  `people_groups:export`, OR introduce a single-purpose `CORE_EXPORT_API_KEY`
  env var if simpler. **Decision at implementation time** — depends on how
  the existing scope infrastructure looks (likely the scoped api-key path is
  cleaner since the infra is already there).
- Response shape:
  ```json
  {
    "data": [
      {
        "people_id3": "10375",
        "rop3": "100425",
        "name": "Arab, general",
        "country_code": "PAK",
        "frontier": true
      },
      ...
    ],
    "count": 3481
  }
  ```
- Collapse multi-country PGIC rows to one per `people_id3`, picking
  highest-population country as the representative (mirrors core's existing
  `normalize_rows` logic in `apps/api/src/jp_adopt_api/scripts/sync_fpg.py`,
  which is the pattern we're moving server-side).
- Frontier-only filter applied server-side.
- Response is JSON; no streaming required at 3.5k row scale.

**Patterns to follow:**
- Existing Next.js API route patterns in `src/app/api/v1/`
- Existing api-key/scope auth wrappers (`api-key.ts`, `api-scopes.ts`,
  `api-guard.ts`)
- Idempotency / envelope patterns (`api-envelopes.ts`) — though probably
  overkill for a read-only export

**Test scenarios:**
- `GET` without bearer → 401.
- `GET` with valid bearer → 200, JSON envelope with `data` array.
- Response collapses multi-country rows by `people_id3` (insert two
  cache rows with same people_id3, different country/population; assert one
  row in response with the higher-population country's country_code).
- `frontier_only=true` (default) excludes non-frontier rows.
- Empty cache → 200 with empty array.

**Verification:**
- *Autonomous:* route handler tests pass (401 without auth, 200 with auth,
  collapse-by-people_id3 logic, frontier filter, empty-cache case); the
  route handles a fixture-seeded `fpg_cache` and returns the expected
  envelope shape; `pnpm test` exits 0.
- *Human-gated (post-deploy):* curl against the deployed staging endpoint
  with a valid bearer token returns ~3,481 rows of the expected shape.

---

### U3. Re-key core's `fpg`, `adopter_interest`, and `facilitator_fpg_coverage`

**Target repo:** jp-adopt-core (PR B)

**Goal:** Single Alembic migration that drops `rop3` as the canonical key,
makes `people_id3` the PK of `fpg`, renames the FK columns on
`adopter_interest` and `facilitator_fpg_coverage`, and adjusts the composite
PK on `facilitator_fpg_coverage`.

**Dependencies:** None within this plan; lands first in PR B.

**Files:**
- `apps/api/alembic/versions/20260528_0021_people_id3_canonical.py` (new;
  current head is `0020`)
- `apps/api/src/jp_adopt_api/models.py` — `Fpg`, `AdopterInterest`,
  `FacilitatorFpgCoverage` model updates
- `apps/api/tests/test_models.py` (if exists; otherwise model behavior is
  tested through the intake/matching tests in later units)

**Approach (destructive migration, per KTD-4):**

Migration order — single transaction:
1. `DELETE FROM facilitator_fpg_coverage; DELETE FROM adopter_interest; DELETE FROM fpg;`
2. Drop FKs: `adopter_interest_rop3_fkey`, the composite PK on
   `facilitator_fpg_coverage`.
3. Drop indexes: `ix_adopter_interest_rop3`, `ix_facilitator_fpg_coverage_rop3`,
   `ix_fpg_people_id3` (redundant once people_id3 is the PK), `ix_fpg_country_code` (recreate after).
4. Drop column: `fpg.rop3`.
5. `ALTER TABLE fpg ALTER COLUMN people_id3 SET NOT NULL;`
6. `ALTER TABLE fpg ADD PRIMARY KEY (people_id3);`
7. `ALTER TABLE adopter_interest RENAME COLUMN rop3 TO people_id3;`
8. `ALTER TABLE adopter_interest ADD CONSTRAINT adopter_interest_people_id3_fkey FOREIGN KEY (people_id3) REFERENCES fpg(people_id3);`
9. `ALTER TABLE facilitator_fpg_coverage RENAME COLUMN rop3 TO people_id3;`
10. `ALTER TABLE facilitator_fpg_coverage ADD PRIMARY KEY (facilitator_org_id, people_id3);`
11. Recreate indexes: `ix_adopter_interest_people_id3`,
    `ix_facilitator_fpg_coverage_people_id3`.
12. `downgrade()` reverses — recreates `rop3` columns as nullable, restores
    PKs/FKs. The downgrade is structurally complete but does NOT restore
    data (since we deleted in step 1); acceptable per KTD-4.

Model updates:
- `Fpg`: drop `rop3` column; `people_id3` becomes `mapped_column(Text,
  primary_key=True, nullable=False)`. Drop `ix_fpg_people_id3` `__table_args__`
  entry (now redundant).
- `AdopterInterest`: rename `rop3` → `people_id3` (Text, nullable, FK to
  fpg.people_id3). Update `ix_adopter_interest_rop3` → `ix_adopter_interest_people_id3`.
- `FacilitatorFpgCoverage`: rename `rop3` → `people_id3`; composite PK
  becomes `(facilitator_org_id, people_id3)`.
- Drop the `contact_has_no_rop3` informational comment / column if present
  on Contact (verify against `models.py:512` — likely a comment, not a
  column, but check).

**Patterns to follow:**
- Migration style: `apps/api/alembic/versions/20260526_0019_contact_assignment.py`
- Destructive-drop pattern: any existing data-purge migration (or write
  inline; this is a one-off pre-prod migration).

**Test scenarios:**
- Migration applies cleanly (`uv run alembic upgrade head` exits 0 against a
  DB at revision 0020).
- After migration: `INSERT INTO fpg (people_id3, name) VALUES ('10375', 'Test')`
  succeeds; `INSERT INTO fpg (people_id3, name) VALUES (NULL, 'Test')` fails
  with NOT NULL violation; `INSERT INTO adopter_interest (id, contact_id,
  people_id3) VALUES (...)` with an unknown people_id3 fails with FK
  violation.
- `facilitator_fpg_coverage` composite PK enforced (duplicate
  `(facilitator_org_id, people_id3)` raises uniqueness violation).
- Model FK relationships load correctly via SQLAlchemy
  (`session.execute(select(AdopterInterest).options(...))` doesn't error on
  missing column).
- Downgrade applies cleanly (schema returns to revision 0020's shape, even
  though data is gone).

**Verification:** `uv run alembic upgrade head` on a fresh DB; structural
inspection via `\d fpg`, `\d adopter_interest`, `\d facilitator_fpg_coverage`
matches expected shape.

---

### U4. Matching domain rename

**Target repo:** jp-adopt-core (PR B)

**Dependencies:** U3 (FK columns must be renamed before code can query the
new column).

**Goal:** Mechanical rename of `rop3` → `people_id3` throughout the matching
algorithm. Zero behavior change — `_fpg_affinity_score` is opaque
exact-match, so the rename is purely syntactic.

**Execution note:** Run the full matching test suite after this unit's
changes. If any test fails, the rename revealed something missed; investigate
before proceeding to subsequent units. The existing matching test coverage
is the regression guardrail.

**Files:**
- `apps/api/src/jp_adopt_api/domain/matching.py` — the bulk of the rename
- `apps/api/src/jp_adopt_api/domain/matching_config.py` — verify no rop3
  references (likely clean, but check)
- `apps/api/tests/test_matching*.py` — update fixtures and assertions;
  rename test variable names for clarity

**Approach (search/replace, but verified case-by-case):**
- `covered_rop3s` → `covered_people_id3s` (frozenset field on Candidate)
- Function parameters: `_fpg_affinity_score(rop3, covered_rop3s)` →
  `_fpg_affinity_score(people_id3, covered_people_id3s)`
- DB queries: `FacilitatorFpgCoverage.rop3` → `FacilitatorFpgCoverage.people_id3`
- DB queries: `AdopterInterest.rop3` → `AdopterInterest.people_id3`
- The "no-FPG" branch condition (`interest.rop3 IS NULL` →
  `interest.people_id3 IS NULL`) — semantic meaning unchanged (no FPG
  selected → triage).
- `MatchAttempt.filter_results` dict keys: `"covered_rop3s"` → `"covered_people_id3s"`
- `InterestOutcome` field (if it stores rop3): rename to `people_id3`

**Patterns to follow:**
- The matching algorithm itself doesn't change; this is rename-only.
- Preserve the existing `MatchAttempt` write semantics — only the dict keys
  change.

**Test scenarios:**
- All existing matching tests pass after rename (this is the primary
  guardrail).
- Add one regression test: same inputs (now expressed in `people_id3`)
  produce the same `_fpg_affinity_score` output as the equivalent rop3-keyed
  test would have. (May be redundant if existing tests fully cover; include
  only if a gap surfaces.)
- The "no-FPG" interest still routes to triage (existing test renamed).

**Verification:** `uv run pytest tests/test_matching*.py -v` exits 0 with the
same test count as before (modulo renames in test names).

---

### U5. Intake schemas + handler refactor

**Target repo:** jp-adopt-core (PR B)

**Dependencies:** U3 (model rename), U4 (matching keys renamed so
read-surfaces match).

**Goal:** Drop the `_resolve_rop3` helper and the `rop3` field from
`FpgInterestIn`. Intake stores `people_id3` directly. Outbox payload bumps
to `jp.adopt.v2`. Read surfaces rename `rop3_name`/`rop3_country` →
`people_id3_name`/`people_id3_country`.

**Files:**
- `apps/api/src/jp_adopt_api/schemas.py` — `FpgInterestIn` (drop `rop3`
  field, drop `require_identifier` validator since `people_id3` is now
  required), `ContactMatchRow` field rename
- `apps/api/src/jp_adopt_api/routers/intake.py` — delete `_resolve_rop3`;
  simplify `_create_interests` (no DB lookup, just set
  `interest.people_id3 = str(sel.people_id3)`); bump
  `outbox_payload["schema_version"]` to `"jp.adopt.v2"`
- `apps/api/src/jp_adopt_api/routers/contacts.py` — matches read endpoint:
  rename `rop3_name`/`rop3_country` projection fields; update the `Fpg` join
  (now on `people_id3`)
- `apps/api/tests/test_intake_profile.py` — rename rop3→people_id3 in
  assertions; drop the `_resolve_rop3` regression test (becomes a no-op
  identity)
- `apps/api/tests/test_intake.py` — update payload fixtures
- `apps/api/tests/test_contacts_record.py` — update matches-endpoint
  assertions for the renamed fields
- `packages/contracts/src/generated/api.ts` — regenerated via `pnpm
  contracts:generate`; commit the artifact diff

**Approach:**
- `FpgInterestIn` becomes: `people_id3: int` (required, no longer
  optional), plus the existing `commitment_level`, `notes`,
  `commitment_types`, `engagement_status`, `facilitation_services`,
  `network_services`. Drop the `rop3` field and the `require_identifier`
  validator.
- `_create_interests`: each `FpgInterestIn` produces one
  `AdopterInterest` row with `people_id3 = str(sel.people_id3)`. No
  lookup. The `_resolve_rop3` function is deleted.
- Outbox payload's `fpg_selections` now serializes with `people_id3` keys
  (no rop3). Bump `schema_version` to `"jp.adopt.v2"`.
- `ContactMatchRow`: `rop3` → `people_id3`; `rop3_name` →
  `people_id3_name`; `rop3_country` → `people_id3_country`. The matches
  router's outerjoin on Fpg now joins on `people_id3`.
- After all code changes, run `pnpm contracts:generate` and commit the
  regenerated `packages/contracts/src/generated/api.ts`. The CI "contracts
  artifact must be committed" check enforces this.

**Patterns to follow:**
- Schema patterns in `apps/api/src/jp_adopt_api/schemas.py` (existing
  field shape, ConfigDict usage)
- Intake handler shape in `apps/api/src/jp_adopt_api/routers/intake.py`

**Test scenarios:**
- Intake POST with `{people_id3: 10375}` in `fpg_selections` succeeds, and
  the resulting `adopter_interest.people_id3` equals `"10375"`.
- Intake POST with `{rop3: "100425"}` (legacy field) returns 422 — the
  `rop3` field is gone, no longer accepted.
- Intake POST without `people_id3` in an `fpg_selection` returns 422.
- Adoption intake outbox event has `schema_version: "jp.adopt.v2"` and
  `fpg_selections[].people_id3` (no `rop3` key).
- `GET /v1/contacts/{id}/matches` returns rows with `people_id3`,
  `people_id3_name`, `people_id3_country` fields (no `rop3*` fields).
- `pnpm contracts:generate` produces a clean diff and the artifact compiles
  against the web client.

**Verification:** Intake/contact-record test suites pass; generated
contracts artifact is committed; manual curl against the running stack
confirms the response shape.

---

### U6. Web client + read-surface updates

**Target repo:** jp-adopt-core (PR B)

**Dependencies:** U5 (regenerated contracts artifact).

**Goal:** Mechanical rename of `rop3` references in the staff web client to
match the new contract. Display labels (visible to users) don't change —
"People group" / country are still the user-facing strings; only the data
field names change.

**Files (per the 24-reference scan):**
- `apps/web/src/components/ContactRecord.tsx` — interest tiles, matches
  tile (read `people_id3_name`/`people_id3_country` instead of `rop3_name`)
- `apps/web/src/components/PipelineView.tsx` — any rop3 column refs
- `apps/web/src/components/Contacts.tsx`, `Adopters.tsx`, `Facilitators.tsx`
  — check for rop3 refs (likely none; verify)
- Any other web files surfacing via `grep -rn 'rop3' apps/web/`
- `apps/web/src/lib/vocab.ts` — verify no enum/label impact (likely none;
  rop3 was never a user-facing enum value)

**Approach:**
- Mechanical search/replace in TypeScript: `rop3` → `people_id3`,
  `rop3_name` → `people_id3_name`, `rop3_country` → `people_id3_country`,
  type imports adjust.
- User-facing strings unchanged.
- After changes, start the dev stack (per AGENTS.md) and visit
  `/contacts/{id}` to confirm matches/interests tiles render correctly.

**Patterns to follow:**
- Existing component data-access patterns in `ContactRecord.tsx`

**Test scenarios:**
- No web test harness yet (#31); verification is manual per AGENTS.md.
- Type check: `pnpm lint:web` exits 0 (the regenerated contracts artifact
  has matching field names).
- Manual UI smoke: load a contact record; interests tile shows people-group
  name + country; matches tile shows the same — both populated from the
  renamed fields.

**Verification:** Type check passes; UI renders correctly in the local dev
stack; screenshot evidence captured if the diff is non-trivial.

---

### U7. Replace JP-API sync with forms-cache mirror + cleanup

**Target repo:** jp-adopt-core (PR B)

**Dependencies:** U1 + U2 must be deployed (forms cache has `rop3`; export
endpoint exists). U3 (the schema is people_id3-keyed).

**Goal:** Replace `apps/api/src/jp_adopt_api/scripts/sync_fpg.py` with a
script that pulls from `jp-adopt-forms`'s export endpoint and upserts core's
`fpg` table. Remove the `JOSHUA_PROJECT_API_KEY` config setting. Drop the
JP-API client code from core.

**Files:**
- `apps/api/src/jp_adopt_api/scripts/sync_fpg.py` — rewritten end-to-end
- `apps/api/src/jp_adopt_api/config.py` — drop `joshua_project_api_key`;
  add `forms_export_url` (str) and `forms_export_api_key` (str)
- `apps/api/tests/test_sync_fpg.py` — rewritten to match new script
- `apps/api/.env` / `.env.example` (if present) — config var rename

**Approach:**
- New `sync_fpg.py` flow:
  1. Read `forms_export_url` + `forms_export_api_key` from settings.
  2. `httpx.AsyncClient` GET to `{forms_export_url}/api/v1/people-groups/export?frontier_only=true`
     with `Authorization: Bearer {forms_export_api_key}`.
  3. Parse response `data` array; each item is `{people_id3, rop3, name,
     country_code, frontier}` (note: response includes `rop3` for forms-cache
     parity, but core's `fpg` schema doesn't have `rop3` after U3, so we
     ignore that field on upsert).
  4. Upsert into `fpg` keyed on `people_id3` (`ON CONFLICT (people_id3) DO
     UPDATE SET name, country_code, frontier`).
- Pure-vs-IO separation: `normalize_rows` (pure, transforms response →
  upsert rows) and `upsert_fpg` (DB) stay separable for unit testing.
- The dedup-by-rop3 logic from the current `sync_fpg.py` goes away — forms'
  endpoint already collapses by `people_id3`.

**Patterns to follow:**
- Existing `sync_fpg.py` structure (httpx async + pg_insert + on_conflict)
- Existing test pattern in `apps/api/tests/test_sync_fpg.py`

**Test scenarios:**
- `normalize_rows` transforms the forms-cache response shape into upsert
  rows (assert one upsert row per response item, fields mapped correctly).
- `normalize_rows` skips items missing `people_id3` (defensive).
- `upsert_fpg` inserts new rows, then updates existing rows on re-run
  (same people_id3 with changed name → name updated).
- End-to-end: with the forms export endpoint mocked to return 3 sample
  rows, the script inserts 3 fpg rows.

**Verification:**
- *Autonomous:* `normalize_rows` and `upsert_fpg` unit tests pass; an
  integration test runs the script against a mocked forms endpoint
  (httpx mocking or a local fixture-served HTTP server) and asserts the
  expected rows land in a local test `fpg` table.
- *Human-gated (post-deploy):* end-to-end run against the deployed forms
  endpoint loads ~3,481 rows into core's `fpg` table; `SELECT COUNT(*) FROM
  fpg` matches the forms-cache frontier count. Document this in PR B's
  description as the post-merge step the operator runs.

---

### U8. ETL rename — replace `rop3` with `people_id3`

**Target repo:** jp-adopt-core (PR B)

**Dependencies:** U3 (column rename complete).

**Goal:** Rename `rop3` references in `apps/etl/` to `people_id3`. Remove
the "p2p_rop3_resolution_deferred" conflict-type writes, since DT already
carries `people_id3` in `fpg_submission_data` (the resolution becomes a
direct read, not a deferred lookup).

**Files (per the 11-reference scan):**
- `apps/etl/src/jp_adopt_etl/orchestrator.py` — lines 399-403 (the deferred
  comment + the conflict-type write); rename remaining rop3 refs
- `apps/etl/src/jp_adopt_etl/dt_source.py` — any rop3 refs (likely the
  `iter_p2p` function or surrounding mappers)
- `apps/etl/tests/test_*.py` — fixture updates

**Approach:**
- Mechanical rename of `rop3` → `people_id3` in the ETL.
- The deferred-resolution comment block at `orchestrator.py:399-403` is
  removed. The note now is "people_id3 read directly from
  fpg_submission_data" — but the actual resolver-path build is out of scope
  for this plan (per Scope Boundaries; see related issue if filed).
- Keep `migration_conflicts` row writes for *other* conflict types; only
  drop the `p2p_rop3_resolution_deferred` row write.

**Patterns to follow:**
- ETL conventions in `apps/etl/src/jp_adopt_etl/`

**Test scenarios:**
- Existing ETL tests pass after rename.
- The "p2p_rop3_resolution_deferred" conflict-type is no longer written
  (no orchestrator path produces this string).
- If a new test demonstrating the people_id3 read path exists, it asserts
  `adopter_interest.people_id3` gets populated from
  `fpg_submission_data.people_id3` — but building the full resolver is
  follow-up, so likely just the rename ships here.

**Verification:** ETL tests pass; `grep -rn 'rop3' apps/etl/` returns
zero results.

---

## System-Wide Impact

| Surface | Impact | Status |
|---|---|---|
| `apps/api` Python | `rop3` → `people_id3` across models, schemas, intake, matching, ETL | Renamed (U3–U5, U7, U8) |
| Alembic migrations | New revision 0021 (destructive); 0020's index becomes redundant and is dropped | U3 |
| `packages/contracts` | TS types regenerated from OpenAPI; field renames flow through | U5 |
| `apps/web` | 24 refs renamed; user-facing display unchanged | U6 |
| `apps/etl` | 11 refs renamed; deferred conflict-type write removed | U8 |
| Outbox events | `submission_received` schema bumped to `jp.adopt.v2`; payload `fpg_selections` shape changes | U5 |
| Forms repo schema | `fpg_cache` gains `rop3` column (nullable, backfilled by refresh cron) | U1 |
| Forms repo API | New `GET /api/v1/people-groups/export` endpoint | U2 |
| External: `JOSHUA_PROJECT_API_KEY` | Removed from core's config; remains in forms (where it's actually needed) | U7 |
| Core's `scripts/sync_fpg.py` | Rewritten to pull from forms, not JP API | U7 |

**Cross-repo coordination:** PR A (forms) must merge and deploy + one
refresh-fpg cycle complete before PR B (core) deploys. PR B's mirror sync
fails fast if the forms export endpoint isn't reachable or returns rows
without `rop3` (core ignores rop3 but the absence signals incomplete
deploy).

---

## Risk Analysis & Mitigation

### R-1. Matching domain regression

**Risk:** U4 renames in the matching algorithm — core's crown jewel. A
mechanical rename should be safe, but matching is the highest-consequence
code path: a regression here breaks adopter↔facilitator pairing.

**Mitigation:**
- Run the full matching test suite (`uv run pytest tests/test_matching*.py`)
  after U4 and after each subsequent unit. Treat any failure as a stop-the-
  line investigation, not a "fix the test."
- The rename is opaque-key substitution (verified: `_fpg_affinity_score` is
  `1.0 if rop3 in covered_rop3s else 0.0` — no semantics in the value).
  Behavior should be identical.
- The `Execution note` on U4 explicitly calls out the test suite as the
  guardrail.

### R-2. Cross-repo sequencing failure

**Risk:** PR B (core) deploys before PR A (forms) deploys + refreshes. The
mirror sync would fail (endpoint missing) or pull rows with `rop3=null`.

**Mitigation:**
- PR ordering is explicit (KTD-7) and the plan owner controls the merge
  order.
- Core's mirror sync is run **once after deploy** as a deliberate operator
  step (`uv run python -m jp_adopt_api.scripts.sync_fpg`), not automatically
  on boot — operator confirms forms is ready before running.
- Add a startup check (optional): on first request to an intake endpoint
  if `fpg` is empty, log a clear error pointing to the mirror sync runbook.

### R-3. Outbox v2 breaks a downstream subscriber

**Risk:** Bumping `schema_version` to `jp.adopt.v2` could break a subscriber
that parses `fpg_selections[].rop3`.

**Mitigation:**
- Audit current outbox subscribers before merging (worker drains: drip
  engine, daily digest, future analytics). At the time of writing, no
  subscriber parses the `rop3` field (verified by grep across `apps/worker`
  and any external consumers).
- The version bump is the honest signal; downstream consumers see a clear
  version in the event and can branch on it.
- If any subscriber does emerge mid-flight, it stays on v1 events
  (everything before the deploy) and parses v2 events with the new shape
  (everything after).

### R-4. Forms cache refresh fills `rop3` slower than expected

**Risk:** The refresh-fpg cron runs daily (or per schedule); existing
~17K cache rows take a refresh cycle to fill `rop3`. Between U1 deploy and
the next refresh, the column is nullable and partially populated.

**Mitigation:**
- U2's export endpoint can either (a) wait for `rop3` to be non-null for
  all rows before returning (preferred — fails fast if cache is mid-
  refresh) or (b) return rows with `rop3=null` and let core ignore them
  (acceptable since core ignores `rop3` anyway after U3).
- Trigger a manual cache refresh after U1 deploys, before U2 is exposed
  for consumption.

### R-5. The destructive migration loses local dev data

**Risk:** Developers running U3 locally lose any local `fpg` /
`adopter_interest` / `facilitator_fpg_coverage` data (including the 3,481
real rows from running the bridge's `sync_fpg.py`).

**Mitigation:**
- Per KTD-4, this is intentional (greenfield prod). Document in the
  migration's docstring and in the U7 sync's docstring that operators
  should re-run the mirror sync after applying 0021.
- Add a one-liner to `scripts/seed-local.sh` (or the dev-stack runner) that
  reminds developers to run `sync_fpg` after migrations.

---

## Verification Strategy

### Autonomous Execution Boundary

This plan is designed to be safe for an unattended runner (cursor-agent,
CI, etc.) to execute end-to-end through the "PRs opened, ready for review"
state. Live-environment steps are explicitly **out of scope for autonomous
execution** and require human action.

**Autonomous (runner does all of this):**
- All code changes across both repos.
- Local schema migrations against developer / CI databases.
- All unit, integration, and contract-regen tests; lint.
- Opening both PRs (PR A in jp-adopt-forms, PR B in jp-adopt-core).
- Cross-referencing PRs and issue #77 in PR descriptions.

**Human-gated (do not automate):**
- Merging either PR. Merge order is A then B (per KTD-7); the merger
  enforces it.
- Deploying either repo. No autonomous step should call `deploy.yml`
  workflows, `gh workflow run`, or any production / staging endpoint.
- Triggering or waiting on the forms `refresh-fpg` cron against a live
  cache.
- Running `uv run python -m jp_adopt_api.scripts.sync_fpg` against the
  deployed forms endpoint (only the mocked / locally-run variant is
  autonomous-safe).
- Real-form intake smoke tests against any deployed pipeline.
- Hitting any URL containing `joshuaproject.net`, deployed Azure endpoints,
  or staging hostnames from automation. The JP-API live call is
  human-gated even though the existing `sync_fpg.py` script invokes it,
  because that script is being removed in U7.

**What the runner should produce as the stopping state:**
- Both PRs open, CI green, descriptions reference issue #77 and each
  other, with an explicit `## Post-merge / Live-env verification` section
  in each PR body listing the human-gated steps below under "Plan-level
  verification" so the merger has a checklist.

### Per-unit verification

Captured in each unit's `Verification` field. Steps that depend on a live
environment are marked **(human-gated)** there.

### Plan-level verification

**Autonomous (CI / runner — gate for "PRs ready to merge"):**

- `alembic heads` → 0021 (head); the 0021 migration applies cleanly on a
  fresh DB and on a DB at 0020 (forward) and reverses cleanly (downgrade).
- `grep -rn 'rop3' apps/api/src apps/web apps/etl packages/contracts`
  returns zero matches in core.
- `uv run pytest apps/api/tests/` exits 0; matching tests specifically
  show zero regressions.
- `pnpm lint:web` and `pnpm contracts:generate` produce a clean diff with
  the generated artifact committed.
- `JOSHUA_PROJECT_API_KEY` no longer referenced anywhere in core (config,
  scripts, tests).
- `_resolve_rop3` no longer exists in `apps/api/src/jp_adopt_api/routers/intake.py`.
- Both PR descriptions reference issue #77 and the bridge PRs (#71,
  `jp-adopt-forms#149`), and carry an explicit "Post-merge / live-env
  verification" checklist matching the human-gated steps below.

**Human-gated (operator, post-merge + post-deploy):**

1. **Forms repo (PR A) merged + deployed.**
   - One refresh-fpg cycle complete in the deployed env (manual trigger or
     scheduled run).
   - `SELECT COUNT(*) FROM fpg_cache WHERE rop3 IS NULL AND is_frontier =
     TRUE` returns 0 in the deployed DB.
   - `curl -H "Authorization: Bearer …" $FORMS_URL/api/v1/people-groups/export`
     returns ~3,481 rows of the expected shape.

2. **Core repo (PR B) merged + deployed**, in that order.
   - Operator runs `uv run python -m jp_adopt_api.scripts.sync_fpg` once
     post-deploy against the deployed forms endpoint; `SELECT COUNT(*) FROM
     fpg` in the core DB matches the forms-cache frontier count
     (~3,481).
   - Real-form intake smoke test: submit an adoption form with
     `people_id3=10375` against the deployed pipeline; confirm an
     `adopter_interest` row exists in core with `people_id3='10375'` and no
     translation/lookup step in the request path.

3. **Bridge teardown verified in production:**
   - PRs #71 / `jp-adopt-forms#149` remain visible in history as the bridge
     (not reverted) — this plan removed the bridge mechanics they
     introduced, not the bridge itself.
   - `JOSHUA_PROJECT_API_KEY` secret can be rotated / removed from core's
     deployed environment (Azure Key Vault entry decommissioned).

**Effort target:** 1–1.5 focused days. The riskiest unit (U4, matching
rename) is mechanical; the longest unit (U3, the migration) is
destructive-but-greenfield. No unit is at risk of exceeding the target
provided U1+U2 (forms) merge cleanly first.

---

## Deferred to Implementation

These resolve at execution time, not in this plan:

- **Forms repo auth-key shape for the export endpoint** — scoped api-key
  (`api-key.ts`/`api-scopes.ts`) vs single-purpose env var. Decide based on
  the existing scope infrastructure's ergonomics at U2 implementation time.
- **Whether to drop `migration 0020`'s `ix_fpg_people_id3` index** in U3's
  migration (since it becomes redundant when `people_id3` is the PK), or
  leave it as a no-op duplicate. Recommended: drop it explicitly in 0021's
  migration body.
- **Forms refresh-cron trigger timing** — whether U1 explicitly triggers a
  cache refresh post-deploy or relies on the next scheduled run. Probably
  manual trigger is cleaner.

---

## Deferred to Follow-Up Work

- **DT contact-import resolver path build-out** — the rename in U8
  unblocks this, but actually wiring the people_id3 → adopter_interest
  resolution from DT's `fpg_submission_data` is its own effort. File as a
  follow-up issue once this plan merges.
- **Forms-cache → core sync cron** — U7 is a script invoked manually
  post-deploy. Scheduling it (worker job or external cron) for ongoing
  freshness is a follow-up.
- **Out-of-band `rop3` consumers, if any are discovered** — keep notes in
  the PR description if anything surfaces during implementation.
