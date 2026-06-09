"""Microsoft Graph client for backend user lookup (#97).

The admin user-roles endpoint and the user-search typeahead both need
to translate Entra OIDs <-> display names + UPNs without a user
session, so we use client-credentials (application permission
`User.Read.All`).

Design:
- Stateless module-level helpers. Token acquisition is delegated to
  the `msal.ConfidentialClientApplication` token cache, which holds
  the AAD-issued access token until its `exp` minus a safety margin.
- All public helpers are async-friendly even though `msal` is sync —
  the network call is offloaded via `asyncio.to_thread`. Network and
  parsing errors degrade to None / empty so admin endpoints stay
  responsive even when Graph is unreachable.
- Configuration is optional. When `AZURE_GRAPH_*` env vars are unset
  (typical dev), `graph_configured()` is False and helpers return
  empty results — the admin endpoints then fall back to OID-only.
- TTL caching: msal handles access-token caching. Lookup-result
  caching (per OID, per query) is intentionally NOT added in v1 —
  the staff endpoints aren't hot enough for cache invalidation to
  pay back its complexity.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_TIMEOUT_S = 5.0

_msal_app: Any | None = None  # msal.ConfidentialClientApplication when configured


def graph_configured() -> bool:
    """True when all three AZURE_GRAPH_* env vars are populated.

    A missing config is not an error — admin endpoints just degrade
    to OID-only output. Callers check this before issuing Graph
    requests so we don't import msal in environments where it isn't
    used.
    """
    s = get_settings()
    return bool(
        s.azure_graph_tenant_id
        and s.azure_graph_client_id
        and s.azure_graph_client_secret
    )


def _get_app() -> Any | None:
    """Memoize the msal app. Returns None when not configured."""
    global _msal_app
    if not graph_configured():
        return None
    if _msal_app is not None:
        return _msal_app
    try:
        import msal  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("graph.msal_missing — install msal to enable user lookup")
        return None
    s = get_settings()
    _msal_app = msal.ConfidentialClientApplication(
        client_id=s.azure_graph_client_id,
        client_credential=s.azure_graph_client_secret,
        authority=f"https://login.microsoftonline.com/{s.azure_graph_tenant_id}",
    )
    return _msal_app


def _acquire_token_sync() -> str | None:
    """Synchronously acquire an access token via client credentials.

    msal's `acquire_token_for_client` consults its internal cache
    before making a network call, so repeated calls in the same
    process don't re-hit AAD.
    """
    app = _get_app()
    if app is None:
        return None
    result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
    if isinstance(result, dict) and "access_token" in result:
        return str(result["access_token"])
    logger.warning(
        "graph.token_acquire_failed err=%s description=%s",
        result.get("error") if isinstance(result, dict) else "unknown",
        result.get("error_description") if isinstance(result, dict) else None,
    )
    return None


async def _acquire_token() -> str | None:
    return await asyncio.to_thread(_acquire_token_sync)


class GraphUser:
    """Subset of Graph's `user` object the admin surfaces consume."""

    __slots__ = ("id", "display_name", "user_principal_name", "mail")

    def __init__(
        self,
        *,
        id: str,  # noqa: A002 — mirrors the Graph field name
        display_name: str | None,
        user_principal_name: str | None,
        mail: str | None,
    ) -> None:
        self.id = id
        self.display_name = display_name
        self.user_principal_name = user_principal_name
        self.mail = mail


def _parse_user(d: dict[str, Any]) -> GraphUser:
    return GraphUser(
        id=str(d.get("id", "")),
        display_name=d.get("displayName"),
        user_principal_name=d.get("userPrincipalName"),
        mail=d.get("mail"),
    )


async def lookup_users_by_ids(ids: list[str]) -> dict[str, GraphUser]:
    """Resolve a batch of OIDs to GraphUser records.

    Uses a single Graph call: ``GET /users?$filter=id in ('a','b')``.
    Returns a dict keyed by OID for callers that need O(1) lookup
    when zipping back into their own rows. Missing OIDs are simply
    absent from the dict — no error.
    """
    if not ids or not graph_configured():
        return {}
    token = await _acquire_token()
    if token is None:
        return {}
    # Graph's $filter `in` clause caps at ~15 ids per request; the
    # admin user-roles list page is bounded at the same scale today.
    # If we ever paginate beyond that we'll chunk here.
    quoted = ",".join(f"'{oid}'" for oid in ids if oid)
    if not quoted:
        return {}
    params = {
        "$filter": f"id in ({quoted})",
        "$select": "id,displayName,userPrincipalName,mail",
        "$top": str(min(len(ids), 100)),
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_S) as client:
            r = await client.get(
                f"{GRAPH_BASE}/users", params=params, headers=headers
            )
            if r.status_code != 200:
                logger.warning(
                    "graph.batch_lookup_failed status=%s body=%s",
                    r.status_code,
                    r.text[:200],
                )
                return {}
            users = (r.json() or {}).get("value") or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("graph.batch_lookup_error err=%s", e)
        return {}
    return {u["id"]: _parse_user(u) for u in users if isinstance(u, dict) and u.get("id")}


async def search_users(query: str, *, limit: int = 10) -> list[GraphUser]:
    """Search users by display name or email prefix for the
    admin-typeahead surface.

    Empty / whitespace-only queries return [] without a Graph hit.
    Errors and missing config degrade to []. Caller (the admin
    router) decides whether to surface that to the operator.
    """
    q = query.strip()
    if not q or not graph_configured():
        return []
    token = await _acquire_token()
    if token is None:
        return []
    # Use startsWith on both displayName and mail; userPrincipalName
    # often matches mail but covers the on-prem-only edge case.
    quoted_q = q.replace("'", "''")
    filter_clause = (
        f"startsWith(displayName,'{quoted_q}') or "
        f"startsWith(mail,'{quoted_q}') or "
        f"startsWith(userPrincipalName,'{quoted_q}')"
    )
    params = {
        "$filter": filter_clause,
        "$select": "id,displayName,userPrincipalName,mail",
        "$top": str(min(limit, 25)),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        # ConsistencyLevel:eventual + $count enables advanced query;
        # $count makes the $filter on `mail` work for users without
        # `mail` populated. Required for startsWith on these fields.
        "ConsistencyLevel": "eventual",
    }
    try:
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_S) as client:
            r = await client.get(
                f"{GRAPH_BASE}/users", params=params, headers=headers
            )
            if r.status_code != 200:
                logger.warning(
                    "graph.search_failed status=%s body=%s",
                    r.status_code,
                    r.text[:200],
                )
                return []
            users = (r.json() or {}).get("value") or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("graph.search_error err=%s", e)
        return []
    return [_parse_user(u) for u in users if isinstance(u, dict) and u.get("id")]


__all__ = [
    "GRAPH_BASE",
    "GRAPH_SCOPE",
    "GraphUser",
    "graph_configured",
    "lookup_users_by_ids",
    "search_users",
]
