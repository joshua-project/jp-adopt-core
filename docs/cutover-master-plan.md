---
title: "Cutover master plan — from Phase 1 testing to DT decommission"
type: index
status: active
created: 2026-06-10
---

# Cutover master plan

The single index for moving jp-adopt-core from "deployed" to "system of
record." Every step links to a runbook or plan that owns the depth;
this doc owns the **sequence and the gates**.

Tracks live in [#91](https://github.com/joshua-project/jp-adopt-core/issues/91).

---

## Where we are (2026-06-10)

- Production deployed, smoke green, every Phase 2 punch-list item has
  either a runbook, a plan, or both.
- DNS for `adoption.joshuaproject.net` still points at the legacy SWA.
- DT MySQL is still the system of record. No `source_system='dt'` rows
  exist in core's Postgres yet.
- Zero production alerts on adopt-core resources.

---

## Sequence (top to bottom = execution order)

### Gate A — Amy testing (Phase 1)

1. **Unstick the backlog**: close [#87](https://github.com/joshua-project/jp-adopt-core/issues/87) (raise `fpg_selections` cap + `INTAKE_MAX_BODY_BYTES`).
2. **Grant Amy access**: `staff_admin` via direct SQL ([`operator-handbook.md` Admin Tasks](./runbooks/operator-handbook.md#admin-tasks-no-ui-for-these-yet)) + Contact row with her email for digest delivery.
3. **Smoke the full journey**: walk [`user-testing-walkthrough.md`](./runbooks/user-testing-walkthrough.md) on production.
4. **Amy session**: she + 1-2 invited facilitators sign in, transition contacts, run a match, enroll in a drip. Reference [`amy-walkthrough.md`](./runbooks/amy-walkthrough.md).

**Exit Gate A when:** zero P0/P1 bugs from the session; at least one real outside-email form submission landed cleanly through the bridge.

### Gate B — Pre-cutover hardening

These can run in parallel with Gate A or before Gate C. None blocks Amy testing; all block DT cutover.

5. **DNS rebind**: SWA → ACA web. Operator runs [`dns-rebind.md`](./runbooks/dns-rebind.md). Closes [#82](https://github.com/joshua-project/jp-adopt-core/issues/82).
6. **API external:false**: operator runs [`api-external-false.md`](./runbooks/api-external-false.md). Closes [#90](https://github.com/joshua-project/jp-adopt-core/issues/90). Depends on step 5.
7. **Backup-restore drill**: operator runs [`postgres-backup-restore.md`](./runbooks/postgres-backup-restore.md). **Non-negotiable** before any data cutover.
8. **Monitoring + alerting**: execute [plan #131](./plans/2026-06-10-001-monitoring-alerting-plan.md). IaC PR in `jp-infrastructure`, fire-the-alert drill from operator machine. Closes monitoring half of #91 Phase 2.
9. **Performance load test**: execute [plan #132](./plans/2026-06-10-002-performance-load-test-plan.md). Seeds synthetic data on staging, runs k6 scenarios, decides per-cliff fix-or-accept. Closes load-test half of #91 Phase 2.

**Exit Gate B when:** all five items complete; no critical findings blocking cutover-day work.

### Gate C — DT contact import (the cutover itself)

10. **Staging dry-run**: per [DT import execution plan #122](./plans/2026-06-09-002-dt-contact-import-execution-plan.md) U2. Open MySQL firewall (operator IP), run `dt-etl --mode dry_run` against staging MySQL → local/staging Postgres. Triage any `unmapped_status:*` conflicts.
11. **Production snapshot dry-run**: same plan U3. Operator-led from your machine. Open prod MySQL firewall to operator IP; run dry-run against a fresh prod snapshot.
12. **Cutover window (Saturday)**: execute [`dt-cutover.md`](./runbooks/dt-cutover.md) step by step — 14:00 write freeze → 14:15 snapshot → 14:30 delta ETL → 15:00 verification → 16:00 flag flip → 17:00 announce. Plan #122 U4 references this runbook for the operator-side detail.
13. **Post-cutover verification**: plan #122 U5. Amy opens a real DT contact end-to-end in core.

**Exit Gate C when:** `contacts WHERE source_system='dt'` count matches MySQL row count modulo documented conflicts; Amy confirms a real contact navigates correctly.

### Gate D — DT decommission + SWA cleanup

14. **SWA decommission**: 14-day soak after Gate B step 5+6 → infra PR [jp-infrastructure#203](https://github.com/joshua-project/jp-infrastructure/issues/203) → adopt-core cleanup PR per [`dns-rebind.md` "Post-soak SWA decommission"](./runbooks/dns-rebind.md#post-soak-swa-decommission).
15. **DT final delta sync**: catch anything that landed in DT after Gate C's cutover. Same ETL, watermark-resumed.
16. **DT shutdown**: WordPress backup → DNS removal → hosting decommission → `dt-adoption-fields` plugin source archived.
17. **Communication plan**: partners + staff get the "DT retired" notice; quick-start link to [`quick-start.md`](./runbooks/quick-start.md).

**Exit Gate D when:** DT WordPress is offline; nobody notices.

---

## Reference index

**Runbooks** (operator depth — `docs/runbooks/`):

[`amy-walkthrough.md`](./runbooks/amy-walkthrough.md) ·
[`api-external-false.md`](./runbooks/api-external-false.md) ·
[`daily-digest.md`](./runbooks/daily-digest.md) ·
[`deploy.md`](./runbooks/deploy.md) ·
[`dns-rebind.md`](./runbooks/dns-rebind.md) ·
[`drip-engine.md`](./runbooks/drip-engine.md) ·
[`dt-cron-sync.md`](./runbooks/dt-cron-sync.md) ·
[`dt-cutover.md`](./runbooks/dt-cutover.md) ·
[`etl-postgres-role-split.md`](./runbooks/etl-postgres-role-split.md) ·
[`forms-data-import.md`](./runbooks/forms-data-import.md) ·
[`local-dev.md`](./runbooks/local-dev.md) ·
[`magic-link-side-car.md`](./runbooks/magic-link-side-car.md) ·
[`matching-algorithm-v1.md`](./runbooks/matching-algorithm-v1.md) ·
[`multi-idp-b2c.md`](./runbooks/multi-idp-b2c.md) ·
[`operator-handbook.md`](./runbooks/operator-handbook.md) ·
[`postgres-backup-restore.md`](./runbooks/postgres-backup-restore.md) ·
[`prod-smoke-walkthrough.md`](./runbooks/prod-smoke-walkthrough.md) ·
[`quick-start.md`](./runbooks/quick-start.md) ·
[`secret-rotation.md`](./runbooks/secret-rotation.md) ·
[`user-testing-walkthrough.md`](./runbooks/user-testing-walkthrough.md)

**Plans** (decision artifacts — `docs/plans/`):

[Rate limiting (#32)](./plans/2026-06-09-001-rate-limiting-plan.md) ·
[DT contact import execution](./plans/2026-06-09-002-dt-contact-import-execution-plan.md) ·
[Monitoring + alerting](./plans/2026-06-10-001-monitoring-alerting-plan.md) ·
[Performance + load test](./plans/2026-06-10-002-performance-load-test-plan.md)

**Tracking issues**:

[#91 Phase 1 → Phase 2 roadmap](https://github.com/joshua-project/jp-adopt-core/issues/91) ·
[#39 User testing readiness](https://github.com/joshua-project/jp-adopt-core/issues/39) ·
[jp-infrastructure#203 SWA cleanup](https://github.com/joshua-project/jp-infrastructure/issues/203)
