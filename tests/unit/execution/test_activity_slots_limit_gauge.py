"""The activity-slot limit gauge, as a scraper of the worker's metrics endpoint sees it.

KEDA's temporal scaler reads this series from each worker pod and counts busy
slots as the limit minus ``temporal_worker_task_slots_available``. These tests
pin the series name and labels by scraping a real Prometheus endpoint, because
the rendered name is what the scaler matches on (see
``docs/standards/cross-repo-contracts.md``).
"""

from __future__ import annotations

import re
import socket
import time
import urllib.request
from unittest import mock

import pytest
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.worker import (
    ACTIVITY_SLOTS_LIMIT_METRIC,
    _publish_activity_slots_limit,
    create_worker,
)

_SERIES = re.compile(
    r"^temporal_worker_activity_slots_limit\{(?P<labels>[^}]*)\} (?P<value>\S+)$"
)


class _GaugeInput(Input, allow_unbounded_fields=True):
    pass


class _GaugeOutput(Output, allow_unbounded_fields=True):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prometheus_runtime(metric_prefix: str | None = None) -> tuple[Runtime, int]:
    port = _free_port()
    telemetry = {"metrics": PrometheusConfig(bind_address=f"127.0.0.1:{port}")}
    if metric_prefix is not None:
        telemetry["metric_prefix"] = metric_prefix
    return Runtime(telemetry=TelemetryConfig(**telemetry)), port


def _client_on(runtime: Runtime) -> mock.MagicMock:
    client = mock.MagicMock()
    client.namespace = "default"
    client.service_client.config.target_host = "localhost:7233"
    client.service_client.config.runtime = runtime
    return client


def _limit_series(port: int) -> list[tuple[dict[str, str], float]]:
    """Every ``temporal_worker_activity_slots_limit`` sample on the endpoint."""
    deadline = time.monotonic() + 5
    while True:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics") as resp:
            body = resp.read().decode()
        samples = []
        for line in body.splitlines():
            match = _SERIES.match(line)
            if match:
                labels = dict(re.findall(r'(\w+)="([^"]*)"', match["labels"]))
                samples.append((labels, float(match["value"])))
        if samples or time.monotonic() > deadline:
            return samples
        time.sleep(0.05)


def test_metric_name_is_the_contract_name() -> None:
    assert ACTIVITY_SLOTS_LIMIT_METRIC == "temporal_worker_activity_slots_limit"


def test_gauge_renders_with_task_queue_and_activity_worker_labels() -> None:
    runtime, port = _prometheus_runtime()

    _publish_activity_slots_limit(_client_on(runtime), "atlan-example-production", 16)

    [(labels, value)] = _limit_series(port)
    assert value == 16
    assert labels["task_queue"] == "atlan-example-production"
    assert labels["worker_type"] == "ActivityWorker"


def test_metric_prefix_does_not_rename_the_gauge() -> None:
    """``TelemetryConfig.metric_prefix`` renames core metrics only."""
    runtime, port = _prometheus_runtime(metric_prefix="custom_")

    _publish_activity_slots_limit(_client_on(runtime), "q", 32)

    [(labels, value)] = _limit_series(port)
    assert value == 32
    assert labels["task_queue"] == "q"


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

    def _published_and_configured(self, **kwargs: int) -> tuple[float, int]:
        runtime, port = _prometheus_runtime()
        captured: dict = {}

        def capture_worker(*args, **worker_kwargs):
            captured.update(worker_kwargs)
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(_client_on(runtime), task_queue="q", **kwargs)

        [(labels, value)] = _limit_series(port)
        assert labels["task_queue"] == "q"
        assert labels["worker_type"] == "ActivityWorker"
        return value, captured["max_concurrent_activities"]

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
