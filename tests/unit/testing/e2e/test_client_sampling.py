"""Tests for AEWorkflowClient.sample_asset_qualified_names_under_connection.

Stubs the async Atlan client so the read shape + fail-open contract are covered
without a tenant: the real FluentSearch request-building runs; only the network
``asset.search`` call is faked.
"""

from __future__ import annotations

from typing import Any

from application_sdk.testing.e2e.client import AEWorkflowClient


class _FakeAsset:
    def __init__(self, qn: str) -> None:
        self.qualified_name = qn


class _FakeSearchResult:
    def __init__(self, assets: list[_FakeAsset]) -> None:
        self._assets = assets

    def current_page(self) -> list[_FakeAsset]:
        return self._assets


class _FakeAssetApi:
    def __init__(self, behavior: Any) -> None:
        self._behavior = behavior

    async def search(self, request: Any) -> Any:
        return self._behavior(request)


class _FakeClient:
    def __init__(self, behavior: Any) -> None:
        self.asset = _FakeAssetApi(behavior)


class _FakeAsyncCM:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    async def __aenter__(self) -> _FakeClient:
        return self._client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _client_with(monkeypatch: Any, behavior: Any) -> AEWorkflowClient:
    client = AEWorkflowClient("https://x.atlan.com", "token")
    monkeypatch.setattr(
        client,
        "_build_async_atlan_client",
        lambda: _FakeAsyncCM(_FakeClient(behavior)),
    )
    return client


def test_empty_type_names_returns_empty() -> None:
    client = AEWorkflowClient("https://x.atlan.com", "token")
    assert (
        client.sample_asset_qualified_names_under_connection(
            "default/x/1", type_names=()
        )
        == {}
    )


def test_per_type_is_enforced(monkeypatch: Any) -> None:
    # Server returns more hits than per_type; the method trims to per_type.
    def behavior(_request: Any) -> _FakeSearchResult:
        return _FakeSearchResult([_FakeAsset(f"default/x/1/t{i}") for i in range(10)])

    client = _client_with(monkeypatch, behavior)
    out = client.sample_asset_qualified_names_under_connection(
        "default/x/1", type_names=("Table",), per_type=3
    )
    assert len(out["Table"]) == 3


def test_fails_open_on_search_error(monkeypatch: Any) -> None:
    # A search fault degrades to [] (skip), not a raised exception.
    def behavior(_request: Any) -> _FakeSearchResult:
        raise RuntimeError("boom")

    client = _client_with(monkeypatch, behavior)
    out = client.sample_asset_qualified_names_under_connection(
        "default/x/1", type_names=("Table",)
    )
    assert out == {"Table": []}


def test_maps_each_type_to_its_qns(monkeypatch: Any) -> None:
    def behavior(request: Any) -> _FakeSearchResult:
        # The type filter is embedded in the DSL; return one qn tagged so the
        # test just asserts the type->list zip is correct regardless of order.
        return _FakeSearchResult([_FakeAsset("default/x/1/db")])

    client = _client_with(monkeypatch, behavior)
    out = client.sample_asset_qualified_names_under_connection(
        "default/x/1", type_names=("Database", "Schema"), per_type=3
    )
    assert set(out) == {"Database", "Schema"}
    assert out["Database"] == ["default/x/1/db"]
