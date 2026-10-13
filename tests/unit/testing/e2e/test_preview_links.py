"""A passing full-DAG run leaves a product link per landed asset type.

A reviewer validating a connector change otherwise has to log in to the test
tenant and search for what the run published, and teardown purges it about a
minute later. The links are written before teardown, and they are reporting
only: nothing about writing them may turn a passing run red.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import orjson
import pytest

from application_sdk.testing.e2e.base import FullDAGOutcome
from application_sdk.testing.harness import preview_hold
from tests.unit.testing._atlas_fakes import FakeAsset, FakeAtlasClient, FakeSearchResult
from tests.unit.testing.e2e.test_base_e2e import _ConcreteE2ETest

_QN = "default/openapi/1787587123106596"
_BASE = "https://example-tenant.atlan.com"


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    counts: dict[str, int],
    behavior: Any,
) -> _ConcreteE2ETest:
    client = FakeAtlasClient(behavior)

    @asynccontextmanager
    async def _client(self: object) -> AsyncIterator[FakeAtlasClient]:
        yield client

    monkeypatch.setattr(_ConcreteE2ETest, "_atlas_client", _client)
    monkeypatch.setattr(_ConcreteE2ETest, "preview_links_dir", str(tmp_path))
    monkeypatch.delenv(preview_hold.HOLD_ENV, raising=False)
    harness = _ConcreteE2ETest()
    harness.connection_qualified_name = _QN
    harness.connection_display_name = "openapi-1787587123106596"
    harness._fake_client = client  # type: ignore[attr-defined]
    harness._auth = SimpleNamespace(base_url=_BASE)
    harness.dag_outcomes = [
        FullDAGOutcome(
            ae_result=MagicMock(),
            connection_qualified_name=_QN,
            connection_in_atlas=True,
            asset_counts=counts,
        )
    ]
    return harness


def _written(tmp_path: Path) -> dict[str, Any]:
    return orjson.loads((tmp_path / "_ConcreteE2ETest.json").read_bytes())


def test_each_landed_type_gets_a_link_to_one_of_its_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _harness(
        monkeypatch,
        tmp_path,
        {"Table": 4},
        lambda _r: FakeSearchResult([FakeAsset(f"{_QN}/t", guid="g-1")]),
    )
    harness._write_preview_links()
    body = _written(tmp_path)
    assert body["links"] == {"Table": f"{_BASE}/assets/g-1/overview"}
    assert body["connection_qualified_name"] == _QN


def test_a_type_with_no_landed_asset_gets_no_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _harness(
        monkeypatch,
        tmp_path,
        {"Table": 4, "Column": 0},
        lambda _r: FakeSearchResult([FakeAsset(f"{_QN}/t", guid="g-1")]),
    )
    harness._write_preview_links()
    assert set(_written(tmp_path)["links"]) == {"Table"}


def test_an_unreadable_tenant_writes_no_links_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom(_request: Any) -> FakeSearchResult:
        raise RuntimeError("atlas unavailable")

    harness = _harness(monkeypatch, tmp_path, {"Table": 4}, _boom)
    harness._write_preview_links()
    assert _written(tmp_path)["links"] == {}


def test_disabled_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _harness(
        monkeypatch, tmp_path, {"Table": 4}, lambda _r: FakeSearchResult([])
    )
    monkeypatch.setattr(_ConcreteE2ETest, "preview_links_dir", "")
    harness._write_preview_links()
    assert not any(tmp_path.iterdir())


# ---------------------------------------------------------------------------
# Holding the connection so the links outlive the run
# ---------------------------------------------------------------------------


def _landed(_request: Any) -> FakeSearchResult:
    return FakeSearchResult([FakeAsset(f"{_QN}/t", guid="g-1")])


def test_a_requested_hold_marks_the_connection_and_records_the_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _harness(monkeypatch, tmp_path, {"Table": 4}, _landed)
    monkeypatch.setenv(preview_hold.HOLD_ENV, "24")
    harness._write_preview_links()
    [saved] = harness._fake_client.saved  # type: ignore[attr-defined]
    assert saved.qualified_name == _QN
    expires = preview_hold.held_until(saved.description)
    assert expires is not None
    assert _written(tmp_path)["expires_at"] == preview_hold.stamp(expires)
    assert harness._preview_held_qn == _QN


def test_a_mark_that_fails_holds_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _harness(monkeypatch, tmp_path, {"Table": 4}, _landed)
    monkeypatch.setenv(preview_hold.HOLD_ENV, "24")

    async def _refuse(_asset: Any) -> None:
        raise RuntimeError("save rejected")

    monkeypatch.setattr(harness._fake_client.asset, "save", _refuse)  # type: ignore[attr-defined]
    harness._write_preview_links()
    assert "expires_at" not in _written(tmp_path)
    assert not getattr(harness, "_preview_held_qn", "")


def test_no_hold_without_the_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _harness(monkeypatch, tmp_path, {"Table": 4}, _landed)
    harness._write_preview_links()
    assert harness._fake_client.saved == []  # type: ignore[attr-defined]
    assert "expires_at" not in _written(tmp_path)


def test_teardown_leaves_a_held_connection_for_the_reaper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    purged: list[str] = []

    async def _record(_client: object, qn: str) -> object:
        purged.append(qn)
        return SimpleNamespace(purged=1, orphaned=(), errors=())

    monkeypatch.setattr("application_sdk.testing.e2e.base.purge_connection", _record)
    harness = _harness(monkeypatch, tmp_path, {"Table": 4}, _landed)
    harness._submitted_connection_qns = [_QN]
    harness._preview_held_qn = _QN
    harness.teardown_method(method=None)
    assert purged == []


def test_teardown_still_deletes_an_unheld_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    purged: list[str] = []

    async def _record(_client: object, qn: str) -> object:
        purged.append(qn)
        return SimpleNamespace(purged=1, orphaned=(), errors=())

    monkeypatch.setattr("application_sdk.testing.e2e.base.purge_connection", _record)
    harness = _harness(monkeypatch, tmp_path, {"Table": 4}, _landed)
    harness._submitted_connection_qns = [_QN]
    harness.teardown_method(method=None)
    assert purged == [_QN]
