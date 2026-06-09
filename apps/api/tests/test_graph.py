"""Tests for the MS Graph client module (#97).

We mock at the httpx boundary so msal's token cache + AAD calls
don't fire — those would require live credentials and aren't part
of the public contract we're testing.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from jp_adopt_api import graph as graph_mod
from jp_adopt_api.graph import (
    GraphUser,
    graph_configured,
    lookup_users_by_ids,
    search_users,
)


def _settings_with_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force settings to look populated and reset the cached msal app."""
    from jp_adopt_api import config as cfg

    monkeypatch.setenv("AZURE_GRAPH_TENANT_ID", "tenant-id-stub")
    monkeypatch.setenv("AZURE_GRAPH_CLIENT_ID", "client-id-stub")
    monkeypatch.setenv("AZURE_GRAPH_CLIENT_SECRET", "client-secret-stub")
    cfg.get_settings.cache_clear() if hasattr(cfg.get_settings, "cache_clear") else None
    # The graph module memoizes the msal app — reset so each test's
    # token-acquire patch is honored.
    graph_mod._msal_app = None  # type: ignore[attr-defined]


def _stub_acquire_token(
    monkeypatch: pytest.MonkeyPatch, token: str | None = "fake-token"
) -> None:
    async def _t() -> str | None:
        return token

    monkeypatch.setattr(graph_mod, "_acquire_token", _t)


class _StubAsyncClient:
    """Minimal AsyncClient stub. Returns whichever responses we queue."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _StubAsyncClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((url, kwargs))
        if not self._responses:
            raise AssertionError(f"unexpected extra GET to {url}")
        return self._responses.pop(0)


def _make_response(status: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status, json=body or {}, request=httpx.Request("GET", "https://x")
    )


def test_graph_configured_false_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jp_adopt_api import config as cfg

    monkeypatch.delenv("AZURE_GRAPH_TENANT_ID", raising=False)
    monkeypatch.delenv("AZURE_GRAPH_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_GRAPH_CLIENT_SECRET", raising=False)
    if hasattr(cfg.get_settings, "cache_clear"):
        cfg.get_settings.cache_clear()
    assert graph_configured() is False


def test_graph_configured_true_when_all_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_graph(monkeypatch)
    assert graph_configured() is True


@pytest.mark.asyncio
async def test_lookup_users_returns_empty_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jp_adopt_api import config as cfg

    monkeypatch.delenv("AZURE_GRAPH_TENANT_ID", raising=False)
    monkeypatch.delenv("AZURE_GRAPH_CLIENT_ID", raising=False)
    monkeypatch.delenv("AZURE_GRAPH_CLIENT_SECRET", raising=False)
    cfg.get_settings.cache_clear()
    out = await lookup_users_by_ids(["a", "b"])
    assert out == {}


@pytest.mark.asyncio
async def test_lookup_users_returns_empty_when_token_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_graph(monkeypatch)
    _stub_acquire_token(monkeypatch, token=None)
    out = await lookup_users_by_ids(["a"])
    assert out == {}


@pytest.mark.asyncio
async def test_lookup_users_parses_graph_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_graph(monkeypatch)
    _stub_acquire_token(monkeypatch)
    stub = _StubAsyncClient(
        [
            _make_response(
                200,
                {
                    "value": [
                        {
                            "id": "oid-1",
                            "displayName": "Amy Adopter",
                            "userPrincipalName": "amy@globalspecifics.com",
                            "mail": "amy@globalspecifics.com",
                        },
                        {
                            "id": "oid-2",
                            "displayName": "Joel Castillo",
                            "userPrincipalName": "joel@joshuaproject.net",
                            "mail": None,
                        },
                    ]
                },
            )
        ]
    )
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", lambda **_: stub)
    out = await lookup_users_by_ids(["oid-1", "oid-2"])
    assert set(out.keys()) == {"oid-1", "oid-2"}
    assert out["oid-1"].display_name == "Amy Adopter"
    assert out["oid-1"].mail == "amy@globalspecifics.com"
    assert out["oid-2"].mail is None
    # The Graph filter string includes both ids quoted.
    url, kwargs = stub.requests[0]
    assert "id in ('oid-1','oid-2')" in kwargs["params"]["$filter"]


@pytest.mark.asyncio
async def test_lookup_users_handles_non_200_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_graph(monkeypatch)
    _stub_acquire_token(monkeypatch)
    stub = _StubAsyncClient([_make_response(403, {"error": {"code": "Forbidden"}})])
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", lambda **_: stub)
    out = await lookup_users_by_ids(["oid-1"])
    assert out == {}


@pytest.mark.asyncio
async def test_lookup_users_handles_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_graph(monkeypatch)
    _stub_acquire_token(monkeypatch)

    class _Bomb:
        async def __aenter__(self) -> _Bomb:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, *a: object, **kw: object) -> httpx.Response:
            raise httpx.ConnectError("dns busted")

    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", lambda **_: _Bomb())
    out = await lookup_users_by_ids(["oid-1"])
    assert out == {}


@pytest.mark.asyncio
async def test_search_users_returns_empty_on_blank_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_graph(monkeypatch)
    _stub_acquire_token(monkeypatch)
    assert await search_users("") == []
    assert await search_users("   ") == []


@pytest.mark.asyncio
async def test_search_users_parses_response_and_uses_consistency_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_graph(monkeypatch)
    _stub_acquire_token(monkeypatch)
    stub = _StubAsyncClient(
        [
            _make_response(
                200,
                {
                    "value": [
                        {
                            "id": "oid-A",
                            "displayName": "Amy A.",
                            "userPrincipalName": "amy@x.com",
                            "mail": "amy@x.com",
                        }
                    ]
                },
            )
        ]
    )
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", lambda **_: stub)
    out = await search_users("amy")
    assert len(out) == 1
    assert isinstance(out[0], GraphUser)
    assert out[0].display_name == "Amy A."
    url, kwargs = stub.requests[0]
    assert kwargs["headers"]["ConsistencyLevel"] == "eventual"
    assert "startsWith(displayName,'amy')" in kwargs["params"]["$filter"]


@pytest.mark.asyncio
async def test_search_users_quotes_single_quote_in_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_with_graph(monkeypatch)
    _stub_acquire_token(monkeypatch)
    stub = _StubAsyncClient([_make_response(200, {"value": []})])
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", lambda **_: stub)
    await search_users("O'Brien")
    _, kwargs = stub.requests[0]
    # OData escapes single quote by doubling it.
    assert "O''Brien" in kwargs["params"]["$filter"]
