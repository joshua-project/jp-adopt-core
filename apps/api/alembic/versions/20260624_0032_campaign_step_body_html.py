"""Add campaign_step.body_html and seed it from the current template files

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-24

The drip template editor moves authored body content from the
``apps/api/email-templates/*.mjml`` files into the DB so non-technical staff
can edit it in-app. This revision:

  1. Adds a nullable ``body_html`` column to ``campaign_step`` and relaxes
     ``mjml_template_name`` to nullable (a step now carries EITHER inline
     ``body_html`` OR a template filename; render prefers ``body_html``).
  2. Seeds ``body_html`` for every existing step from the ``{% block body %}``
     content of its referenced template file, so editors start from the
     existing wording rather than a blank slate.

The seed reads the template files directly. ``alembic upgrade head`` runs in
the deploy workflow's CI checkout (full repo present) and in local dev, both of
which ship ``apps/api/email-templates/``. If a file is missing or has no body
block, that step is left with ``body_html = NULL`` and render falls back to the
file — so the seed is safe even where the files are absent. Idempotent: only
rows with ``body_html IS NULL`` are touched.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


# apps/api/email-templates, resolved relative to this migration file
# (apps/api/alembic/versions/<this>.py -> apps/api/email-templates).
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "email-templates"

_BODY_BLOCK_RE = re.compile(
    r"{%\s*block\s+body\s*%}(.*?){%\s*endblock\s*%}",
    re.DOTALL,
)


def _extract_body(template_name: str) -> str | None:
    """Return the inner ``{% block body %}`` content of a template file,
    stripped, or ``None`` if the file or block is absent."""
    path = _TEMPLATES_DIR / template_name
    if not path.is_file():
        return None
    match = _BODY_BLOCK_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        return None
    return match.group(1).strip()


def upgrade() -> None:
    op.add_column(
        "campaign_step",
        sa.Column("body_html", sa.Text(), nullable=True),
    )
    op.alter_column(
        "campaign_step",
        "mjml_template_name",
        existing_type=sa.Text(),
        nullable=True,
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, mjml_template_name FROM campaign_step "
            "WHERE body_html IS NULL AND mjml_template_name IS NOT NULL"
        )
    ).fetchall()

    for step_id, template_name in rows:
        body = _extract_body(template_name)
        if body is None:
            continue
        bind.execute(
            sa.text(
                "UPDATE campaign_step SET body_html = :body "
                "WHERE id = :id AND body_html IS NULL"
            ),
            {"body": body, "id": step_id},
        )


def downgrade() -> None:
    op.drop_column("campaign_step", "body_html")
    # Best-effort restore of the NOT NULL on mjml_template_name. Rows created
    # after this migration may have a null template (body-only steps); guard so
    # downgrade doesn't fail on them.
    bind = op.get_bind()
    null_count = bind.execute(
        sa.text(
            "SELECT count(*) FROM campaign_step WHERE mjml_template_name IS NULL"
        )
    ).scalar_one()
    if null_count == 0:
        op.alter_column(
            "campaign_step",
            "mjml_template_name",
            existing_type=sa.Text(),
            nullable=False,
        )
