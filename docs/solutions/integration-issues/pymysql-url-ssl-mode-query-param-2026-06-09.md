---
title: pymysql rejects `?ssl_mode=REQUIRED` in SQLAlchemy connection URL
date: 2026-06-09
category: integration-issues
module: apps/etl
problem_type: integration_issue
component: background_job
severity: high
symptoms:
  - "Container Apps Job first scheduled execution failed at SQLAlchemy connect time"
  - "`Connection.__init__() got an unexpected keyword argument 'ssl_mode'`"
  - "ETL cron container started cleanly but could not establish DT MySQL connection"
  - "Composed DT MySQL URL contained `?ssl_mode=REQUIRED` query parameter"
root_cause: wrong_api
resolution_type: config_change
tags:
  - etl
  - mysql
  - pymysql
  - sqlalchemy
  - azure-mysql-flex
  - github-actions
  - container-apps-job
  - ssl
related_components:
  - tooling
  - database
---

# pymysql rejects `?ssl_mode=REQUIRED` in SQLAlchemy connection URL

## Problem

The hourly DT ETL Azure Container Apps Job failed at its first scheduled
execution because the composed SQLAlchemy connection URL contained the
query parameter `?ssl_mode=REQUIRED`. pymysql does not recognize
`ssl_mode` as a connection kwarg, so SQLAlchemy raised
`Connection.__init__() got an unexpected keyword argument 'ssl_mode'`
at the first DB connect — before any data import work began.

## Symptoms

- `Connection.__init__() got an unexpected keyword argument 'ssl_mode'`
  raised at SQLAlchemy connect-time on the Job's first scheduled
  execution.
- Container Apps Job container started cleanly and the entrypoint shell
  script ran — the failure was at the first MySQL connect, not at boot
  or auth.
- The MySQL CLI (`mysql --ssl-mode=REQUIRED`) and the Azure Portal both
  suggest `ssl_mode` is valid syntax, masking the driver-specific
  incompatibility.
- No local repro before deploy — the GitHub Actions CI does not connect
  to prod MySQL, so this class of bug never surfaces there.

## What Didn't Work

- Assumed it was an Azure IAM/managed-identity issue on the Job — wrong,
  the container reached the DB connect step cleanly with full network
  access.
- Suspected the DT password contained a special character corrupting the
  URL — wrong, the URL was correctly percent-encoded by the
  `urllib.parse.quote` step in the deploy workflow.
- Considered adding `connect_args={"ssl": ...}` to SQLAlchemy as
  required — possibly defensive but not actually needed; Azure MySQL
  Flexible Server enforces SSL server-side and pymysql negotiates the
  upgrade automatically.

## Solution

Drop `?ssl_mode=REQUIRED` from the URL composition in
`.github/workflows/deploy.yml`.

**Before:**

```yaml
DT_MYSQL_URL="mysql+pymysql://${DT_MYSQL_USERNAME}:${PW_ENC}@${DT_MYSQL_HOST}:3306/${DT_MYSQL_DATABASE}?ssl_mode=REQUIRED"
```

**After:**

```yaml
# `?ssl_mode=REQUIRED` is a MySQL CLI/connector flag — pymysql
# ignores it as a URL query param and SQLAlchemy then errors
# with `Connection.__init__() got an unexpected keyword
# argument 'ssl_mode'`. Azure MySQL Flex enforces SSL
# server-side regardless; pymysql negotiates it automatically.
DT_MYSQL_URL="mysql+pymysql://${DT_MYSQL_USERNAME}:${PW_ENC}@${DT_MYSQL_HOST}:3306/${DT_MYSQL_DATABASE}"
```

Also update the **live ACA Job secret out-of-band** so the next
scheduled fire doesn't fail before the workflow re-applies the secret
on its next deploy:

```bash
az containerapp job secret set \
  --name jp-adopt-etl-cron-production \
  --resource-group rg-jp-adopt-core-production \
  --secrets "dt-mysql-url=mysql+pymysql://${USER}:${PW}@${HOST}:3306/${DB}"
```

## Why This Works

- `pymysql.connections.Connection.__init__` accepts SSL via the `ssl`
  kwarg (a dict), not via a string `ssl_mode` param. SQLAlchemy passes
  URL query params directly to the driver as kwargs, so any unknown
  param surfaces as a `TypeError`-style init failure.
- Azure MySQL Flexible Server enforces SSL at the server level — it
  rejects non-SSL connections during the protocol handshake, and pymysql
  transparently upgrades to SSL when the server requires it. No
  client-side opt-in is needed.
- The `mysql --ssl-mode=REQUIRED` CLI flag exists because the CLI
  doesn't default to SSL negotiation. pymysql is a different code path
  with different defaults and a different config surface — the two are
  not interchangeable just because the parameter name looks similar.

## Prevention

- **Don't translate CLI flags to URL query params blindly.** Driver URL
  contracts and CLI flag surfaces are independent. Look up the
  driver-specific URL spec when crossing between them. *(In this case
  the `dt-mysql-access` auto memory's docker invocation explicitly uses
  `--ssl-mode=REQUIRED` as a CLI flag — almost certainly the source of
  the broken URL composition.)*
- **Pre-flight the connection URL locally** with a one-shot `SELECT 1`
  before pushing to a Container Apps Job. CI doesn't reach prod MySQL,
  so this class of bug only surfaces at deploy-time without the
  pre-flight.
- **Add a sentinel `SELECT 1` smoke check** at the start of
  `apps/etl/src/jp_adopt_etl/orchestrator.py` so driver errors surface
  immediately rather than at first batch read.
- **Document the URL format** explicitly in `apps/etl/runbook.md` and
  `docs/runbooks/dt-cron-sync.md` — call out the no-`ssl_mode` rule
  with a citation to this doc.
- **Optional defensive layer:** pass `connect_args={"ssl":
  {"check_hostname": True}}` to `open_engine` in
  `apps/etl/src/jp_adopt_etl/dt_source.py` instead of relying solely on
  Azure's server-side enforcement. Not blocking, but useful if the ETL
  ever needs to run against a non-Azure MySQL where server-side
  enforcement isn't a given.

## Related Issues

- PR #103 — the fix
  (`fix(deploy): drop ?ssl_mode=REQUIRED from DT MySQL URL`,
  commit `fd8fb0b`).
- PR #102 — preceding deploy fix
  (`fix(deploy): pass ACA env's full resource ID for the ETL cron
  job`); same workflow file, different root cause.
- Sibling deploy-workflow learning:
  [`docs/solutions/logic-errors/aca-wait-for-healthy-active-revision-jmespath-2026-05-27.md`](../logic-errors/aca-wait-for-healthy-active-revision-jmespath-2026-05-27.md)
  — different problem (JMESPath active-revision matching) but same
  `.github/workflows/deploy.yml` file, worth knowing when editing
  adjacent deploy steps.
