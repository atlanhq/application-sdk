"""Infra-failure diagnosis path: heartbeat timeout (spot / OOM / node loss) →
diagnose against the infra-event service → record the cause, then re-raise.

Covers:
- ``_is_heartbeat_timeout`` distinguishes heartbeat timeout from start-to-close
  and from an ApplicationError cause
- ``_decode_activity_location`` reads identity from heartbeat details, falls
  back to worker identity, and never raises
- ``classify_infra_failure`` returns the recorded-cause contract shape
- ``diagnose_infra_failure`` activity fails safe (NONE) and calls the client
- ``execute_activity_with_infra_diagnosis`` is a passthrough when disabled and
  diagnoses + records + re-raises on a heartbeat timeout when enabled

The temporalio ``TimeoutError`` construction here PINS the contract the
detection relies on (``ActivityError.cause`` → ``TimeoutError.type`` /
``.last_heartbeat_details``). If a temporalio upgrade changes that shape, these
fixtures fail loudly rather than the feature regressing silently.
"""

from __future__ import annotations

from unittest import mock

import pytest
from temporalio.exceptions import ActivityError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.exceptions import TimeoutType

from application_sdk.execution._temporal import infra_diagnosis
from application_sdk.execution._temporal.infra_diagnosis import (
    RecordedCause,
    _decode_activity_location,
    _is_heartbeat_timeout,
    _parse_worker_identity,
    classify_infra_failure,
    diagnose_infra_failure,
    execute_activity_with_infra_diagnosis,
    read_pod_identity,
)
from application_sdk.execution._temporal.infra_event_client import (
    ActivityLocation,
    FakeInfraEventClient,
    InfraCause,
    InfraVerdict,
)


def _make_timeout_activity_error(
    timeout_type: TimeoutType = TimeoutType.HEARTBEAT,
    heartbeat_details: list[object] | None = None,
    identity: str = "pod-1@node-1",
) -> ActivityError:
    """Build an ``ActivityError`` whose cause is a ``TimeoutError`` of the given
    type — mirroring what the workflow sees when an activity times out."""
    cause = TemporalTimeoutError(
        "activity timeout",
        type=timeout_type,
        last_heartbeat_details=heartbeat_details or [],
    )
    err = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity=identity,
        activity_type="dummy",
        activity_id="dummy-1",
        retry_state=None,
    )
    err.__cause__ = cause
    return err


def _make_app_error_activity_error() -> ActivityError:
    """An ``ActivityError`` caused by an ApplicationError, not a timeout."""
    from application_sdk.execution.errors import ApplicationError

    err = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="dummy",
        activity_id="dummy-1",
        retry_state=None,
    )
    err.__cause__ = ApplicationError("boom", type="ValueError")
    return err


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------


class TestIsHeartbeatTimeout:
    def test_true_for_heartbeat_timeout(self) -> None:
        assert _is_heartbeat_timeout(_make_timeout_activity_error()) is True

    def test_false_for_start_to_close_timeout(self) -> None:
        err = _make_timeout_activity_error(timeout_type=TimeoutType.START_TO_CLOSE)
        assert _is_heartbeat_timeout(err) is False

    def test_false_for_application_error_cause(self) -> None:
        assert _is_heartbeat_timeout(_make_app_error_activity_error()) is False


class TestDecodeActivityLocation:
    def test_reads_identity_from_heartbeat_details(self) -> None:
        err = _make_timeout_activity_error(
            heartbeat_details=[
                {"node": "n-hb", "pod": "p-hb", "started_at": "2026-01-01T00:00:00Z"}
            ],
            identity="p-wi@n-wi",
        )
        loc = _decode_activity_location(err)
        # Heartbeat details win over worker identity.
        assert loc.node == "n-hb"
        assert loc.pod == "p-hb"
        assert loc.started_at == "2026-01-01T00:00:00Z"

    def test_falls_back_to_worker_identity(self) -> None:
        err = _make_timeout_activity_error(heartbeat_details=[], identity="p-wi@n-wi")
        loc = _decode_activity_location(err)
        assert loc.node == "n-wi"
        assert loc.pod == "p-wi"

    def test_empty_when_nothing_decodable(self) -> None:
        err = _make_timeout_activity_error(heartbeat_details=[], identity="")
        loc = _decode_activity_location(err)
        assert loc.is_correlatable() is False

    def test_never_raises_on_odd_details(self) -> None:
        err = _make_timeout_activity_error(
            heartbeat_details=["not-a-dict"], identity=""
        )
        loc = _decode_activity_location(err)  # must not raise
        assert loc.is_correlatable() is False


class TestParseWorkerIdentity:
    def test_parses_pod_at_node(self) -> None:
        assert _parse_worker_identity("mypod@mynode") == ("mynode", "mypod")

    def test_none_when_no_at(self) -> None:
        assert _parse_worker_identity("just-a-string") == (None, None)

    def test_none_when_empty(self) -> None:
        assert _parse_worker_identity(None) == (None, None)


# ---------------------------------------------------------------------------
# classification (contract shape only — the mapping itself is a TODO)
# ---------------------------------------------------------------------------


class TestClassifyInfraFailure:
    @pytest.mark.parametrize(
        "cause",
        [
            InfraCause.SPOT_RECLAIM,
            InfraCause.OOM_KILLED,
            InfraCause.NODE_LOST,
            InfraCause.NONE,
        ],
    )
    def test_returns_recorded_cause_for_every_verdict(self, cause: InfraCause) -> None:
        recorded = classify_infra_failure(InfraVerdict(cause=cause))
        assert isinstance(recorded, RecordedCause)
        assert recorded.infra_reason == cause.value
        assert recorded.code
        assert recorded.category
        assert recorded.audience


# ---------------------------------------------------------------------------
# diagnosis activity
# ---------------------------------------------------------------------------


class TestDiagnoseInfraFailureActivity:
    async def test_returns_none_when_not_correlatable(self) -> None:
        verdict = await diagnose_infra_failure(ActivityLocation())
        assert verdict.cause is InfraCause.NONE

    async def test_uses_client_when_correlatable(self) -> None:
        fake = FakeInfraEventClient({"p1": InfraVerdict(cause=InfraCause.OOM_KILLED)})
        with mock.patch.object(
            infra_diagnosis, "get_infra_event_client", return_value=fake
        ):
            verdict = await diagnose_infra_failure(
                ActivityLocation(pod="p1", node="n1")
            )
        assert verdict.cause is InfraCause.OOM_KILLED

    async def test_fails_safe_when_client_raises(self) -> None:
        boom = mock.Mock()
        boom.lookup = mock.AsyncMock(side_effect=RuntimeError("network"))
        with mock.patch.object(
            infra_diagnosis, "get_infra_event_client", return_value=boom
        ):
            verdict = await diagnose_infra_failure(ActivityLocation(pod="p1"))
        assert verdict.cause is InfraCause.NONE


# ---------------------------------------------------------------------------
# workflow-side wrapper
# ---------------------------------------------------------------------------


class TestExecuteActivityWithInfraDiagnosis:
    def _patch_eviction_loop(self, side_effect: object) -> mock._patch:
        return mock.patch.object(
            infra_diagnosis,
            "execute_activity_with_eviction_retry",
            mock.AsyncMock(side_effect=side_effect),
        )

    async def test_returns_result_on_success(self) -> None:
        with mock.patch.object(
            infra_diagnosis,
            "execute_activity_with_eviction_retry",
            mock.AsyncMock(return_value="ok"),
        ):
            result = await execute_activity_with_infra_diagnosis("act")
        assert result == "ok"

    async def test_passthrough_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.ENABLE_INFRA_FAILURE_DIAGNOSIS", False
        )
        err = _make_timeout_activity_error()
        diag = mock.AsyncMock()
        with (
            self._patch_eviction_loop(err),
            mock.patch.object(infra_diagnosis.workflow, "execute_activity", diag),
            pytest.raises(ActivityError),
        ):
            await execute_activity_with_infra_diagnosis("act")
        diag.assert_not_awaited()

    async def test_diagnoses_and_records_on_heartbeat_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.ENABLE_INFRA_FAILURE_DIAGNOSIS", True
        )
        err = _make_timeout_activity_error(
            heartbeat_details=[{"node": "n1", "pod": "p1"}]
        )
        diag = mock.AsyncMock(return_value=InfraVerdict(cause=InfraCause.OOM_KILLED))
        upsert = mock.MagicMock()
        with (
            self._patch_eviction_loop(err),
            mock.patch.object(infra_diagnosis.workflow, "execute_activity", diag),
            mock.patch.object(
                infra_diagnosis.workflow, "upsert_search_attributes", upsert
            ),
            mock.patch.object(infra_diagnosis.workflow, "logger", mock.MagicMock()),
            pytest.raises(ActivityError),
        ):
            await execute_activity_with_infra_diagnosis("act")
        diag.assert_awaited_once()
        upsert.assert_called_once()

    async def test_does_not_diagnose_non_timeout_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.ENABLE_INFRA_FAILURE_DIAGNOSIS", True
        )
        err = _make_app_error_activity_error()
        diag = mock.AsyncMock()
        with (
            self._patch_eviction_loop(err),
            mock.patch.object(infra_diagnosis.workflow, "execute_activity", diag),
            pytest.raises(ActivityError),
        ):
            await execute_activity_with_infra_diagnosis("act")
        diag.assert_not_awaited()

    async def test_record_failure_never_masks_original(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If diagnosis itself blows up, the original ActivityError still wins."""
        monkeypatch.setattr(
            "application_sdk.constants.ENABLE_INFRA_FAILURE_DIAGNOSIS", True
        )
        err = _make_timeout_activity_error()
        with (
            self._patch_eviction_loop(err),
            mock.patch.object(
                infra_diagnosis.workflow,
                "execute_activity",
                mock.AsyncMock(side_effect=RuntimeError("diagnosis broke")),
            ),
            mock.patch.object(infra_diagnosis.workflow, "logger", mock.MagicMock()),
            pytest.raises(ActivityError),
        ):
            await execute_activity_with_infra_diagnosis("act")


# ---------------------------------------------------------------------------
# identity seeding (activity-side)
# ---------------------------------------------------------------------------


class TestReadPodIdentity:
    def test_reads_node_and_pod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NODE_NAME", "n1")
        monkeypatch.setenv("POD_NAME", "p1")
        assert read_pod_identity() == {"node": "n1", "pod": "p1"}

    def test_hostname_fallback_for_pod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POD_NAME", raising=False)
        monkeypatch.setenv("HOSTNAME", "p-host")
        monkeypatch.delenv("NODE_NAME", raising=False)
        assert read_pod_identity() == {"pod": "p-host"}
