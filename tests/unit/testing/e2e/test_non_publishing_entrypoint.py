"""Tests for the non-publishing-entrypoint path: expect_connection + seeding.

A bundle app's second entrypoint (a query-history miner, a marker promotion)
publishes no connection inventory, so the crawler-shaped assertion ladder in
``test_full_dag_runs_end_to_end`` can never pass for it. ``expect_connection``
drops that ladder; ``seed_prerequisites`` / ``seed_connection`` let such a suite
put the state it consumes in place first.

Two properties matter most here and are asserted explicitly rather than left
implied:

* **Inertness.** A suite that declares nothing behaves exactly as before. Every
  test class below that omits ``expect_connection`` is a control.
* **Isolation.** ``seed_connection`` writes under the harness's OWN ephemeral
  qualified name, so ``teardown_method`` purges it. A regression to a shared,
  surviving connection would let a half-set-up left-over green a later run, and
  ``TestSeededConnectionIsTornDown`` is the test that catches it.

No tenant needed: the AE client and pyatlan are both stubbed.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from application_sdk.testing.e2e._errors import (
    SeededConnectionNotSearchableError,
    UnknownConnectorTypeError,
)
from application_sdk.testing.e2e.base import BaseE2ETest, FullDAGOutcome
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)


def _node(name: str, status: DAGNodeStatus) -> DAGNodeResult:
    return DAGNodeResult(
        name=name,
        status=status,
        started_at_ms=None,
        completed_at_ms=None,
        error_message=None,
    )


def _succeeded(*names: str) -> DAGRunResult:
    return DAGRunResult(
        run_id="r",
        workflow_slug="s",
        status=DAGRunStatus.SUCCEEDED,
        nodes=[_node(n, DAGNodeStatus.SUCCEEDED) for n in names],
    )


def _failed(*names: str) -> DAGRunResult:
    return DAGRunResult(
        run_id="r",
        workflow_slug="s",
        status=DAGRunStatus.FAILED,
        nodes=[_node(n, DAGNodeStatus.FAILED) for n in names],
    )


class _Crawler(BaseE2ETest):
    """Control: declares nothing, so it must grade exactly as it did before."""

    connector_short_name = "bundle"
    argo_package_name = "@atlan/crawler"
    argo_template_name = "atlan-crawler"


class _Miner(BaseE2ETest):
    """A miner: one-node DAG, publishes no inventory."""

    connector_short_name = "bundle"
    argo_package_name = "@atlan/miner"
    argo_template_name = "atlan-miner"
    expect_connection = False
    expect_lineage = False
    require_nonempty_assets = False
    required_dag_nodes = ("extract",)


# ---------------------------------------------------------------------------
# The default is inert
# ---------------------------------------------------------------------------


class TestDefaultsAreUnchanged:
    """expect_connection defaults True, and the seeding hook defaults to nothing."""

    def test_expect_connection_defaults_true(self) -> None:
        assert BaseE2ETest.expect_connection is True
        assert _Crawler.expect_connection is True

    def test_seed_prerequisites_default_is_a_no_op(self) -> None:
        harness = _Crawler()
        # Nothing to assert but the absence of an effect: no attributes are set,
        # nothing raises, and no connection QN is invented.
        harness.seed_prerequisites()
        assert not hasattr(harness, "connection_qualified_name")

    def test_outcome_connection_expected_defaults_true(self) -> None:
        """An outcome built without the new field grades exactly as before."""
        outcome = FullDAGOutcome(
            ae_result=_succeeded("extract", "publish"),
            connection_qualified_name="default/bundle/1",
            connection_in_atlas=True,
        )
        assert outcome.connection_expected is True
        assert outcome.succeeded is True


# ---------------------------------------------------------------------------
# FullDAGOutcome.succeeded
# ---------------------------------------------------------------------------


class TestOutcomeSucceeded:
    """The Connection clause drops out only when it was never expected."""

    def test_missing_connection_fails_when_expected(self) -> None:
        outcome = FullDAGOutcome(
            ae_result=_succeeded("extract", "publish"),
            connection_qualified_name="default/bundle/1",
            connection_in_atlas=False,
            connection_expected=True,
        )
        assert outcome.succeeded is False

    def test_missing_connection_ignored_when_not_expected(self) -> None:
        outcome = FullDAGOutcome(
            ae_result=_succeeded("extract"),
            connection_qualified_name="default/bundle/1",
            connection_in_atlas=False,
            connection_expected=False,
        )
        assert outcome.succeeded is True

    def test_a_failed_dag_still_fails_without_a_connection_expectation(self) -> None:
        """expect_connection must not weaken the DAG gate — only the Atlas one."""
        outcome = FullDAGOutcome(
            ae_result=_failed("extract"),
            connection_qualified_name="default/bundle/1",
            connection_in_atlas=False,
            connection_expected=False,
        )
        assert outcome.succeeded is False


# ---------------------------------------------------------------------------
# run_full_dag skips the Atlas probes
# ---------------------------------------------------------------------------


def _stub_run(harness: BaseE2ETest, ae_result: DAGRunResult) -> MagicMock:
    """Wire a harness so run_full_dag reaches the Atlas branch without a tenant."""
    client = MagicMock()
    client.submit_workflow.return_value = "run-1"
    client.poll_native_status.return_value = ae_result
    harness.client = client  # type: ignore[attr-defined]
    harness.run_id = 1  # type: ignore[attr-defined]
    harness.connection_qualified_name = "default/bundle/1"  # type: ignore[attr-defined]
    harness._bootstrap_workflow = lambda: "slug"  # type: ignore[method-assign]
    harness._build_ae_payload = lambda slug: {}  # type: ignore[method-assign]
    harness._extract_task_queue = lambda: "atlan-bundle-default"  # type: ignore[method-assign]
    return client


class TestRunFullDagSkipsAtlasProbes:
    def test_no_atlas_call_when_connection_not_expected(self) -> None:
        harness = _Miner()
        client = _stub_run(harness, _succeeded("extract"))

        outcome = harness.run_full_dag()

        client.poll_atlas_for_connection.assert_not_called()
        client.count_assets_under_connection.assert_not_called()
        client.count_total_assets_under_connection.assert_not_called()
        client.count_lineage_under_connection.assert_not_called()
        # Not observed, so reported as not observed — never invented as True.
        assert outcome.connection_in_atlas is False
        assert outcome.connection_expected is False
        assert outcome.succeeded is True

    def test_atlas_is_still_polled_for_a_crawler(self) -> None:
        """The control: the probe path is untouched when nothing is declared."""
        harness = _Crawler()
        client = _stub_run(harness, _succeeded("extract", "publish"))
        client.poll_atlas_for_connection.return_value = True
        client.count_total_assets_under_connection.return_value = 7
        client.count_lineage_under_connection.return_value = {"Process": 1}

        outcome = harness.run_full_dag()

        client.poll_atlas_for_connection.assert_called_once()
        assert outcome.connection_in_atlas is True
        assert outcome.connection_expected is True
        assert outcome.total_assets == 7


# ---------------------------------------------------------------------------
# test_full_dag_runs_end_to_end
# ---------------------------------------------------------------------------


class TestAssertionLadder:
    def test_miner_passes_on_the_dag_alone(self) -> None:
        harness = _Miner()
        harness.source_available = True  # type: ignore[attr-defined]
        _stub_run(harness, _succeeded("extract"))

        # Would raise on the zero-asset backstop if the inventory assertions ran.
        harness.test_full_dag_runs_end_to_end()

    def test_miner_still_fails_on_a_failed_dag(self) -> None:
        harness = _Miner()
        harness.source_available = True  # type: ignore[attr-defined]
        _stub_run(harness, _failed("extract"))

        with pytest.raises(AssertionError) as exc:
            harness.test_full_dag_runs_end_to_end()
        # The connection line must not send the reader hunting a connection that
        # was never meant to exist.
        assert "not applicable (expect_connection=False)" in str(exc.value)

    def test_seed_prerequisites_runs_before_the_dag(self) -> None:
        calls: list[str] = []

        class _Seeding(_Miner):
            def seed_prerequisites(self) -> None:
                calls.append("seed")

        harness = _Seeding()
        harness.source_available = True  # type: ignore[attr-defined]
        _stub_run(harness, _succeeded("extract"))
        original = harness.run_full_dag

        def _tracked() -> FullDAGOutcome:
            calls.append("run")
            return original()

        harness.run_full_dag = _tracked  # type: ignore[method-assign]
        harness.test_full_dag_runs_end_to_end()

        assert calls == ["seed", "run"], (
            "seed_prerequisites must run exactly once, before run_full_dag — "
            "state seeded after the DAG starts is state the DAG cannot see"
        )

    def test_crawler_still_fails_without_a_connection(self) -> None:
        """The control: the Atlas clause still gates a publishing entrypoint."""
        harness = _Crawler()
        harness.source_available = True  # type: ignore[attr-defined]
        client = _stub_run(harness, _succeeded("extract", "publish"))
        client.poll_atlas_for_connection.return_value = False

        with pytest.raises(AssertionError) as exc:
            harness.test_full_dag_runs_end_to_end()
        assert "Connection in Atlas? False" in str(exc.value)


# ---------------------------------------------------------------------------
# seed_connection
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Stand-in for pyatlan's Connection, which assigns the QN client-side."""

    def __init__(self, qualified_name: str) -> None:
        self.qualified_name = qualified_name

    @classmethod
    def creator(cls, **kwargs: Any) -> _FakeConnection:
        cls.last_kwargs = kwargs  # type: ignore[attr-defined]
        return cls("default/miner-source/9999")


@pytest.fixture
def fake_pyatlan(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install stub pyatlan modules so seed_connection's lazy imports resolve.

    seed_connection imports pyatlan inside the method body (it is a heavy,
    testing-time-only dependency), so the stubs must be in ``sys.modules``.
    """
    saved_client = MagicMock()

    client_mod = types.ModuleType("pyatlan.client.atlan")
    client_mod.AtlanClient = MagicMock(return_value=saved_client)  # type: ignore[attr-defined]

    assets_mod = types.ModuleType("pyatlan.model.assets")
    assets_mod.Connection = _FakeConnection  # type: ignore[attr-defined]
    # teardown_method imports Asset + FluentSearch from the same package. They
    # belong in this fixture rather than a per-test injection: without them the
    # import fails, teardown's broad except swallows it as a warning, and the
    # isolation test below would report "not purged" for the wrong reason.
    assets_mod.Asset = MagicMock()  # type: ignore[attr-defined]

    search_mod = types.ModuleType("pyatlan.model.fluent_search")
    search_mod.FluentSearch = MagicMock()  # type: ignore[attr-defined]

    class _ConnectorType:
        def __init__(self, value: str) -> None:
            if value not in ("miner-source", "api"):
                raise ValueError(value)
            self.value = value

    enums_mod = types.ModuleType("pyatlan.model.enums")
    enums_mod.AtlanConnectorType = _ConnectorType  # type: ignore[attr-defined]

    for name, mod in (
        ("pyatlan.client.atlan", client_mod),
        ("pyatlan.model.assets", assets_mod),
        ("pyatlan.model.enums", enums_mod),
        ("pyatlan.model.fluent_search", search_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    monkeypatch.setenv("ATLAN_BASE_URL", "https://tenant.example.com")
    monkeypatch.setenv("ATLAN_API_KEY", "token")
    return saved_client


class _SeedingMiner(_Miner):
    connection_type = "miner-source"
    atlas_poll_interval_seconds = 0
    atlas_poll_timeout_seconds = 1


def _seeding_harness() -> _SeedingMiner:
    harness = _SeedingMiner()
    harness.connection_qualified_name = "default/miner-source/minted"  # type: ignore[attr-defined]
    harness.connection_display_name = "miner-source-minted"  # type: ignore[attr-defined]
    harness._auto_admin_roles = ("role-guid",)  # type: ignore[attr-defined]
    harness._auto_admin_users = ("svc",)  # type: ignore[attr-defined]
    harness.client = MagicMock()  # type: ignore[attr-defined]
    harness.client.poll_atlas_for_connection.return_value = True
    return harness


class TestSeedConnection:
    def test_adopts_the_creator_assigned_qualified_name(
        self, fake_pyatlan: MagicMock
    ) -> None:
        harness = _seeding_harness()

        returned = harness.seed_connection()

        assert returned == "default/miner-source/9999"
        # Adopted onto the instance, because that is what the AE payload, the
        # Atlas polls, and the teardown purge all read.
        assert harness.connection_qualified_name == "default/miner-source/9999"
        fake_pyatlan.asset.save.assert_called_once()

    def test_passes_the_resolved_admin_acl(self, fake_pyatlan: MagicMock) -> None:
        harness = _seeding_harness()
        harness.seed_connection()

        kwargs = _FakeConnection.last_kwargs  # type: ignore[attr-defined]
        assert kwargs["admin_roles"] == ["role-guid"]
        assert kwargs["admin_users"] == ["svc"]

    def test_unknown_connector_type_names_the_fix(
        self, fake_pyatlan: MagicMock
    ) -> None:
        class _BadType(_SeedingMiner):
            connection_type = "not-a-real-connector"

        harness = _BadType()
        harness.connection_display_name = "x"  # type: ignore[attr-defined]
        harness._auto_admin_roles = ()  # type: ignore[attr-defined]
        harness._auto_admin_users = ()  # type: ignore[attr-defined]

        with pytest.raises(UnknownConnectorTypeError) as exc:
            harness.seed_connection()
        assert "connection_type" in str(exc.value)

    def test_unsearchable_seed_fails_fast(self, fake_pyatlan: MagicMock) -> None:
        harness = _seeding_harness()
        harness.client.poll_atlas_for_connection.return_value = False

        with pytest.raises(SeededConnectionNotSearchableError):
            harness.seed_connection()

    def test_probe_is_retried_until_policies_go_live(
        self, fake_pyatlan: MagicMock
    ) -> None:
        harness = _seeding_harness()
        harness.atlas_poll_timeout_seconds = 30  # type: ignore[misc]
        attempts: list[int] = []

        def _probe() -> None:
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("403 — connection policies not live yet")

        harness.seed_connection(probe=_probe)

        assert len(attempts) == 3

    def test_probe_that_never_succeeds_propagates(
        self, fake_pyatlan: MagicMock
    ) -> None:
        """A seed that never became writable must fail, not run anyway."""
        harness = _seeding_harness()
        harness.atlas_poll_timeout_seconds = 0  # type: ignore[misc]

        def _probe() -> None:
            raise PermissionError("403 forever")

        with pytest.raises(PermissionError):
            harness.seed_connection(probe=_probe)

    @pytest.mark.parametrize("exc_type", [TypeError, ValueError])
    def test_a_non_transient_probe_error_fails_fast(
        self, fake_pyatlan: MagicMock, exc_type: type[Exception]
    ) -> None:
        """A deterministic probe bug must not burn the whole timeout retrying.

        A wrong call signature or a bad config value raises identically on
        every attempt, so the retry loop re-raises it on first sight rather
        than waiting out ``atlas_poll_timeout_seconds``.
        """
        harness = _seeding_harness()
        harness.atlas_poll_timeout_seconds = 1500  # would hang if retried
        attempts: list[int] = []

        def _probe() -> None:
            attempts.append(1)
            raise exc_type("probe bug — not transient")

        with pytest.raises(exc_type):
            harness.seed_connection(probe=_probe)

        assert len(attempts) == 1


class TestSeededConnectionIsTornDown:
    """The isolation guarantee.

    A seeded connection must be purged like a crawler-created one. If this test
    ever fails, the design has regressed toward a surviving shared connection —
    which is exactly what lets a half-set-up left-over green a later run.
    """

    def test_teardown_purges_the_seeded_connection(
        self, fake_pyatlan: MagicMock
    ) -> None:
        harness = _seeding_harness()
        seeded_qn = harness.seed_connection()

        # teardown_method resolves its own client; give it the same stub and a
        # searchable connection to purge.
        connection_asset = MagicMock()
        connection_asset.guid = "conn-guid"
        fake_pyatlan.asset.search.return_value = [connection_asset]

        harness.teardown_method(None)

        purged = [
            call.args[0] for call in fake_pyatlan.asset.purge_by_guid.call_args_list
        ]
        assert "conn-guid" in purged, (
            f"seeded connection {seeded_qn} was not purged — a surviving "
            "connection breaks run isolation"
        )
