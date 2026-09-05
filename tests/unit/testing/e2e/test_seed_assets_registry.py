"""``BaseE2ETest.seed_assets`` — the registry that closes the teardown gap.

The harness half (what is serialised, under which exact QNs, and what the
publish node is handed) is pinned in ``tests/unit/testing/harness/seed/``. What
is left to check here is the wiring, the same split ``teardown_method``'s own
tests draw: the seeded connection's QN is minted when the spec brings none, the
QN *and* the object-store prefix are registered **before** the seed runs so a
half-failed seed still gets reclaimed, and teardown purges every registered
connection after the run's own — each purge independently guarded, so one that
will not purge cannot orphan the others.

``DAGSpec.connection_qualified_name`` lands in the same registry, and is checked
here for the same reason: it is the other way a run touches a connection this
suite did not mint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from application_sdk.testing.e2e.base import BaseE2ETest, DAGSpec
from application_sdk.testing.e2e.payload import RunMode
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.harness import seed as harness_seed

_RUN_QN = "default/openapi/1787587123106596"
_SEED_QN = "default/snowflake/1787587123106596"
_SEED_PREFIX = "artifacts/apps/openapi/e2e-seed/1787587123106596"


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


def _spec(qualified_name: str | None = _SEED_QN) -> harness_seed.SeedSpec:
    return harness_seed.SeedSpec(
        connector_type="snowflake",
        qualified_name=qualified_name,
        display_name="snowflake-seed",
        admin_roles=("role-guid",),
    )


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed_error: BaseException | None = None,
) -> tuple[_SeedingE2ETest, list[str], list[str], list[str]]:
    """A harness whose seed, purge and prefix delete are recorded, not performed."""
    seeded: list[str] = []
    purged: list[str] = []
    deleted: list[str] = []

    async def _record_seed(
        spec: harness_seed.ResolvedSeedSpec, **_wiring: Any
    ) -> harness_seed.SeededConnection:
        if seed_error is not None:
            raise seed_error
        seeded.append(spec.qualified_name)
        return harness_seed.SeededConnection(qualified_name=spec.qualified_name)

    async def _record_purge(client: object, connection_qualified_name: str) -> object:
        purged.append(connection_qualified_name)
        return SimpleNamespace(purged=1, orphaned=(), errors=())

    async def _record_delete(prefix: str, _store: object) -> int:
        deleted.append(prefix)
        return 1

    harness = _SeedingE2ETest()
    harness.run_id = 1787587123
    harness._ae = object()
    harness.connection_qualified_name = _RUN_QN
    harness._seeded_connection_qns = []
    harness._seeded_prefixes = []
    harness._minter = SimpleNamespace(
        connection_identity=lambda connector_type: SimpleNamespace(
            qualified_name=f"default/{connector_type}/minted",
            display_name=f"{connector_type}-minted",
        )
    )
    monkeypatch.setattr(
        "application_sdk.testing.e2e.base.harness_seed.seed_assets", _record_seed
    )
    monkeypatch.setattr(
        "application_sdk.testing.e2e.base.purge_connection", _record_purge
    )
    monkeypatch.setattr(
        "application_sdk.testing.e2e.base.delete_prefix", _record_delete
    )
    monkeypatch.setattr(
        _SeedingE2ETest, "_atlas_client", lambda self: _null_atlas_client()
    )
    monkeypatch.setattr(_SeedingE2ETest, "seed_object_store", lambda self: object())
    return harness, seeded, purged, deleted


class TestSeedAssetsRegistry:
    """Every seeded connection is registered, and registered up front."""

    def test_the_seeded_qn_is_recorded_and_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, seeded, _purged, _deleted = _harness(monkeypatch)
        report = harness.seed_assets(_spec())
        assert report.qualified_name == _SEED_QN
        assert seeded == [_SEED_QN]
        assert harness._seeded_connection_qns == [_SEED_QN]

    def test_the_object_store_prefix_is_registered_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The NDJSON is the other thing a seed leaves on a shared tenant."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        harness.seed_assets(_spec())
        assert harness._seeded_prefixes == [_SEED_PREFIX]

    def test_a_none_qn_is_minted_from_the_specs_connector_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, seeded, _purged, _deleted = _harness(monkeypatch)
        report = harness.seed_assets(_spec(qualified_name=None))
        assert report.qualified_name == "default/snowflake/minted"
        assert seeded == ["default/snowflake/minted"]

    def test_a_seed_that_fails_is_still_registered_for_teardown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The NDJSON may already be uploaded — and the connection created by
        publish — by the time the run fails; registering on success would leave
        exactly those half-set-up artifacts behind."""
        harness, _seeded, _purged, _deleted = _harness(
            monkeypatch, seed_error=RuntimeError("publish rejected the batch")
        )
        with pytest.raises(RuntimeError):
            harness.seed_assets(_spec())
        assert harness._seeded_connection_qns == [_SEED_QN]
        assert harness._seeded_prefixes == [_SEED_PREFIX]

    def test_a_bad_segment_is_rejected_before_anything_is_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validation is the whole point of the spec; a segment that cannot
        compose must not reach the tenant, or the registry."""
        harness, seeded, _purged, _deleted = _harness(monkeypatch)
        spec = harness_seed.SeedSpec(
            connector_type="snowflake",
            qualified_name=_SEED_QN,
            display_name="snowflake-seed",
            databases=(harness_seed.DatabaseSpec(name="A/B"),),
        )
        with pytest.raises(harness_seed.SeedSegmentInvalidError):
            harness.seed_assets(spec)
        assert seeded == []
        assert harness._seeded_connection_qns == []
        assert harness._seeded_prefixes == []


class TestPurgeIncludesSeededConnections:
    """Teardown reaches every connection the run minted, its own first."""

    def test_seeded_connections_are_purged_after_the_runs_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _seeded, purged, _deleted = _harness(monkeypatch)
        harness.seed_assets(_spec())
        harness.teardown_method(method=None)
        assert purged == [_RUN_QN, _SEED_QN]

    def test_the_seed_prefix_is_deleted_after_the_connections(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _seeded, _purged, deleted = _harness(monkeypatch)
        harness.seed_assets(_spec())
        harness.teardown_method(method=None)
        assert deleted == [_SEED_PREFIX]

    def test_a_run_that_seeded_nothing_purges_only_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _seeded, purged, deleted = _harness(monkeypatch)
        harness.teardown_method(method=None)
        assert purged == [_RUN_QN]
        assert deleted == []

    def test_one_failing_purge_does_not_orphan_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report-not-raise, per connection: the run's own purge failing must
        not skip the seeded ones, and none of it may replace the verdict."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
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

    def test_an_unreachable_store_still_leaves_the_connections_purged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bytes left behind are strictly less harmful than a replaced verdict,
        and the entities are what a later run trips over."""
        harness, _seeded, purged, _deleted = _harness(monkeypatch)
        harness.seed_assets(_spec())
        monkeypatch.setattr(
            _SeedingE2ETest,
            "seed_object_store",
            lambda self: (_ for _ in ()).throw(RuntimeError("no binding")),
        )
        harness.teardown_method(method=None)
        assert purged == [_RUN_QN, _SEED_QN]


class TestPerRunConnection:
    """``DAGSpec.connection_qualified_name`` rebinds the run and is reclaimed."""

    _OTHER_QN = "default/snowflake/1787587123106597"

    def test_the_active_run_rebinds_the_connection_and_restores_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        with harness._dag_run(DAGSpec(connection_qualified_name=self._OTHER_QN)) as dag:
            assert dag.connection_qualified_name == self._OTHER_QN
            assert harness.connection_qualified_name == self._OTHER_QN
        assert harness.connection_qualified_name == _RUN_QN

    def test_a_run_that_names_no_connection_keeps_the_suites_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        with harness._dag_run(DAGSpec()) as dag:
            assert dag.connection_qualified_name == _RUN_QN
            assert harness.connection_qualified_name == _RUN_QN
        assert harness._seeded_connection_qns == []

    def test_a_named_connection_joins_the_teardown_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _seeded, purged, _deleted = _harness(monkeypatch)
        with harness._dag_run(DAGSpec(connection_qualified_name=self._OTHER_QN)):
            pass
        harness.teardown_method(method=None)
        assert purged == [_RUN_QN, self._OTHER_QN]

    def test_the_same_connection_is_registered_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two runs against one prepared connection purge it once, not twice."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        for _ in range(2):
            with harness._dag_run(DAGSpec(connection_qualified_name=self._OTHER_QN)):
                pass
        assert harness._seeded_connection_qns == [self._OTHER_QN]
