"""DT migration-conflict reconciliation tooling.

Operator-facing utilities that work the ``migration_conflicts`` backlog
left behind by the main ETL. Each track targets one ``conflict_type``:

  * ``track_b_assignments`` — ``assignee_no_subject`` (246 rows in prod).

Every module here DEFAULTS TO DRY-RUN. Writes require an explicit
``--apply`` flag and go through the existing outbox-suppression path so a
single ``jp.adopt.v1.bulk_imported`` summary event is emitted instead of
per-row Outbox rows. Everything is idempotent (ON CONFLICT upserts +
delete-by-natural-key), so dry-run-then-apply or apply-twice is safe.
"""

from __future__ import annotations

__all__: list[str] = []
