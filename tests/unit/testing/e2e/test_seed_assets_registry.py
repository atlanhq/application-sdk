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

Since FND-1724 the registry is drained through the ``connection-delete`` app
rather than through ``pyatlan``, so what the wiring has to show has one more
step in it: every registered connection reaches
:func:`~application_sdk.testing.harness.teardown.delete_connection`, a delete
that does not complete falls back to the runner-side purge (and only then), and
the seed NDJSON is deleted **by key** — the bulk-prefix delete it replaced is
what the tenant's s3proxy answers with a 403.
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
from application_sdk.testing.harness.teardown import (
    ConnectionDeletePlan,
    ConnectionDeleteReport,
    DeleteType,
)

_RUN_QN = "default/openapi/1787587123106596"
_SEED_QN = "default/snowflake/1787587123106596"
_SEED_PREFIX = "artifacts/apps/openapi/e2e-seed/default%2Fsnowflake%2F1787587123106596"
#: The one key the harness itself writes under that root. Teardown deletes this,
#: not the root: see :func:`~application_sdk.testing.harness.seed.seed_object_keys`.
_SEED_KEY = f"{_SEED_PREFIX}/transformed/assets.json"


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
    """A minimal but non-empty spec — an empty tree is rejected at resolve."""
    return harness_seed.SeedSpec(
        connector_type="snowflake",
        qualified_name=qualified_name,
        display_name="snowflake-seed",
        admin_roles=("role-guid",),
        databases=(harness_seed.DatabaseSpec(name="ANALYTICS"),),
    )


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed_error: BaseException | None = None,
    delete_reports: dict[str, ConnectionDeleteReport] | None = None,
) -> tuple[_SeedingE2ETest, list[str], list[str], list[str]]:
    """A harness whose seed, delete, purge and object delete are recorded.

    ``plans``, ``specs`` and ``deleted_connections`` are exposed on the harness
    rather than returned: only a few tests read them, and widening the tuple
    would touch every call site.

    Args:
        monkeypatch: pytest's patcher.
        seed_error: Raised by the patched ``seed_assets`` instead of seeding.
        delete_reports: Per-QN outcome for the ``connection-delete`` app path.
            A QN with no entry succeeds, which is the shape every test that is
            not about the fallback wants — the fallback purge then runs for
            exactly the connections named here, and for no others.
    """
    seeded: list[str] = []
    purged: list[str] = []
    deleted: list[str] = []
    deleted_connections: list[str] = []
    plans: list[harness_seed.SeedPublishPlan] = []
    delete_plans: list[ConnectionDeletePlan] = []
    specs: list[harness_seed.ResolvedSeedSpec] = []

    async def _record_seed(
        spec: harness_seed.ResolvedSeedSpec, **wiring: Any
    ) -> harness_seed.SeededConnection:
        if seed_error is not None:
            raise seed_error
        seeded.append(spec.qualified_name)
        specs.append(spec)
        plans.append(wiring["plan"])
        return harness_seed.SeededConnection(qualified_name=spec.qualified_name)

    async def _record_delete_connection(
        qualified_name: str, **wiring: Any
    ) -> ConnectionDeleteReport:
        deleted_connections.append(qualified_name)
        delete_plans.append(wiring["plan"])
        scripted = (delete_reports or {}).get(qualified_name)
        return scripted or ConnectionDeleteReport(
            qualified_name=qualified_name, succeeded=True
        )

    async def _record_purge(client: object, connection_qualified_name: str) -> object:
        purged.append(connection_qualified_name)
        return SimpleNamespace(purged=1, orphaned=(), errors=())

    async def _record_delete(key: str, _store: object) -> bool:
        deleted.append(key)
        return True

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
        "application_sdk.testing.e2e.base.delete_connection", _record_delete_connection
    )
    monkeypatch.setattr(
        "application_sdk.testing.e2e.base.purge_connection", _record_purge
    )
    monkeypatch.setattr(
        "application_sdk.testing.e2e.base.delete_object", _record_delete
    )
    monkeypatch.setattr(
        _SeedingE2ETest, "_atlas_client", lambda self: _null_atlas_client()
    )
    monkeypatch.setattr(_SeedingE2ETest, "seed_object_store", lambda self: object())
    harness.recorded_plans = plans
    harness.recorded_specs = specs
    harness.recorded_delete_plans = delete_plans
    harness.deleted_connections = deleted_connections
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

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_an_empty_qn_is_rejected_rather_than_minted(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        """``None`` is the one omission sentinel. An empty or blank string is a
        *supplied* value, and one the spec rejects — falling back to the minter
        on it would paper over the caller's mistake with a valid QN, and the
        seed would publish under a connection nobody asked for while the
        connector's refs still named the empty one."""
        harness, seeded, _purged, _deleted = _harness(monkeypatch)
        with pytest.raises(harness_seed.SeedSegmentInvalidError):
            harness.seed_assets(_spec(qualified_name=blank))
        assert seeded == []
        assert harness._seeded_connection_qns == []

    def test_an_empty_display_name_is_not_replaced_by_the_minted_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same rule on the other field. An empty display name is not a
        validation failure, so the only way to see it was honoured is to read
        the resolved spec back — asserting on anything derived from
        ``connector_type`` (the workflow name, say) would stay green through a
        regression that minted over it."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        spec = harness_seed.SeedSpec(
            connector_type="snowflake",
            qualified_name=_SEED_QN,
            display_name="",
            admin_roles=("role-guid",),
            databases=(harness_seed.DatabaseSpec(name="ANALYTICS"),),
        )
        harness.seed_assets(spec)
        assert harness.recorded_specs[0].display_name == ""

    def test_a_none_display_name_is_minted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other side of the same assertion — without it, "not replaced"
        could pass on a resolver that never mints at all."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        spec = harness_seed.SeedSpec(
            connector_type="snowflake",
            qualified_name=_SEED_QN,
            display_name=None,
            admin_roles=("role-guid",),
            databases=(harness_seed.DatabaseSpec(name="ANALYTICS"),),
        )
        harness.seed_assets(spec)
        assert harness.recorded_specs[0].display_name == "snowflake-minted"


class TestSeedWorkflowNames:
    """``create_workflow`` is idempotent on the name, so names must be unique."""

    def test_two_seeds_of_one_type_get_distinct_workflow_names(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A suite seeding two connections of the same type — two warehouses,
        two accounts — would otherwise reuse one slug, publish each graph over
        the other, and leave an AE run list that cannot tell them apart. Same
        collision ``_ae_workflow_name_suffix`` solves for multi-DAGSpec runs."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        harness.seed_assets(_spec(qualified_name="default/snowflake/111"))
        harness.seed_assets(_spec(qualified_name="default/snowflake/222"))
        first, second = (plan.ae_workflow_name for plan in harness.recorded_plans)
        assert first != second

    def test_the_name_separates_a_seed_from_the_suites_own_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the same rule: sharing the suite's own workflow
        name would publish the seed's one-node graph over the connector's DAG."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        harness.seed_assets(_spec())
        name = harness.recorded_plans[0].ae_workflow_name
        assert "-seed-" in name
        assert name.endswith("-snowflake")

    def test_a_dag_run_between_two_seeds_cannot_collide_them(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinal is the length of the teardown registry, which a
        ``DAGSpec``-named connection also appends to — so it counts registry
        entries, not seeds, and the second seed here is ordinal 3 rather than 2.
        That is fine and is what this pins: the registry only ever grows within
        a run, so the ordinals cannot repeat whatever else lands in it. Reading
        the count off a shared structure is only safe while that holds."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        harness.seed_assets(_spec(qualified_name="default/snowflake/111"))
        with harness._dag_run(
            DAGSpec(connection_qualified_name="default/snowflake/999")
        ):
            pass
        harness.seed_assets(_spec(qualified_name="default/snowflake/222"))
        names = [plan.ae_workflow_name for plan in harness.recorded_plans]
        assert len(set(names)) == 2
        assert names[0].endswith("-seed-1-snowflake")
        assert names[1].endswith("-seed-3-snowflake")


class TestTeardownIncludesSeededConnections:
    """Teardown reaches every connection the run minted, its own first.

    Through the ``connection-delete`` app since FND-1724 — which is what makes
    "reached" mean the byte-stores as well as Atlas. The runner-side purge is
    the fallback, and each test here says which of the two it expects, because
    a fallback that fired silently on every connection would look identical to
    the app path from the outside.
    """

    def test_seeded_connections_are_deleted_after_the_runs_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run's assets hold lineage refs *into* the seeded skeletons, so
        the referrer goes first — the direction that cannot strand an edge."""
        harness, _seeded, purged, _deleted = _harness(monkeypatch)
        harness.seed_assets(_spec())
        harness.teardown_method(method=None)
        assert harness.deleted_connections == [_RUN_QN, _SEED_QN]
        assert purged == []

    def test_each_connection_gets_its_own_addressed_delete_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One single-node run per connection, each named apart from the other:
        ``create_workflow`` is idempotent on the name, so two teardowns sharing
        one would publish each graph over the other."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        harness.seed_assets(_spec())
        harness.teardown_method(method=None)
        names = [plan.ae_workflow_name for plan in harness.recorded_delete_plans]
        assert len(set(names)) == 2
        assert all("-teardown-" in name for name in names)
        assert {plan.task_queue for plan in harness.recorded_delete_plans} == {
            "atlan-connection-delete-production"
        }
        assert {plan.delete_type for plan in harness.recorded_delete_plans} == {
            DeleteType.PURGE
        }

    def test_the_seed_object_is_deleted_by_key_after_the_connections(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """By key, not by prefix. A bulk-prefix delete is a bucket-level URL the
        tenant's s3proxy answers with ``403 code 1009`` however allowlisted the
        prefix is — which is why the old teardown never deleted this at all."""
        harness, _seeded, _purged, deleted = _harness(monkeypatch)
        harness.seed_assets(_spec())
        harness.teardown_method(method=None)
        assert deleted == [_SEED_KEY]

    def test_a_run_that_seeded_nothing_deletes_only_its_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _seeded, _purged, deleted = _harness(monkeypatch)
        harness.teardown_method(method=None)
        assert harness.deleted_connections == [_RUN_QN]
        assert deleted == []

    def test_an_absent_app_falls_back_to_the_runner_purge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``connection-delete`` is a marketplace utility, so a tenant may
        simply not have it. Leaking a whole connection is worse than leaking
        bytes, so the Atlas half is still reclaimed — and never by raising."""
        harness, _seeded, purged, _deleted = _harness(
            monkeypatch,
            delete_reports={
                _RUN_QN: ConnectionDeleteReport(
                    qualified_name=_RUN_QN,
                    app_absent=True,
                    errors=("nothing polled atlan-connection-delete-production",),
                )
            },
        )
        harness.teardown_method(method=None)
        assert harness.deleted_connections == [_RUN_QN]
        assert purged == [_RUN_QN]

    def test_one_incomplete_delete_does_not_orphan_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report-not-raise, per connection: the run's own delete failing must
        not skip the seeded ones, and none of it may replace the verdict. Only
        the connection that failed is purged from the runner — a fallback that
        ran for the others too would re-walk what the app had already taken."""
        harness, _seeded, purged, _deleted = _harness(
            monkeypatch,
            delete_reports={
                _RUN_QN: ConnectionDeleteReport(
                    qualified_name=_RUN_QN, errors=("tenant hiccup",)
                )
            },
        )
        harness.seed_assets(_spec())
        harness.teardown_method(method=None)
        assert harness.deleted_connections == [_RUN_QN, _SEED_QN]
        assert purged == [_RUN_QN]

    def test_a_tier_without_an_ae_client_still_reclaims_atlas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worker-up-only tier wires no AE client, so there is nothing to
        submit a delete through — and a connection it minted still has to go."""
        harness, _seeded, purged, _deleted = _harness(monkeypatch)
        del harness._ae
        harness.teardown_method(method=None)
        assert harness.deleted_connections == []
        assert purged == [_RUN_QN]

    def test_an_unreachable_store_still_leaves_the_connections_deleted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bytes left behind are strictly less harmful than a replaced verdict,
        and the entities are what a later run trips over."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        harness.seed_assets(_spec())
        monkeypatch.setattr(
            _SeedingE2ETest,
            "seed_object_store",
            lambda self: (_ for _ in ()).throw(RuntimeError("no binding")),
        )
        harness.teardown_method(method=None)
        assert harness.deleted_connections == [_RUN_QN, _SEED_QN]


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
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        with harness._dag_run(DAGSpec(connection_qualified_name=self._OTHER_QN)):
            pass
        harness.teardown_method(method=None)
        assert harness.deleted_connections == [_RUN_QN, self._OTHER_QN]

    def test_the_same_connection_is_registered_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two runs against one prepared connection delete it once, not twice."""
        harness, _seeded, _purged, _deleted = _harness(monkeypatch)
        for _ in range(2):
            with harness._dag_run(DAGSpec(connection_qualified_name=self._OTHER_QN)):
                pass
        assert harness._seeded_connection_qns == [self._OTHER_QN]
