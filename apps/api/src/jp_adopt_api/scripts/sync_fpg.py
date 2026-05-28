"""Mirror jp-adopt-forms's fpg_cache into core's ``fpg`` reference table.

Core keys people groups by ``people_id3``. The forms repo's export endpoint
collapses multi-country cache rows to one row per ``people_id3``; this script
pulls that JSON and upserts into Postgres.

Usage (needs FORMS_EXPORT_URL + FORMS_EXPORT_API_KEY in the environment):

    uv run python -m jp_adopt_api.scripts.sync_fpg
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jp_adopt_api.config import get_settings
from jp_adopt_api.db import get_session_factory
from jp_adopt_api.models import Fpg


def normalize_rows(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map forms export rows to fpg upsert dicts. Skips items missing people_id3."""
    out: list[dict[str, Any]] = []
    for item in data:
        people_id3 = str(item.get("people_id3") or "").strip()
        if not people_id3:
            continue
        country_code = str(item.get("country_code") or "").strip().upper() or None
        name = str(item.get("name") or f"People {people_id3}").strip()
        out.append(
            {
                "people_id3": people_id3,
                "name": name,
                "country_code": country_code,
                "frontier": bool(item.get("frontier", True)),
            }
        )
    return out


async def fetch_from_forms_export(
    export_url: str, api_key: str, *, timeout_s: float = 60.0
) -> list[dict[str, Any]]:
    base = export_url.rstrip("/")
    url = f"{base}/api/v1/people-groups/export?frontier_only=true"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        resp.raise_for_status()
        body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"forms export returned ok=false: {body!r}")
    data = body.get("data", {})
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected export envelope: {body!r}")
    return rows


async def upsert_fpg(
    session: AsyncSession, rows: list[dict[str, Any]], *, chunk: int = 500
) -> int:
    """Upsert fpg rows by people_id3."""
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        stmt = pg_insert(Fpg).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["people_id3"],
            set_={
                "name": stmt.excluded.name,
                "country_code": stmt.excluded.country_code,
                "frontier": stmt.excluded.frontier,
            },
        )
        await session.execute(stmt)
        written += len(batch)
    await session.commit()
    return written


async def sync() -> int:
    settings = get_settings()
    export_url = settings.forms_export_url.strip()
    api_key = settings.forms_export_api_key.strip()
    if not export_url or not api_key:
        print(
            "FORMS_EXPORT_URL and FORMS_EXPORT_API_KEY must be set; nothing to sync.",
            file=sys.stderr,
        )
        return 1
    raw = await fetch_from_forms_export(export_url, api_key)
    rows = normalize_rows(raw)
    factory = get_session_factory()
    async with factory() as session:
        written = await upsert_fpg(session, rows)
    print(
        f"fpg sync: {len(raw)} export rows -> "
        f"{len(rows)} upserted ({written} written)."
    )
    return 0


def main() -> None:
    sys.exit(asyncio.run(sync()))


if __name__ == "__main__":
    main()
