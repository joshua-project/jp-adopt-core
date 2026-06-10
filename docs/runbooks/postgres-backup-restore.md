# Postgres backup + restore drill runbook

Production Postgres for `jp-adopt-core` is an **Azure Database for
PostgreSQL — Flexible Server** instance. Azure takes automated backups
on a schedule; restore is **point-in-time, to a new server** (never
in-place). This runbook covers the drill that proves the restore path
actually works, and the day-of procedure when a real incident demands
it.

Run a drill **before the DT cutover** (#91 Phase 2 requirement) and
**quarterly** thereafter. A backup you've never restored is not a
backup.

## Production backup model

Verify the current posture before relying on this runbook:

```bash
az postgres flexible-server show \
  --resource-group rg-jp-shared-production \
  --name jp-postgresql-production \
  --query "{retentionDays:backup.backupRetentionDays, \
            geoRedundant:backup.geoRedundantBackup, \
            earliestRestore:backup.earliestRestoreDate, \
            ha:highAvailability.mode, \
            version:version}" \
  -o table
```

As of 2026-06-09:

| Setting | Value | Implication |
|---|---|---|
| `backupRetentionDays` | 7 | Restore window is the last 7 days. Older data is gone. |
| `geoRedundantBackup` | Disabled | A regional outage in Central US loses every backup. |
| `highAvailability.mode` | Disabled | Compute restart = downtime (no warm standby). |
| `version` | 16 | Restore must target Postgres 16. |
| `earliestRestoreDate` | rolling 7-day window | Use this as the lower bound when picking a restore point. |

The shared production server hosts every JP database, not just
`jp_adopt`: `adopt_forms`, `n8n`, `link_hub`, `prayer_map`. **A restore
brings the whole instance**; you cannot restore just `jp_adopt`. That
is a feature for disaster recovery and a constraint for partial
recovery (covered under "Recovering a single table" below).

Non-default Postgres parameters that are baked into the server image
and persist through restore: `azure.extensions=PGCRYPTO,PG_TRGM`,
`data_checksums=on`, `default_toast_compression=lz4`,
`archive_mode=always`. These do not need to be re-applied on the
restored server.

## Drill procedure

The drill takes ~30 minutes and costs ~$1 (a B1ms server for an hour).
Do it from your operator machine — Azure CLI handles everything.

### 1. Decide on a restore point

```bash
# The "earliest restore" floor:
az postgres flexible-server show \
  --resource-group rg-jp-shared-production \
  --name jp-postgresql-production \
  --query "backup.earliestRestoreDate" -o tsv

# Pick a point a few minutes in the past (Azure rejects future timestamps):
RESTORE_POINT=$(date -u -v-5M '+%Y-%m-%dT%H:%M:%SZ')   # macOS
# RESTORE_POINT=$(date -u -d '5 minutes ago' '+%Y-%m-%dT%H:%M:%SZ')  # GNU
echo "Restoring to: $RESTORE_POINT"
```

For a **drill** pick `5 minutes ago`. For a **real incident** pick the
last good timestamp you can identify — the verification queries below
help confirm the restored server is at the right point.

### 2. Trigger the point-in-time restore

```bash
RESTORED_NAME="jp-postgresql-drill-$(date +%Y%m%d-%H%M)"

az postgres flexible-server restore \
  --resource-group rg-jp-shared-production \
  --name "$RESTORED_NAME" \
  --source-server jp-postgresql-production \
  --restore-time "$RESTORE_POINT" \
  --no-wait
```

The restore typically takes 10–20 minutes on a 32 GB server.
`--no-wait` returns immediately; poll state with:

```bash
until [ "$(az postgres flexible-server show \
    --resource-group rg-jp-shared-production \
    --name "$RESTORED_NAME" \
    --query 'state' -o tsv 2>/dev/null)" = "Ready" ]; do
  echo "still restoring at $(date)…"
  sleep 60
done
echo "READY"
```

### 3. Open the firewall to your operator IP

The restored server inherits the source's firewall rules. If your
current IP isn't already on the source, add it temporarily:

```bash
MY_IP=$(curl -s https://api.ipify.org)
az postgres flexible-server firewall-rule create \
  --resource-group rg-jp-shared-production \
  --name "$RESTORED_NAME" \
  --rule-name "drill-$(date +%Y%m%d)" \
  --start-ip-address "$MY_IP" \
  --end-ip-address "$MY_IP"
```

### 4. Verify the restored database

Use the same `jp_adopt_migrator` password as the source (the restore
preserves all role passwords). Pull it from 1Password:

```bash
MIGRATOR_PW=$(op item get "Adopt Core - Production" \
  --vault "JP Adopt Platform" \
  --account joshuaproject.1password.com \
  --fields migrator-password --reveal)

RESTORED_FQDN=$(az postgres flexible-server show \
  --resource-group rg-jp-shared-production \
  --name "$RESTORED_NAME" \
  --query 'fullyQualifiedDomainName' -o tsv)

PGPASSWORD="$MIGRATOR_PW" docker run --rm -i \
  -e PGPASSWORD postgres:16-alpine \
  psql "postgresql://jp_adopt_migrator@${RESTORED_FQDN}:5432/jp_adopt?sslmode=require" \
  -c "SELECT version();" \
  -c "SELECT version_num FROM alembic_version;" \
  -c "SELECT 'contacts' AS table, COUNT(*) FROM contacts
      UNION ALL SELECT 'matches', COUNT(*) FROM matches
      UNION ALL SELECT 'outbox', COUNT(*) FROM outbox
      UNION ALL SELECT 'etl_run', COUNT(*) FROM etl_run;" \
  -c "SELECT MAX(created_at) AS latest_contact FROM contacts;" \
  -c "SELECT MAX(emitted_at) AS latest_outbox FROM outbox;"
```

What to check:
- **Engine version**: matches the source (16.x).
- **Alembic head**: matches the source's current head (`0026` as of
  2026-06-09; see `apps/api/alembic/versions/` for the current head).
- **Row counts**: within reasonable delta of the source (a 5-minute-ago
  restore should be within a handful of rows on `contacts`, `outbox`,
  `etl_run`).
- **Latest timestamps**: `MAX(created_at) FROM contacts` and
  `MAX(emitted_at) FROM outbox` should be close to your restore point.
  This is the **single most important check** — it proves the restore
  point you asked for is what you got.

Run the same query against the source for the delta comparison:

```bash
SOURCE_PW="$MIGRATOR_PW" PGPASSWORD="$SOURCE_PW" docker run --rm -i \
  -e PGPASSWORD postgres:16-alpine \
  psql "postgresql://jp_adopt_migrator@jp-postgresql-production.postgres.database.azure.com:5432/jp_adopt?sslmode=require" \
  -c "SELECT 'contacts' AS table, COUNT(*) FROM contacts
      UNION ALL SELECT 'matches', COUNT(*) FROM matches
      UNION ALL SELECT 'outbox', COUNT(*) FROM outbox
      UNION ALL SELECT 'etl_run', COUNT(*) FROM etl_run;"
```

### 5. Tear down the drill server

```bash
az postgres flexible-server delete \
  --resource-group rg-jp-shared-production \
  --name "$RESTORED_NAME" \
  --yes
```

**Do not skip this step.** A B1ms server costs ~$15/month even when
idle. Drill servers without a cleanup line in the runbook accumulate
silently.

Also delete the temporary firewall rule on the source if you added one
(`az postgres flexible-server firewall-rule delete`).

## Real-incident playbook

When this is not a drill, the procedure is the same but the decisions
are different:

| Decision | Drill | Real incident |
|---|---|---|
| Restore point | `5 minutes ago` | Last known-good state — usually a timestamp just before the corrupting event |
| Naming | `jp-postgresql-drill-…` | `jp-postgresql-restore-…` (so monitoring + IaC drift checks treat it as the recovery target, not a drill) |
| Application cutover | Skip — drill only verifies the server | Repoint `jp-adopt-api` + worker + ETL + n8n + every consumer of the shared instance at the restored FQDN (see "Cutover to restored server" below) |
| Teardown | Drop immediately | Drop only after the new server has been promoted to be the source-of-truth and the old server has been retained for forensics + dropped after the post-incident review |

### Cutover to restored server

When the restored server has to **become** production:

1. Update `database-url` in `jp-adopt-core-kv-prod` Key Vault to point
   at the restored FQDN. See `docs/runbooks/secret-rotation.md` —
   `database-url` is one of the rotated secrets.
2. Force a revision restart on `jp-adopt-api` and `jp-adopt-worker` so
   the new connection string takes effect (the rotation runbook covers
   the exact command).
3. ETL: bump the connection string in `jp-adopt-etl-cron`'s ACA Job env
   vars.
4. Other shared-instance consumers (`n8n`, `link_hub`, `prayer_map`)
   need their own connection-string updates — coordinate with those
   teams.
5. Once stable, rename the restored server to a permanent name (Azure
   does not support rename — you do this by another PITR to the
   permanent name, or by letting the recovery-named server stay as the
   long-term server).
6. After 24+ hours of stability, drop the original `jp-postgresql-production`.
7. Update IaC in `jp-infrastructure` to reflect the new server as the
   source-of-truth — otherwise the next `terraform apply` will try to
   reverse the cutover.

### Recovering a single table

PITR brings the whole instance, but most incidents are not whole-server
corruption — they're "a developer ran the wrong UPDATE on `contacts`."
For partial recovery:

1. PITR-restore to a drill-named server at the timestamp just before
   the bad write.
2. Open both servers and `pg_dump` just the affected table from the
   restored server:
   ```bash
   pg_dump \
     "postgresql://jp_adopt_migrator@${RESTORED_FQDN}:5432/jp_adopt?sslmode=require" \
     --table contacts --data-only --column-inserts \
     > /tmp/contacts-recovered-$(date +%Y%m%d-%H%M).sql
   ```
3. Surgical merge into prod — usually a `TRUNCATE … RESTART IDENTITY`
   on a staging table + `INSERT … ON CONFLICT DO UPDATE` against
   production. The exact SQL is incident-specific; have a code review
   from a second engineer before running it against prod.
4. Drop the drill server.

If the affected table feeds downstream state (outbox events,
materialized projections), the partial recovery also has to replay or
reconcile those — Postgres doesn't know your application invariants.

## Cadence

- **Before any cutover-day work** (DT cutover, schema migration with
  data movement, role-split changes): the drill is a pre-flight.
- **Quarterly**: even when nothing is changing, the drill verifies
  Azure is still taking backups, retention is still 7 days, and the
  CLI commands above still work against the current `az` version.
- **After any change to backup config in IaC**: confirm the new
  retention or geo-redundancy actually applied.

Record each drill in `docs/runbooks/postgres-backup-restore-log.md`
(create on first drill; one line per drill: date, operator, restore
point, source row-count, restored row-count, time to ready, time to
verify, notes).

## Known limitations / config notes

- **Geo-redundancy is disabled.** A Central US regional outage destroys
  every backup. If JP wants cross-region survivability, enable
  `geoRedundantBackup` in `jp-infrastructure` Terraform — it doubles
  storage cost.
- **HA is disabled.** A planned Azure maintenance window or unplanned
  compute restart causes downtime. Acceptable for the current SLA but
  flag if it changes.
- **Shared instance.** Every database on the server shares the same
  backup window and restore. If `n8n` writes a lot more than `jp_adopt`
  the restore is paced by the heaviest writer.
- **Backup retention is 7 days.** If an incident is older than 7 days
  before it's discovered, the data is gone.

## Companion runbooks

- `docs/runbooks/secret-rotation.md` — for the `database-url` rotation
  step in the cutover-to-restored-server path.
- `docs/runbooks/deploy.md` — for the revision restart commands.
- `docs/runbooks/dt-cutover.md` — drill is a prerequisite for the DT
  cutover.
