"""``BaseE2ETest.seed_assets`` — the registry that closes the teardown gap.

The harness half (what gets written, in what order, under which exact QNs) is
pinned in ``tests/unit/testing/harness/atlas/test_seed_assets.py``. What is left
to check here is the wiring, the same split ``teardown_method``'s own tests
draw: the seeded connection's QN is minted when the spec brings none, it is
registered *before* the seed runs so a half-failed seed still gets torn down,
and ``_purge_this_run`` purges every registered QN after the run's own — with
each purge independently guarded, so one connection that will not purge cannot
orphan the others.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from application_sdk.testing.e2e.base import BaseE2ETest
from application_sdk.testing.e2e.payload import RunMode
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.harness.atlas import seed as atlas_seed

_RUN_QN = "default/openapi/1787587123106596"
_SEED_QN = "default/snowflake/1787587123106596-seed"


@asynccontextmanager
async def _null_atlas_client() -> AsyncIterator[object]:
    """Stand-in for the Atlas client, for a test that patches what reads it."""
    yield object()


class _SeedingE2ETest(BaseE2ETest):
    """Minimal concrete subclass, built without ``setup_method``."""

    connector_short_name = "openapi"
    argo_package_name = "@atlan/openapi"
    argo_template_name = "atlan-openapi"
    mode = RunMode.DIRECT
    app_service_url = "http://openapi.svc"

    def _mustache_substitutions(self) -> MustacheSubstitutions:  # pragma: no cover
        raise NotImplementedError


def _spec(qualified_name: str = _SEED_QN) -> atlas_seed.SeedSpec:
    return atlas_seed.SeedSpec(
        connector_type="snowflake",
        qualified_name=qualified_name,
        display_name="snowflake-seed",
        admin_roles=("role-guid",),
    )


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed_error: BaseException | None = None,
) -> tuple[_SeedingE2ETest, list[str], list[str]]:
    """A harness whose seed and purge are recorded rather than performed."""
    seeded: list[str] = []
    purged: list[str] = []

    async def _record_seed(
        client: object, spec: atlas_seed.SeedSpec, **_budgets: Any
    ) -> atlas_seed.SeededConnection:
        if seed_error is not None:
            raise seed_error
        seeded.append(spec.qualified_name)
        return atlas_seed.SeededConnection(qualified_name=spec.qualified_name)

    async def _record_purge(client: object, connection_qualified_name: str) -> object:
        purged.append(connection_qualified_name)
        return SimpleNamespace(purged=1, orphaned=(), errors=())

    harness = _SeedingE2ETest()
    harness.connection_qualified_name = _RUN_QN
    harness._seeded_connection_qns = []
    monkeypatch.setattr(
        "application_sdk.testing.e2e.base.atlas_seed.seed_assets", _record_seed
    )
    monkeypatch.setattr(
        "application_sdk.testing.e2e.base.purge_connection", _record_purge
    )
    monkeypatch.setattr(
        _SeedingE2ETest, "_atlas_client", lambda self: _null_atlas_client()
    )
    return harness, seeded, purged


class TestSeedAssetsRegistry:
    """Every seeded connection is registered, and registered up front."""

    def test_the_seeded_qn_is_recorded_and_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, seeded, _purged = _harness(monkeypatch)
        report = harness.seed_assets(_spec())
        assert report.qualified_name == _SEED_QN
        assert seeded == [_SEED_QN]
        assert harness._seeded_connection_qns == [_SEED_QN]

    def test_an_empty_qn_is_minted_from_the_specs_connector_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, seeded, _purged = _harness(monkeypatch)
        harness._minter = SimpleNamespace(
            connection_identity=lambda connector_type: SimpleNamespace(
                qualified_name=f"default/{connector_type}/minted",
                display_name=f"{connector_type}-minted",
            )
        )
        report = harness.seed_assets(_spec(qualified_name=""))
        assert report.qualified_name == "default/snowflake/minted"
        assert seeded == ["default/snowflake/minted"]

    def test_a_seed_that_fails_is_still_registered_for_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The connection may exist by the time the tree write fails; a
        registration on success would leave exactly that half-set-up
        connection behind."""
        harness, _seeded, _purged = _harness(
            monkeypatch, seed_error=RuntimeError("tree write rejected")
        )
        with pytest.raises(RuntimeError):
            harness.seed_assets(_spec())
        assert harness._seeded_connection_qns == [_SEED_QN]


class TestPurgeIncludesSeededConnections:
    """Teardown reaches every connection the run minted, its own first."""

    def test_seeded_connections_are_purged_after_the_runs_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _seeded, purged = _harness(monkeypatch)
        harness.seed_assets(_spec())
        harness.teardown_method(method=None)
        assert purged == [_RUN_QN, _SEED_QN]

    def test_a_run_that_seeded_nothing_purges_only_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _seeded, purged = _harness(monkeypatch)
        harness.teardown_method(method=None)
        assert purged == [_RUN_QN]

    def test_one_failing_purge_does_not_orphan_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report-not-raise, per connection: the run's own purge failing must
        not skip the seeded ones, and none of it may replace the verdict."""
        harness, _seeded, purged = _harness(monkeypatch)
        harness.seed_assets(_spec())

        attempts: list[str] = []

        async def _first_fails(
            client: object, connection_qualified_name: str
        ) -> object:
            attempts.append(connection_qualified_name)
            if connection_qualified_name == _RUN_QN:
                raise RuntimeError("tenant hiccup")
            return SimpleNamespace(purged=1, orphaned=(), errors=())

        monkeypatch.setattr(
            "application_sdk.testing.e2e.base.purge_connection", _first_fails
        )
        harness.teardown_method(method=None)
        assert attempts == [_RUN_QN, _SEED_QN]
