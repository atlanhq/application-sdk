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

No tenant needed. What is stubbed is the *seam*, not pyatlan: since child H the
base class calls the harness' own Atlas functions, so these tests replace those
functions and assert the wiring — which qualified name is created, which budget
the poll gets, whether the purge runs. What those functions do with pyatlan is
pinned in ``tests/unit/testing/harness/atlas/``, against one shared double, so
the two halves cannot drift into disagreeing about the seam between them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

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
from application_sdk.testing.harness import atlas as atlas_api
from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import Settled


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


class _FakeAE:
    """Async stand-in for the AE client the base holds."""

    def __init__(self, ae_result: DAGRunResult) -> None:
        self._ae_result = ae_result

    async def submit_workflow(self, payload: dict[str, Any], **_kwargs: Any) -> str:
        return "run-1"

    async def poll_native_status(self, run_id: str, **_kwargs: Any) -> DAGRunResult:
        return self._ae_result

    async def get_published_version(self, slug: str) -> None:
        return None

    async def probe_run_is_listed(self, slug: str, run_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


@dataclass
class _AtlasCalls:
    """Which Atlas reads the run actually made, and what they were told to say.

    A recorder rather than a mock so the assertions read as claims about the
    run ("the connection was never probed") instead of as claims about a
    library ("assert_not_called").
    """

    connection_found: bool = True
    total: int = 0
    lineage: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    polled_connection: list[str] = field(default_factory=list)
    counted: list[Sequence[str]] = field(default_factory=list)
    counted_total: list[str] = field(default_factory=list)
    counted_lineage: list[str] = field(default_factory=list)
    sampled: list[Sequence[str]] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    purged: list[str] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace the Atlas seam ``BaseE2ETest`` calls, for one test."""

        def _settled(value: Any) -> Settled[Any]:
            return Settled(label="fake", attempts=1, elapsed=timedelta(0), value=value)

        async def _poll_for_connection(
            _client: object, qualified_name: str, *, budget: Budget
        ) -> Any:
            self.polled_connection.append(qualified_name)
            return _settled(self.connection_found)

        async def _count_assets(
            _client: object, _qn: str, type_names: Sequence[str]
        ) -> Any:
            self.counted.append(tuple(type_names))
            return _settled({name: self.counts.get(name, 0) for name in type_names})

        async def _count_total(_client: object, qn: str) -> Any:
            self.counted_total.append(qn)
            return _settled(self.total)

        async def _count_lineage(
            _client: object, qn: str, _type_names: Sequence[str]
        ) -> Any:
            self.counted_lineage.append(qn)
            return _settled(dict(self.lineage))

        async def _sample(
            _client: object, _qn: str, type_names: Sequence[str], **_kwargs: Any
        ) -> Any:
            self.sampled.append(tuple(type_names))
            return _settled({name: [] for name in type_names})

        async def _create_connection(_client: object, **kwargs: Any) -> str:
            self.created.append(kwargs)
            return str(kwargs["qualified_name"])

        async def _purge(_client: object, qualified_name: str) -> Any:
            self.purged.append(qualified_name)
            return None

        monkeypatch.setattr(atlas_api, "poll_for_connection", _poll_for_connection)
        monkeypatch.setattr(atlas_api, "count_assets", _count_assets)
        monkeypatch.setattr(atlas_api, "count_total_assets", _count_total)
        monkeypatch.setattr(atlas_api, "count_lineage", _count_lineage)
        monkeypatch.setattr(atlas_api, "sample_qualified_names", _sample)
        monkeypatch.setattr(atlas_api, "create_connection", _create_connection)
        monkeypatch.setattr("application_sdk.testing.e2e.base.purge_connection", _purge)
        monkeypatch.setattr(
            BaseE2ETest, "_atlas_client", lambda _self: _null_atlas_client()
        )


@asynccontextmanager
async def _null_atlas_client() -> AsyncIterator[object]:
    """The client the replaced Atlas functions never look at."""
    yield object()


def _stub_run(
    harness: BaseE2ETest,
    ae_result: DAGRunResult,
    monkeypatch: pytest.MonkeyPatch,
    **atlas_overrides: Any,
) -> _AtlasCalls:
    """Wire a harness so run_full_dag reaches the Atlas branch without a tenant."""
    calls = _AtlasCalls(**atlas_overrides)
    calls.install(monkeypatch)
    harness._ae = _FakeAE(ae_result)  # type: ignore[attr-defined]
    harness.run_id = 1  # type: ignore[attr-defined]
    harness.connection_qualified_name = "default/bundle/1"  # type: ignore[attr-defined]

    async def _bootstrap() -> str:
        return "slug"

    harness._bootstrap_workflow = _bootstrap  # type: ignore[method-assign]
    harness._build_ae_payload = lambda slug: {}  # type: ignore[method-assign]
    harness._extract_task_queue = lambda: "atlan-bundle-default"  # type: ignore[method-assign]
    return calls


class TestRunFullDagSkipsAtlasProbes:
    def test_no_atlas_call_when_connection_not_expected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Miner()
        calls = _stub_run(harness, _succeeded("extract"), monkeypatch)

        outcome = harness.run_full_dag()

        assert calls.polled_connection == []
        assert calls.counted == []
        assert calls.counted_total == []
        assert calls.counted_lineage == []
        # Not observed, so reported as not observed — never invented as True.
        assert outcome.connection_in_atlas is False
        assert outcome.connection_expected is False
        assert outcome.succeeded is True

    def test_atlas_is_still_polled_for_a_crawler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: the probe path is untouched when nothing is declared."""
        harness = _Crawler()
        calls = _stub_run(
            harness,
            _succeeded("extract", "publish"),
            monkeypatch,
            connection_found=True,
            total=7,
            lineage={"Process": 1},
        )

        outcome = harness.run_full_dag()

        assert calls.polled_connection == ["default/bundle/1"]
        assert outcome.connection_in_atlas is True
        assert outcome.connection_expected is True
        assert outcome.total_assets == 7


# ---------------------------------------------------------------------------
# test_full_dag_runs_end_to_end
# ---------------------------------------------------------------------------


class TestAssertionLadder:
    def test_miner_passes_on_the_dag_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Miner()
        harness.source_available = True  # type: ignore[attr-defined]
        _stub_run(harness, _succeeded("extract"), monkeypatch)

        # Would raise on the zero-asset backstop if the inventory assertions ran.
        harness.test_full_dag_runs_end_to_end()

    def test_miner_still_fails_on_a_failed_dag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Miner()
        harness.source_available = True  # type: ignore[attr-defined]
        _stub_run(harness, _failed("extract"), monkeypatch)

        with pytest.raises(AssertionError) as exc:
            harness.test_full_dag_runs_end_to_end()
        # The connection line must not send the reader hunting a connection that
        # was never meant to exist.
        assert "not applicable (expect_connection=False)" in str(exc.value)

    def test_seed_prerequisites_runs_before_the_dag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        class _Seeding(_Miner):
            def seed_prerequisites(self) -> None:
                calls.append("seed")

        harness = _Seeding()
        harness.source_available = True  # type: ignore[attr-defined]
        _stub_run(harness, _succeeded("extract"), monkeypatch)
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

    def test_crawler_still_fails_without_a_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: the Atlas clause still gates a publishing entrypoint."""
        harness = _Crawler()
        harness.source_available = True  # type: ignore[attr-defined]
        _stub_run(
            harness,
            _succeeded("extract", "publish"),
            monkeypatch,
            connection_found=False,
        )

        with pytest.raises(AssertionError) as exc:
            harness.test_full_dag_runs_end_to_end()
        assert "Connection in Atlas? False" in str(exc.value)


# ---------------------------------------------------------------------------
# seed_connection
# ---------------------------------------------------------------------------


class _SeedingMiner(_Miner):
    connection_type = "miner-source"
    atlas_poll_interval_seconds = 0
    atlas_poll_timeout_seconds = 1


def _seeding_harness(
    monkeypatch: pytest.MonkeyPatch, **overrides: Any
) -> tuple[_SeedingMiner, _AtlasCalls]:
    """A harness with the identity ``setup_method`` would have minted."""
    calls = _AtlasCalls(**overrides)
    calls.install(monkeypatch)
    harness = _SeedingMiner()
    harness.connection_qualified_name = "default/miner-source/minted"  # type: ignore[attr-defined]
    harness.connection_display_name = "miner-source-minted"  # type: ignore[attr-defined]
    harness._auto_admin_roles = ("role-guid",)  # type: ignore[attr-defined]
    harness._auto_admin_users = ("svc",)  # type: ignore[attr-defined]
    return harness, calls


class TestSeedConnection:
    def test_keeps_this_runs_own_minted_qualified_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The name is an input to the create, not something adopted back from it.

        ``Connection.creator`` derived ``default/<type>/<epoch>`` and the lifted
        code adopted whatever came back — one second of resolution, so two legs
        of one e2e matrix starting in the same second shared a connection and the
        first to finish purged the other's assets. The minted name does not move.
        """
        harness, calls = _seeding_harness(monkeypatch)

        returned = harness.seed_connection()

        assert returned == "default/miner-source/minted"
        assert harness.connection_qualified_name == "default/miner-source/minted"
        assert calls.created[0]["qualified_name"] == "default/miner-source/minted"
        assert calls.created[0]["display_name"] == "miner-source-minted"

    def test_passes_the_resolved_admin_acl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, calls = _seeding_harness(monkeypatch)
        harness.seed_connection()

        assert calls.created[0]["admin_roles"] == ["role-guid"]
        assert calls.created[0]["admin_users"] == ["svc"]

    def test_the_connector_type_is_the_catalog_segment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``connection_type`` wins over the app's short name where they differ."""
        harness, calls = _seeding_harness(monkeypatch)
        harness.seed_connection()

        assert calls.created[0]["connector_type"] == "miner-source"

    def test_unknown_connector_type_names_the_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leaf comes from the create itself, which is where the type is used."""
        harness, _ = _seeding_harness(monkeypatch)

        async def _refuse(_client: object, **kwargs: Any) -> str:
            raise UnknownConnectorTypeError(
                message="not a real connector type", value_summary="nope"
            )

        monkeypatch.setattr(atlas_api, "create_connection", _refuse)
        with pytest.raises(UnknownConnectorTypeError) as exc:
            harness.seed_connection()
        assert exc.value.field == "connection_type"

    def test_unsearchable_seed_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _ = _seeding_harness(monkeypatch, connection_found=False)

        with pytest.raises(SeededConnectionNotSearchableError):
            harness.seed_connection()

    def test_probe_is_retried_until_policies_go_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, _ = _seeding_harness(monkeypatch)
        harness.atlas_poll_timeout_seconds = 30  # type: ignore[misc]
        attempts: list[int] = []

        def _probe() -> None:
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("403 — connection policies not live yet")

        with fake_clock():
            harness.seed_connection(probe=_probe)

        assert len(attempts) == 3

    def test_probe_that_never_succeeds_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A seed that never became writable must fail, not run anyway."""
        harness, _ = _seeding_harness(monkeypatch)
        harness.atlas_poll_timeout_seconds = 0  # type: ignore[misc]

        def _probe() -> None:
            raise PermissionError("403 forever")

        with pytest.raises(PermissionError):
            harness.seed_connection(probe=_probe)

    def test_the_probe_retry_stops_inside_its_stated_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The off-by-one FND-240 removed along with the hand-rolled deadline.

        The old loop checked ``time.monotonic() >= deadline`` *after* the probe,
        so a 30s budget at a 10s interval probed at 0, 10, 20 **and 30** — it
        slept a whole interval past its own timeout to find out it had expired.
        The shared primitive makes that decision before the sleep: three probes,
        20s of sleeping, and the failure raised at 20s rather than at 30s.
        """
        harness, _ = _seeding_harness(monkeypatch)
        harness.atlas_poll_timeout_seconds = 30  # type: ignore[misc]
        harness.atlas_poll_interval_seconds = 10  # type: ignore[misc]
        attempts: list[int] = []

        def _probe() -> None:
            attempts.append(1)
            raise PermissionError("403 forever")

        with fake_clock() as clock, pytest.raises(PermissionError):
            harness.seed_connection(probe=_probe)

        assert len(attempts) == 3
        assert clock.slept == [10, 10]

    @pytest.mark.parametrize("exc_type", [TypeError, ValueError])
    def test_a_non_transient_probe_error_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
    ) -> None:
        """A deterministic probe bug must not burn the whole timeout retrying.

        A wrong call signature or a bad config value raises identically on
        every attempt, so the retry loop re-raises it on first sight rather
        than waiting out ``atlas_poll_timeout_seconds``.
        """
        harness, _ = _seeding_harness(monkeypatch)
        harness.atlas_poll_timeout_seconds = 1500  # would hang if retried
        attempts: list[int] = []

        def _probe() -> None:
            attempts.append(1)
            raise exc_type("probe bug — not transient")

        with pytest.raises(exc_type):
            harness.seed_connection(probe=_probe)

        assert len(attempts) == 1

    def test_an_async_probe_is_awaited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The seam only exists because the retry loop runs on the harness' loop.

        A suite whose representative write is itself async no longer has to
        bridge back to sync to be retried.
        """
        harness, _ = _seeding_harness(monkeypatch)
        attempts: list[int] = []

        async def _probe() -> None:
            attempts.append(1)

        harness.seed_connection(probe=_probe)  # type: ignore[arg-type]

        assert attempts == [1]


class TestSeededConnectionIsTornDown:
    """The isolation guarantee.

    A seeded connection must be purged like a crawler-created one. If this test
    ever fails, the design has regressed toward a surviving shared connection —
    which is exactly what lets a half-set-up left-over green a later run.
    """

    def test_teardown_purges_the_seeded_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, calls = _seeding_harness(monkeypatch)
        seeded_qn = harness.seed_connection()

        harness.teardown_method(None)

        assert calls.purged == [seeded_qn], (
            f"seeded connection {seeded_qn} was not purged — a surviving "
            "connection breaks run isolation"
        )
