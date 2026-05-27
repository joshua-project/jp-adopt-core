"""Populate the `fpg` reference table from the Joshua Project API.

The public intake forms carry a numeric `people_id3`; intake resolves it to
the canonical `rop3` via `fpg.people_id3` (see routers/intake.py
`_resolve_rop3`). That resolution is inert until `fpg` holds real Joshua
Project people groups with both ids — this script loads them.

Data model note: the JP API returns one row per people-group-in-country
(PGIC). `ROP3` and `PeopleID3` are the across-country people identity (1:1
with each other) and repeat across a group's countries; `fpg` is keyed by
`rop3`, so we collapse the PGIC rows to one row per `ROP3`, choosing the
highest-population country as the representative `country_code`/name. The
`people_id3 -> rop3` resolution stays exact because the pair is 1:1.

Usage (needs JOSHUA_PROJECT_API_KEY in the environment):

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

JP_API_URL = "https://api.joshuaproject.net/v1/people_groups.json"
_PAGE_LIMIT = 250  # JP API hard max


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_rows(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse PGIC rows to one `fpg` row per ROP3 (highest-population country
    wins as the representative country_code). Rows missing ROP3 or PeopleID3
    are skipped — both are required for the resolution to mean anything."""
    best: dict[str, dict[str, Any]] = {}
    best_pop: dict[str, int] = {}
    for row in raw:
        rop3 = str(row.get("ROP3") or "").strip()
        people_id3 = row.get("PeopleID3")
        if not rop3 or people_id3 is None:
            continue
        pop = _to_int(row.get("Population"))
        if rop3 in best and pop <= best_pop[rop3]:
            continue
        iso3 = str(row.get("ISO3") or "").strip().upper()
        name = (
            str(
                row.get("PeopNameAcrossCountries")
                or row.get("PeopNameInCountry")
                or f"People {rop3}"
            ).strip()
        )
        best[rop3] = {
            "rop3": rop3,
            "people_id3": str(_to_int(people_id3)),
            "name": name,
            "country_code": iso3 or None,
            "frontier": True,
        }
        best_pop[rop3] = pop
    return list(best.values())


async def fetch_all_frontier(
    api_key: str, *, max_pages: int = 200, delay_s: float = 0.3
) -> list[dict[str, Any]]:
    """Page through the JP API's frontier people groups (PGIC rows)."""
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        page = 1
        while page <= max_pages:
            resp = await client.get(
                JP_API_URL,
                params={
                    "api_key": api_key,
                    "page": page,
                    "limit": _PAGE_LIMIT,
                    "is_frontier": "Y",
                    "include_profile_text": "N",
                    "include_resources": "N",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            rows = data if isinstance(data, list) else data.get("data", [])
            out.extend(rows)
            if len(rows) < _PAGE_LIMIT:
                break
            page += 1
            if delay_s > 0:
                await asyncio.sleep(delay_s)
    return out


async def upsert_fpg(
    session: AsyncSession, rows: list[dict[str, Any]], *, chunk: int = 500
) -> int:
    """Upsert fpg rows by rop3. Refreshes people_id3/name/country_code/frontier
    on conflict so re-running the sync keeps the table current."""
    if not rows:
        return 0
    written = 0
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        stmt = pg_insert(Fpg).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["rop3"],
            set_={
                "people_id3": stmt.excluded.people_id3,
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
    api_key = settings.joshua_project_api_key.strip()
    if not api_key:
        print(
            "JOSHUA_PROJECT_API_KEY is not set; nothing to sync.", file=sys.stderr
        )
        return 1
    raw = await fetch_all_frontier(api_key)
    rows = normalize_rows(raw)
    factory = get_session_factory()
    async with factory() as session:
        written = await upsert_fpg(session, rows)
    print(
        f"fpg sync: {len(raw)} PGIC rows fetched -> "
        f"{len(rows)} unique people groups upserted ({written} written)."
    )
    return 0


def main() -> None:
    sys.exit(asyncio.run(sync()))


if __name__ == "__main__":
    main()
