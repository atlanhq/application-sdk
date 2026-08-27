"""What child H changed about ``BaseE2ETest``, as claims rather than as prose.

Three things moved that a connector can observe, and each is pinned here rather
than in the file whose subject it happens to touch:

* an Atlas search that **could not be read** is no longer graded as a low count
  (finding C4 on FND-224). It raises
  :class:`~application_sdk.testing.e2e._errors.AtlasReadIndeterminateError`,
  which is not an ``AssertionError`` — so pytest reports the leg as an *error*
  and nobody reads it as a connector regression;
* the ``NoWorkerOnTaskQueueError`` the stall grace *infers* can now carry what
  Temporal *reports*, where a suite has a route to a frontend
  (:mod:`application_sdk.testing.harness.temporal`, opt-in);
* the timing ``ClassVar``\\s a connector declares still govern every wait —
  they build a :class:`~application_sdk.testing.harness.budgets.Budget` instead
  of being read at the call site, which is invisible to a subclass that
  overrides one and would be very visible if it silently stopped applying.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from application_sdk.testing.e2e._errors import (
    AtlasReadIndeterminateError,
    MissingHarnessEnvError,
    NoWorkerOnTaskQueueError,
)
from application_sdk.testing.e2e.base import BaseE2ETest, FullDAGOutcome
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)
from application_sdk.testing.harness import atlas as atlas_api
from application_sdk.testing.harness.expectations import Unreadable
from application_sdk.testing.harness.outcome import (
    Expired,
    Indeterminate,
    NeverStarted,
    Settled,
)
from application_sdk.testing.harness.temporal import PollerInfo, TaskQueueType

_QN = "default/openapi/1700000000123456"


class _Suite(BaseE2ETest):
    connector_short_name = "openapi"
    argo_package_name = "@atlan/openapi"
    argo_template_name = "atlan-openapi"
    expected_min_asset_counts = {"APISpec": 1}
    expect_lineage = False
    required_dag_nodes = ("extract",)


def _succeeded(*names: str) -> DAGRunResult:
    return DAGRunResult(
        run_id="r",
        workflow_slug="s",
        status=DAGRunStatus.SUCCEEDED,
        nodes=[
            DAGNodeResult(
                name=name,
                status=DAGNodeStatus.SUCCEEDED,
                started_at_ms=None,
                completed_at_ms=None,
                error_message=None,
            )
            for name in names
        ],
    )


def _suite() -> _Suite:
    suite = _Suite()
    suite.connection_qualified_name = _QN
    return suite


# ---------------------------------------------------------------------------
# An unreadable Atlas read is not a connector regression
# ---------------------------------------------------------------------------


def _unreadable_outcome() -> FullDAGOutcome:
    """An outcome whose count read failed, as the readers now report it."""
    return FullDAGOutcome(
        ae_result=_succeeded("extract"),
        connection_qualified_name=_QN,
        connection_in_atlas=True,
        asset_counts={},
        asset_count_reads={"APISpec": Unreadable(cause=RuntimeError("atlas is down"))},
        total_asset_read=Unreadable(cause=RuntimeError("atlas is down")),
    )


class TestUnreadableAtlasIsNotAFailure:
    def test_an_unreadable_count_raises_a_dependency_leaf(self) -> None:
        with pytest.raises(AtlasReadIndeterminateError) as exc:
            _suite()._assert_full_dag_outcome(_unreadable_outcome())
        assert exc.value.code == "DEPENDENCY_UNAVAILABLE_ATLAS_READ_INDETERMINATE"
        assert exc.value.checks is not None and "APISpec" in exc.value.checks

    def test_the_leaf_is_not_an_assertion_error(self) -> None:
        """The whole point. pytest reports an error, not a failed expectation.

        Before child H the same run reported "APISpec: got 0, expected >= 1" —
        a confident claim about the connector, made by a run that never read it.
        """
        with pytest.raises(AtlasReadIndeterminateError) as exc:
            _suite()._assert_full_dag_outcome(_unreadable_outcome())
        assert not isinstance(exc.value, AssertionError)

    def test_a_settled_zero_still_fails_the_floor(self) -> None:
        """The control: "read it, and it was zero" is still the connector's."""
        outcome = FullDAGOutcome(
            ae_result=_succeeded("extract"),
            connection_qualified_name=_QN,
            connection_in_atlas=True,
            asset_counts={"APISpec": 0},
            asset_count_reads={"APISpec": 0},
            total_asset_read=0,
        )
        with pytest.raises(AssertionError, match="expected >= 1"):
            _suite()._assert_full_dag_outcome(outcome)

    def test_an_unreadable_sample_no_longer_passes_silently(self) -> None:
        """The location check used to fail open.

        A failed sample read arrived as ``[]``, an empty sample is skipped, so an
        auth fault was graded as a pass. It has its own spelling now.
        """

        class _Depths(_Suite):
            expected_asset_qn_depth = {"APISpec": 1}

        suite = _Depths()
        suite.connection_qualified_name = _QN
        outcome = FullDAGOutcome(
            ae_result=_succeeded("extract"),
            connection_qualified_name=_QN,
            connection_in_atlas=True,
            asset_counts={"APISpec": 3},
            asset_count_reads={"APISpec": 3},
            total_asset_read=3,
            asset_qn_reads={"APISpec": Unreadable(cause=RuntimeError("search down"))},
        )
        with pytest.raises(AtlasReadIndeterminateError):
            suite._assert_full_dag_outcome(outcome)

    def test_an_empty_sample_is_still_skipped(self) -> None:
        """The control for the case above: nothing landed is the COUNT's job."""

        class _Depths(_Suite):
            expected_asset_qn_depth = {"APISpec": 1}

        suite = _Depths()
        suite.connection_qualified_name = _QN
        outcome = FullDAGOutcome(
            ae_result=_succeeded("extract"),
            connection_qualified_name=_QN,
            connection_in_atlas=True,
            asset_counts={"APISpec": 3},
            asset_count_reads={"APISpec": 3},
            total_asset_read=3,
            asset_qn_reads={"APISpec": []},
        )
        suite._assert_full_dag_outcome(outcome)  # must not raise


class TestTheConnectionPollKeepsItsVerdict:
    """`connection_in_atlas` is one boolean over three different findings.

    "It never appeared" and "Atlas could not be read" both flattened to `False`,
    and the ladder called both *"Connection in Atlas? False"* — sending the
    reader to the publish path for a fault that was never in it.
    """

    def _outcome(self, connection_read: Any) -> FullDAGOutcome:
        return FullDAGOutcome(
            ae_result=_succeeded("extract"),
            connection_qualified_name=_QN,
            connection_in_atlas=False,
            connection_read=connection_read,
        )

    def test_an_unreadable_poll_raises_a_dependency_leaf(self) -> None:
        outcome = self._outcome(
            Indeterminate(
                label="Atlas Connection",
                attempts=10,
                elapsed=timedelta(seconds=270),
                cause=RuntimeError("atlas is down"),
            )
        )
        with pytest.raises(AtlasReadIndeterminateError) as exc:
            _suite()._assert_full_dag_outcome(outcome)
        assert not isinstance(exc.value, AssertionError)
        assert exc.value.checks is not None and "Connection" in exc.value.checks

    @pytest.mark.parametrize(
        "verdict",
        [
            pytest.param(
                NeverStarted(
                    label="Atlas Connection",
                    attempts=10,
                    elapsed=timedelta(seconds=270),
                    grace=timedelta(seconds=270),
                ),
                id="never-started",
            ),
            pytest.param(
                Expired(
                    label="Atlas Connection",
                    attempts=50,
                    elapsed=timedelta(seconds=1500),
                    budget=timedelta(seconds=1500),
                ),
                id="expired",
            ),
        ],
    )
    def test_a_connection_that_never_appeared_is_still_the_connectors(
        self, verdict: Any
    ) -> None:
        """The narrowing that matters: only *unreadable* is ungraded.

        A poll that read Atlas fine and never saw the Connection is a real
        finding about the publish path. Ungrading it too would turn a genuine
        regression into "could not tell".
        """
        with pytest.raises(AssertionError, match="Connection in Atlas\\? False"):
            _suite()._assert_full_dag_outcome(self._outcome(verdict))

    def test_a_run_that_never_probed_is_unaffected(self) -> None:
        """`connection_read` is None when the DAG failed before the Atlas phase.

        The DAG failure is the verdict there, and it must not be displaced by a
        complaint about a probe that never ran.
        """
        outcome = FullDAGOutcome(
            ae_result=_succeeded("extract"),
            connection_qualified_name=_QN,
            connection_in_atlas=False,
        )
        with pytest.raises(AssertionError):
            _suite()._assert_full_dag_outcome(outcome)


class TestTheLineageCountKeepsItsVerdict:
    """`lineage_present=False` meant both "no Process" and "could not read".

    With `expect_lineage` on, the second was reported as *"no lineage rows
    reached Atlas"* — the connector's fault, asserted by a run that never
    looked. The same C4 shape as the counts, on the one probe that was left out.
    """

    def _outcome(self, lineage_read: Any) -> FullDAGOutcome:
        return FullDAGOutcome(
            ae_result=_succeeded("extract"),
            connection_qualified_name=_QN,
            connection_in_atlas=True,
            asset_counts={"APISpec": 3},
            asset_count_reads={"APISpec": 3},
            total_asset_read=3,
            lineage_present=lineage_read is True,
            lineage_read=lineage_read,
        )

    def _lineage_suite(self) -> _Suite:
        class _WithLineage(_Suite):
            expect_lineage = True

        suite = _WithLineage()
        suite.connection_qualified_name = _QN
        return suite

    def test_an_unreadable_lineage_count_raises_a_dependency_leaf(self) -> None:
        outcome = self._outcome(Unreadable(cause=RuntimeError("atlas is down")))
        with pytest.raises(AtlasReadIndeterminateError) as exc:
            self._lineage_suite()._assert_full_dag_outcome(outcome)
        assert not isinstance(exc.value, AssertionError)
        assert exc.value.checks is not None and "lineage" in exc.value.checks

    def test_a_settled_zero_still_fails_the_assertion(self) -> None:
        """The control: read it, and there was no lineage. Still the connector's."""
        with pytest.raises(AssertionError, match="No lineage"):
            self._lineage_suite()._assert_full_dag_outcome(self._outcome(False))

    def test_a_settled_true_passes(self) -> None:
        self._lineage_suite()._assert_full_dag_outcome(self._outcome(True))


class TestTheWorkerUpTierHasNoClient:
    """`setup_method` wires no AE client on the no-source tier.

    Reaching for `self.client` there used to raise `AttributeError` on a private
    field, because the property built `AEWorkflowClient(..., ae=self._ae)` and
    `_ae` was never assigned. Reading the *environment* to decide would not have
    caught it: the tier is reachable with a tenant configured
    (`E2E_SOURCE_AVAILABLE=false` on a leg that has credentials), so the env is
    fine and the attribute is still missing.
    """

    def test_the_leaf_names_the_tier_rather_than_a_private_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
        monkeypatch.setenv("ATLAN_API_KEY", "test-token")
        monkeypatch.setenv("E2E_SOURCE_AVAILABLE", "false")

        suite = _Suite()
        suite.setup_method()

        assert suite.source_available is False
        with pytest.raises(MissingHarnessEnvError) as exc:
            _ = suite.client
        assert "source_available=False" in str(exc.value)


class TestTheAdminIdentityIsReadOnce:
    """Two callers, two policies, one reading.

    The base degrades on an absent `$admin`; `SQLAppE2ETest` raises. Both used
    to call `atlas.admin_identity` themselves, so a transient blip *between* the
    two reads failed a run whose ACL had already resolved.
    """

    def test_the_second_caller_gets_the_first_reading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads = 0

        async def _count(_client: object, **_kwargs: Any) -> Any:
            nonlocal reads
            reads += 1
            return Settled(
                label="admin",
                attempts=1,
                elapsed=timedelta(0),
                value=SimpleNamespace(roles=("role-guid",), users=("svc",)),
            )

        monkeypatch.setattr(atlas_api, "admin_identity", _count)
        suite = _suite()

        first = _run_sync(suite._admin_identity(object()))
        second = _run_sync(suite._admin_identity(object()))

        assert reads == 1, "the reading must be taken once per run"
        assert second is first


class TestUnreadableCountsReachTheOutcome:
    """The projection: one reading, two shapes on the outcome.

    ``asset_counts`` stays ``dict[str, int]`` because connector suites index it
    and compare the values; ``asset_count_reads`` is what the ladder grades.
    """

    def test_an_unreadable_batch_marks_every_probed_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        suite = _suite()
        monkeypatch.setattr(atlas_api, "count_assets", _indeterminate_counts)
        monkeypatch.setattr(atlas_api, "count_total_assets", _indeterminate_total)
        monkeypatch.setattr(
            atlas_api,
            "poll_for_connection",
            _settled_async(True),
        )
        monkeypatch.setattr(BaseE2ETest, "_atlas_client", lambda _s: _null_client())
        suite._ae = SimpleNamespace()  # type: ignore[attr-defined]

        outcome = _run_sync(suite._read_atlas(_succeeded("extract")))

        assert isinstance(outcome.asset_count_reads["APISpec"], Unreadable)
        # The settled projection simply has no entry — never a zero, which is
        # what an expectation would have tripped on.
        assert "APISpec" not in outcome.asset_counts


def _run_sync(coro: Any) -> Any:
    """Drive one of the suite's coroutines from a synchronous test.

    Under ``fake_clock`` so the bounded waits inside cost nothing. The Atlas
    count poll runs its whole ``atlas_asset_poll_timeout_seconds`` budget
    whenever the expectations are never met — which is the case every one of
    these tests sets up — and that is ten real seconds of ``asyncio.sleep`` per
    call on the default 15s/5s timings (FND-962). Applied here rather than per
    test so a new one cannot forget it.

    Only ``_poll``'s clock is faked, never ``time.monotonic`` — the bridge's
    event loop reads that for its own timers.
    """
    from application_sdk.testing.harness._poll import fake_clock
    from application_sdk.testing.harness.bridge import run_sync

    with fake_clock():
        return run_sync(coro)


@asynccontextmanager
async def _null_client() -> AsyncIterator[object]:
    yield object()


def _settled_async(value: Any) -> Any:
    async def _call(*_args: Any, **_kwargs: Any) -> Any:
        return Settled(label="fake", attempts=1, elapsed=timedelta(0), value=value)

    return _call


async def _indeterminate_counts(
    _client: object, _qn: str, type_names: Sequence[str]
) -> Any:
    return Indeterminate(
        label="counts",
        attempts=1,
        elapsed=timedelta(0),
        cause=RuntimeError("atlas is down"),
    )


async def _indeterminate_total(_client: object, _qn: str) -> Any:
    return Indeterminate(
        label="total",
        attempts=1,
        elapsed=timedelta(0),
        cause=RuntimeError("atlas is down"),
    )


# ---------------------------------------------------------------------------
# The Temporal poller read, where a suite has a route to a frontend
# ---------------------------------------------------------------------------


class _Reader:
    """A ``TemporalReader`` that answers a fixed poller list, or raises."""

    def __init__(
        self, pollers: list[PollerInfo], *, error: BaseException | None = None
    ) -> None:
        self._pollers = pollers
        self._error = error
        self.queries: list[tuple[str, str]] = []

    async def __aenter__(self) -> _Reader:
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def task_queue_pollers(
        self, queue: str, *, namespace: str
    ) -> list[PollerInfo]:
        self.queries.append((queue, namespace))
        if self._error is not None:
            raise self._error
        return self._pollers


def _install_reader(monkeypatch: pytest.MonkeyPatch, reader: _Reader) -> None:
    """Stand in for the reader the base builds when an address is configured."""
    import application_sdk.testing.harness.temporal as temporal_api

    monkeypatch.setattr(temporal_api, "TemporalServiceReader", lambda **_kwargs: reader)


def _poller(identity: str, build_id: str | None = None) -> PollerInfo:
    from datetime import UTC, datetime

    return PollerInfo(
        identity=identity,
        last_access=datetime.now(UTC),
        task_queue_type=TaskQueueType.WORKFLOW,
        build_id=build_id,
    )


class TestObservedPollers:
    def test_no_address_leaves_the_diagnosis_inferential(self) -> None:
        """The default, and what a connector CI leg always gets.

        The runner has no route into the tenant vcluster, which is the same
        constraint that makes the AE submit the only tenant-facing probe of the
        installed app pod.
        """
        assert _run_sync(_suite()._observed_pollers()) is None

    def test_an_empty_poller_list_is_reported_as_the_observation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty is a real answer — the observed form of "no worker on the queue".

        It is available on the first probe, where the stall grace needs three
        minutes of silence to guess at it.
        """
        suite = _suite()
        suite.temporal_address = "127.0.0.1:7233"  # type: ignore[misc]
        reader = _Reader([])
        _install_reader(monkeypatch, reader)

        observed = _run_sync(suite._observed_pollers())

        assert observed is not None
        assert "NO pollers" in observed
        assert reader.queries == [(suite._extract_task_queue(), "default")]

    def test_a_populated_queue_points_the_reader_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Something IS polling, so a queue-name mismatch is not the cause."""
        suite = _suite()
        suite.temporal_address = "127.0.0.1:7233"  # type: ignore[misc]
        _install_reader(monkeypatch, _Reader([_poller("1@worker-a", "build-7")]))

        observed = _run_sync(suite._observed_pollers())

        assert observed is not None
        assert "1@worker-a" in observed
        assert "build-7" in observed

    def test_an_unreadable_frontend_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Temporal that cannot be read must not turn a real finding into a
        harness error — the inference still stands on its own."""
        suite = _suite()
        suite.temporal_address = "127.0.0.1:7233"  # type: ignore[misc]
        _install_reader(
            monkeypatch, _Reader([], error=RuntimeError("frontend unreachable"))
        )

        assert _run_sync(suite._observed_pollers()) is None

    def test_the_env_var_wins_over_the_class_attribute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("E2E_TEMPORAL_ADDRESS", "10.0.0.1:7233")
        monkeypatch.setenv("E2E_TEMPORAL_NAMESPACE", "tenant-ns")
        suite = _suite()
        suite.temporal_address = "127.0.0.1:7233"  # type: ignore[misc]
        assert suite._resolved_temporal_address() == "10.0.0.1:7233"
        assert suite._resolved_temporal_namespace() == "tenant-ns"

    def test_a_blank_env_var_falls_back_to_the_class_attribute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset GitHub Actions env var arrives as an empty string."""
        monkeypatch.setenv("E2E_TEMPORAL_ADDRESS", "  ")
        suite = _suite()
        suite.temporal_address = "127.0.0.1:7233"  # type: ignore[misc]
        assert suite._resolved_temporal_address() == "127.0.0.1:7233"


class TestTheStallErrorCarriesTheObservation:
    def test_the_observation_lands_on_the_same_leaf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identity a caller matches on is preserved, not wrapped.

        And the note is what a red CI leg prints — the field alone would be
        invisible, because a leaf renders only its message.
        """
        suite = _suite()
        suite.temporal_address = "127.0.0.1:7233"  # type: ignore[misc]
        _install_reader(monkeypatch, _Reader([]))
        original = NoWorkerOnTaskQueueError(message="nothing started in 180s")

        class _AE:
            async def poll_native_status(self, run_id: str, **_kwargs: Any) -> Any:
                raise original

        suite._ae = _AE()  # type: ignore[attr-defined]

        with pytest.raises(NoWorkerOnTaskQueueError) as exc:
            _run_sync(suite._poll_dag("run-1"))

        assert exc.value is original
        assert exc.value.observed_pollers is not None
        assert "NO pollers" in exc.value.observed_pollers
        assert any("Temporal was asked" in note for note in exc.value.__notes__)

    def test_without_a_route_the_error_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        suite = _suite()
        original = NoWorkerOnTaskQueueError(message="nothing started in 180s")

        class _AE:
            async def poll_native_status(self, run_id: str, **_kwargs: Any) -> Any:
                raise original

        suite._ae = _AE()  # type: ignore[attr-defined]

        with pytest.raises(NoWorkerOnTaskQueueError) as exc:
            _run_sync(suite._poll_dag("run-1"))

        assert exc.value.observed_pollers is None
        assert not getattr(exc.value, "__notes__", [])


# ---------------------------------------------------------------------------
# The timing ClassVars still govern every wait
# ---------------------------------------------------------------------------


class TestBudgetsComeFromTheClassAttributes:
    """A suite that overrides a timing ClassVar must still be obeyed.

    The values are no longer read at the call site — they build a ``Budget`` —
    and that is invisible when it works and total when it does not.
    """

    def test_the_connection_poll_budget(self) -> None:
        class _Slow(_Suite):
            atlas_poll_timeout_seconds = 900
            atlas_poll_interval_seconds = 15

        budget = _Slow()._atlas_connection_budget()
        assert budget.timeout == timedelta(seconds=900)
        assert budget.poll_interval == timedelta(seconds=15)
        # The ten-consecutive-empty-searches cap, as the duration it always was.
        assert budget.start_grace == timedelta(seconds=135)
        assert budget.max_transient_failures == 10

    def test_the_asset_count_budget(self) -> None:
        class _Slow(_Suite):
            atlas_asset_poll_timeout_seconds = 60
            atlas_asset_poll_interval_seconds = 10

        budget = _Slow()._atlas_counts_budget()
        assert budget.timeout == timedelta(seconds=60)
        assert budget.poll_interval == timedelta(seconds=10)

    def test_the_deployed_manifest_budget(self) -> None:
        class _Slow(_Suite):
            deployed_manifest_timeout_seconds = 120
            deployed_manifest_poll_interval_seconds = 10

        budget = _Slow()._deployed_manifest_budget()
        assert budget.timeout == timedelta(seconds=120)
        assert budget.poll_interval == timedelta(seconds=10)

    def test_the_worker_health_budget(self) -> None:
        class _Slow(_Suite):
            worker_health_timeout_seconds = 240
            worker_health_poll_interval_seconds = 6

        budget = _Slow()._worker_health_budget()
        assert budget.timeout == timedelta(seconds=240)
        assert budget.poll_interval == timedelta(seconds=6)

    def test_the_submit_retry_and_its_mapping_agree(self) -> None:
        """One derivation, two spellings — the mapping stays because suites
        assert on it."""
        suite = _Suite()
        retry = suite._submit_retry()
        mapping = suite._submit_retry_kwargs()
        assert retry is not None
        assert retry.retries == mapping["retries"]
        assert retry.sleep_seconds == mapping["retry_sleep_seconds"]
