"""The activity-slot limit gauge KEDA's temporal scaler reads from each worker.

These tests record through a real ``Runtime`` into an in-memory
``MetricBuffer``, so they need no socket (the unit tier runs with
``--disable-socket``). The rendered Prometheus line is pinned by scraping a real
endpoint in ``tests/integration/test_activity_slots_limit_scrape.py``. See
``docs/standards/cross-repo-contracts.md`` before renaming the series or a label.
"""

from __future__ import annotations

from unittest import mock

import pytest
from temporalio.runtime import MetricBuffer, Runtime, TelemetryConfig

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.worker import (
    ACTIVITY_SLOTS_LIMIT_METRIC,
    _publish_activity_slots_limit,
    create_worker,
)


class _GaugeInput(Input, allow_unbounded_fields=True):
    pass


class _GaugeOutput(Output, allow_unbounded_fields=True):
    pass


def _buffered_runtime(metric_prefix: str | None = None) -> tuple[Runtime, MetricBuffer]:
    buffer = MetricBuffer(10_000)
    if metric_prefix is None:
        telemetry = TelemetryConfig(metrics=buffer)
    else:
        telemetry = TelemetryConfig(metrics=buffer, metric_prefix=metric_prefix)
    return Runtime(telemetry=telemetry), buffer


def _client_on(runtime: Runtime | None) -> mock.MagicMock:
    client = mock.MagicMock()
    client.namespace = "default"
    client.service_client.config.target_host = "localhost:7233"
    client.service_client.config.runtime = runtime
    return client


def _limit_samples(buffer: MetricBuffer) -> list[tuple[dict[str, str], int]]:
    """Every update recorded under the contract name, as (labels, value)."""
    return [
        (
            {str(k): str(v) for k, v in update.attributes.items()},
            int(update.value),
        )
        for update in buffer.retrieve_updates()
        if update.metric.name == ACTIVITY_SLOTS_LIMIT_METRIC
    ]


def test_metric_name_is_the_contract_name() -> None:
    assert ACTIVITY_SLOTS_LIMIT_METRIC == "temporal_worker_activity_slots_limit"


def test_gauge_carries_task_queue_and_activity_worker_labels() -> None:
    runtime, buffer = _buffered_runtime()

    _publish_activity_slots_limit(_client_on(runtime), "atlan-example-production", 16)

    [(labels, value)] = _limit_samples(buffer)
    assert value == 16
    assert labels["task_queue"] == "atlan-example-production"
    assert labels["worker_type"] == "ActivityWorker"


def test_metric_prefix_does_not_rename_the_gauge() -> None:
    """``TelemetryConfig.metric_prefix`` renames core metrics only."""
    runtime, buffer = _buffered_runtime(metric_prefix="custom_")

    _publish_activity_slots_limit(_client_on(runtime), "q", 32)

    [(labels, value)] = _limit_samples(buffer)
    assert value == 32
    assert labels["task_queue"] == "q"


def test_a_client_without_a_runtime_publishes_on_the_default_runtime() -> None:
    """temporalio connects such a client on ``Runtime.default()``."""
    default = mock.MagicMock()
    with mock.patch(
        "application_sdk.execution._temporal.worker.Runtime.default",
        return_value=default,
    ):
        _publish_activity_slots_limit(_client_on(None), "q", 16)

    default.metric_meter.with_additional_attributes.return_value.create_gauge.return_value.set.assert_called_once_with(
        16
    )


def test_a_failing_meter_does_not_stop_the_worker() -> None:
    client = mock.MagicMock()
    client.service_client.config.runtime.metric_meter.with_additional_attributes.side_effect = RuntimeError(
        "meter unavailable"
    )
    with mock.patch("application_sdk.execution._temporal.worker.logger") as log:
        _publish_activity_slots_limit(client, "q", 16)
    log.warning.assert_called_once()


class TestCreateWorkerPublishesTheLimit:
    """create_worker publishes the slot count it runs with, whatever its source."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

        class _GaugeApp(App):
            async def run(self, input: _GaugeInput) -> _GaugeOutput:
                return _GaugeOutput()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def _published_and_configured(self, **kwargs: int) -> tuple[int, int]:
        runtime, buffer = _buffered_runtime()
        captured: dict[str, object] = {}

        def capture_worker(*args: object, **worker_kwargs: object) -> mock.MagicMock:
            captured.update(worker_kwargs)
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(_client_on(runtime), task_queue="q", **kwargs)

        [(labels, value)] = _limit_samples(buffer)
        assert labels["task_queue"] == "q"
        assert labels["worker_type"] == "ActivityWorker"
        configured = captured["max_concurrent_activities"]
        assert isinstance(configured, int)
        return value, configured

    def test_value_passed_to_create_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "37")

        assert self._published_and_configured(max_concurrent_activities=5) == (5, 5)

    def test_value_from_the_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "37")

        assert self._published_and_configured() == (37, 37)

    def test_value_derived_from_the_pod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", raising=False)
        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", "512Mi")
        monkeypatch.setattr(
            "application_sdk.execution._temporal.worker.available_cpus", lambda: 1
        )

        assert self._published_and_configured() == (16, 16)
