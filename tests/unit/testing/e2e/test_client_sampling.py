"""Tests for the sync ``AEWorkflowClient`` adapter over the harness's Atlas reads.

Stubs the async Atlan client so the read shape + the adapter's fail-open
contract are covered without a tenant: the real ``FluentSearch`` request-building
runs; only the network ``asset.search`` call is faked.

The fail-open assertions here are pinning a **deliberately temporary**
behaviour. The harness readers themselves report an unreadable search as
:class:`~application_sdk.testing.harness.outcome.Indeterminate` (see
``tests/unit/testing/harness/atlas/test_atlas_reads.py``); this adapter collapses
that back to today's empty value because its callers in ``testing/e2e/base.py``
take a plain ``dict`` and grade it. Child H deletes the collapse, and these
assertions with it.
"""

from __future__ import annotations

from typing import Any

from application_sdk.testing.e2e.client import AEWorkflowClient
from tests.unit.testing._atlas_fakes import FakeAsset, FakeSearchResult, fake_atlas


def _client_with(monkeypatch: Any, behavior: Any) -> AEWorkflowClient:
    client = AEWorkflowClient("https://x.atlan.com", "token")
    monkeypatch.setattr(client, "_atlas", lambda: fake_atlas(behavior))
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
    def behavior(_request: Any) -> FakeSearchResult:
        return FakeSearchResult([FakeAsset(f"default/x/1/t{i}") for i in range(10)])

    client = _client_with(monkeypatch, behavior)
    out = client.sample_asset_qualified_names_under_connection(
        "default/x/1", type_names=("Table",), per_type=3
    )
    assert len(out["Table"]) == 3


def test_fails_open_on_search_error(monkeypatch: Any) -> None:
    # A search fault degrades to [] (skip), not a raised exception — the
    # adapter's collapse, not the reader's verdict.
    def behavior(_request: Any) -> FakeSearchResult:
        raise RuntimeError("boom")

    client = _client_with(monkeypatch, behavior)
    out = client.sample_asset_qualified_names_under_connection(
        "default/x/1", type_names=("Table",)
    )
    assert out == {"Table": []}


def test_maps_each_type_to_its_qns(monkeypatch: Any) -> None:
    def behavior(request: Any) -> FakeSearchResult:
        # The type filter is embedded in the DSL; return one qn tagged so the
        # test just asserts the type->list zip is correct regardless of order.
        return FakeSearchResult([FakeAsset("default/x/1/db")])

    client = _client_with(monkeypatch, behavior)
    out = client.sample_asset_qualified_names_under_connection(
        "default/x/1", type_names=("Database", "Schema"), per_type=3
    )
    assert set(out) == {"Database", "Schema"}
    assert out["Database"] == ["default/x/1/db"]


def test_counts_fail_open_to_zeros(monkeypatch: Any) -> None:
    """The other half of the collapse: a count that could not be read is zeros.

    Which is exactly the misdiagnosis FND-224 catalogued as C4 — an Atlas outage
    surfacing as "asset floor not met". The reader no longer says that; this
    adapter still does, and says so in the log.
    """

    def behavior(_request: Any) -> FakeSearchResult:
        raise RuntimeError("atlas is down")

    client = _client_with(monkeypatch, behavior)
    assert client.count_assets_under_connection(
        "default/x/1", type_names=("Table", "View")
    ) == {"Table": 0, "View": 0}
    assert client.count_total_assets_under_connection("default/x/1") == 0
    assert client.count_lineage_under_connection(
        "default/x/1", type_names=("Table",)
    ) == {"Table": 0}
    assert client.connection_exists_in_atlas_via_search("default/x/1") is False


def test_a_readable_zero_is_still_zero(monkeypatch: Any) -> None:
    """The distinction only pays if a genuine zero survives it unchanged."""

    def behavior(_request: Any) -> FakeSearchResult:
        return FakeSearchResult(count=0)

    client = _client_with(monkeypatch, behavior)
    assert client.count_assets_under_connection(
        "default/x/1", type_names=("Table",)
    ) == {"Table": 0}
