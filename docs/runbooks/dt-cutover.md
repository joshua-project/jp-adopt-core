# DT → jp-adopt-core cutover runbook (U13 execution)

Sequence Joel runs Saturday 5/23 14:00-18:00 ET to move the team from
Disciple.Tools to jp-adopt-core as the system of record. The ETL itself
is built in U9 (see `apps/etl/runbook.md`); this runbook is the
operator-facing wrapper.

## Pre-cutover (Friday evening, 5/22)

1. Confirm staging Postgres is at the latest migration (`0023`):
   ```bash
   uv run --package jp-adopt-api alembic current
   # expect: 0023 (head)
   ```
   `0022` adds `contacts.phone`; `0023` adds `adopter_interest.source_system`/
   `source_id` plus the partial unique index needed for ETL idempotency.
2. Dry-run against the latest DT MySQL snapshot:
   ```bash
   uv run --package jp-adopt-etl dt-etl \
     --mysql-url "mysql+pymysql://${DT_MYSQL_USER}:${DT_MYSQL_PASS}@${DT_MYSQL_HOST}:3306/${DT_MYSQL_DB}" \
     --postgres-url "postgresql+psycopg2://${JP_ADOPT_USER}:${JP_ADOPT_PASS}@${PG_HOST}:5432/jp_adopt" \
     --table all \
     --mode dry_run \
     --verbose 2>&1 | tee /tmp/dt-etl-dryrun-$(date +%Y%m%d).log
   ```
   Expect exit code 0 and a per-table summary. Any `UnmappedStatusError`
   means a new DT enum value has appeared; add a mapping to
   `apps/etl/src/jp_adopt_etl/mappers/status.py` and re-run.
3. Spot-check 5 known contacts against the staging DB. Pick contacts you
   manually edited recently in DT — their `display_name`, `email_normalized`,
   and lifecycle status should round-trip:
   ```sql
   SELECT source_id, display_name, party_kind, adopter_status,
          facilitator_status, email_normalized, phone, origin
   FROM contacts WHERE source_system = 'dt' ORDER BY created_at DESC LIMIT 5;
   ```
   Also verify the related rows landed:
   ```sql
   SELECT COUNT(*) FROM contact_profile WHERE contact_id IN
     (SELECT id FROM contacts WHERE source_system = 'dt');
   SELECT COUNT(*) FROM adopter_interest WHERE source_system = 'dt';
   SELECT COUNT(*) FROM contact_assignment;
   SELECT COUNT(*) FROM activity_log WHERE source_system = 'dt';
   ```
4. Triage `migration_conflicts` by `conflict_type`:
   ```sql
   SELECT conflict_type, COUNT(*) FROM migration_conflicts GROUP BY 1;
   ```
   Expected conflict types (informational, not blocking):
   - `duplicate_email` — DT permits multiple contacts to share an email;
     the new system's partial unique index does not. The ETL keeps the
     contact and clears the duplicate's `email_normalized`. Reconcile
     post-cutover by picking the canonical contact per email.
   - `assignee_no_subject` — DT user has no B2C signin yet, so
     `assigned_to` can't resolve to a `user_subject_id`. Re-run after the
     assignee signs into jp-adopt-core for the first time.
   - `fpg_not_found` — `fpg_submission_data.peopleId3` not in the local
     `fpg` table. Seed the FPG and re-run.
   - `unmapped_status:*` — a DT enum value missing from `status.py`. Add
     a mapping and re-run.
   - `local_modified_after_import` — staff edited the contact in
     jp-adopt-core after a prior import; the ETL preserved the local edit.
5. Rehearse the rollback from Step "If cutover fails" below in staging.

## Cutover (Saturday 5/23 14:00-18:00 ET)

### 14:00 — announce write freeze
- Slack/email: "DT going read-only for cutover 14:00-18:00 ET. Use
  jp-adopt-core for new entries starting 18:00."
- WordPress admin: remove `edit_posts` / `delete_posts` capabilities from
  all non-admin roles. Document the role-change in DT's audit log.

### 14:15 — take final MySQL snapshot
- Run the snapshot tool (mysqldump or your replica's snapshot command).
- Confirm snapshot is consistent (no transactions in flight).

### 14:30 — delta ETL with watermark
```bash
# Get the watermark from yesterday's run:
LAST_WATERMARK=$(psql -h ${PG_HOST} -U ${JP_ADOPT_USER} -d jp_adopt -t -A \
  -c "SELECT source_max_modified_at FROM etl_run WHERE table_name='contacts' AND mode='production' ORDER BY started_at DESC LIMIT 1")

uv run --package jp-adopt-etl dt-etl \
  --mysql-url "mysql+pymysql://..." \
  --postgres-url "postgresql+psycopg2://..." \
  --table all \
  --mode production \
  --watermark "${LAST_WATERMARK}" \
  --verbose 2>&1 | tee /tmp/dt-etl-cutover-$(date +%Y%m%d-%H%M).log
```

### 15:00 — verification queries
```sql
-- Row counts vs MySQL source
SELECT COUNT(*) FROM contacts WHERE source_system = 'dt';
SELECT COUNT(*) FROM contact_profile WHERE contact_id IN
  (SELECT id FROM contacts WHERE source_system = 'dt');
SELECT COUNT(*) FROM adopter_interest WHERE source_system = 'dt';
SELECT COUNT(*) FROM contact_assignment;
SELECT COUNT(*) FROM activity_log WHERE source_system = 'dt';
SELECT COUNT(*) FROM staff_identity_link WHERE source_system = 'dt';

-- Activity_log by kind (notes + emails + field changes)
SELECT kind, COUNT(*) FROM activity_log WHERE source_system = 'dt'
  GROUP BY 1 ORDER BY 2 DESC;

-- Status distribution sanity check
SELECT adopter_status, COUNT(*) FROM contacts WHERE source_system = 'dt'
  GROUP BY 1 ORDER BY 2 DESC;
SELECT facilitator_status, COUNT(*) FROM contacts
  WHERE source_system = 'dt' AND facilitator_status IS NOT NULL
  GROUP BY 1 ORDER BY 2 DESC;

-- Recent ETL run summary
SELECT table_name, mode, rows_in, rows_out_inserted, rows_out_updated,
       rows_out_skipped, rows_in_conflict, errors,
       started_at, ended_at
FROM etl_run
WHERE started_at > now() - interval '1 hour'
ORDER BY started_at DESC;
```

Compare contact counts against DT MySQL:
```sql
-- in MySQL
SELECT COUNT(*) FROM wp_posts WHERE post_type = 'contacts'
  AND post_status NOT IN ('trash', 'auto-draft');
SELECT COUNT(*) FROM wp_dt_activity_log
  WHERE action = 'field_update' AND object_type = 'contacts';
```
The Postgres contact count should match (modulo any rows in
`migration_conflicts` flagged as `local_modified_after_import`).
`activity_log` (kind='field_change') should match `wp_dt_activity_log`
minus any `rows_out_skipped` (contact not yet imported when the audit row
was visited).

### 17:00 — review migration_conflicts
```sql
SELECT conflict_type, COUNT(*) FROM migration_conflicts GROUP BY 1;
SELECT * FROM migration_conflicts
  WHERE conflict_type LIKE 'unmapped%' ORDER BY detected_at DESC LIMIT 20;
```
- `unmapped_status:*` rows mean DT enum values without a mapping. Decide
  per case whether to map them post-cutover.
- `local_modified_after_import` rows mean a staff member edited a
  contact in the new system AFTER Friday's dry run; the cutover skipped
  those rows. Decide per case.

### 17:30 — flip authoritative source flag
- App-level: set the feature flag (TBD; not yet implemented) so the
  staff UI displays "jp-adopt-core is canonical" in the nav.
- DT: leave read-only for at least 2 weeks before final archive.

### 18:00 — announce cutover complete
- Slack/email: "jp-adopt-core is canonical from now on. DT remains
  read-only until further notice. Report any data discrepancies to Joel."

## If cutover fails

### Bug surfaces during ETL run
1. Stop the ETL (`Ctrl-C` if interactive, or `kill <pid>` if backgrounded).
2. The ETL writes inside one transaction per table — if it crashes
   mid-table, that table's partial writes were committed (PostgreSQL's
   default). Check `etl_run.errors > 0` for the offending row.
3. Reconcile manually OR re-run with the same watermark — the `ON
   CONFLICT DO UPDATE` is idempotent and will pick up where it left off.
4. If the bug is the mapper raising on a new DT value, add the mapping
   to `status.py`, rebuild (`uv sync --package jp-adopt-etl`), and rerun.

### Verification fails after cutover
1. **Don't panic.** DT is still read-only and contains the canonical
   pre-cutover data.
2. Roll back: revert the feature flag flip; announce "rollback in
   progress" in Slack.
3. Capture the diagnostic data:
   ```sql
   SELECT * FROM etl_run ORDER BY started_at DESC LIMIT 5;
   SELECT conflict_type, COUNT(*) FROM migration_conflicts GROUP BY 1;
   ```
4. Wipe the DT-imported rows from Postgres (preserves locally created
   contacts):
   ```sql
   DELETE FROM activity_log WHERE source_system = 'dt';
   DELETE FROM adopter_interest WHERE source_system = 'dt';
   DELETE FROM contact_assignment WHERE contact_id IN
     (SELECT id FROM contacts WHERE source_system = 'dt');
   DELETE FROM contact_profile WHERE contact_id IN
     (SELECT id FROM contacts WHERE source_system = 'dt');
   DELETE FROM contacts
     WHERE source_system = 'dt' AND local_modified_after_import = false;
   DELETE FROM staff_identity_link WHERE source_system = 'dt';
   DELETE FROM etl_deleted_in_source WHERE source_system = 'dt';
   DELETE FROM migration_conflicts
     WHERE source_system = 'dt' AND detected_at > now() - interval '6 hours';
   ```
5. Diagnose the root cause Saturday evening; reschedule the cutover for
   Sunday or postpone until Amy is back.

### Abort protocol (solo cutover, time-pressured)
If Joel hits a blocker and needs to abort fast (e.g., MySQL snapshot
corrupt, Postgres unreachable, ETL panics with no clear remediation):

1. **Hour 0-1 of the window:** abort outright. Announce "cutover delayed,
   investigating" and reschedule.
2. **Hour 2-3 of the window:** if Postgres is healthy and DT is still
   read-only, attempt one more delta ETL with `--mode production --verbose`.
3. **Hour 3-4 of the window:** at the 17:30 flip moment, if verification
   queries pass, commit. Otherwise abort.

Don't attempt cleverness under time pressure. The Friday rehearsal is
where unusual configurations get debugged; Saturday is execution-only.

## Post-cutover (Sunday 5/24 evening)

- Re-run verification queries; counts should match Saturday's.
- Resolve `migration_conflicts` rows (manual reconciliation).
- Send Amy the quick-start link.

---

For ETL implementation details, see `apps/etl/runbook.md`.
For the build plan, see the Amy-return build plan in the knowledge base.
