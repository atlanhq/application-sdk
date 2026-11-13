"""The activity-slot limit gauge, as a scraper of the worker's metrics endpoint sees it.

KEDA's temporal scaler reads this series from each worker pod and counts busy
slots as the limit minus ``temporal_worker_task_slots_available``. These tests
pin the rendered series name and labels by scraping a real Prometheus
endpoint, because the rendered line is what the scaler matches on (see
``docs/standards/cross-repo-contracts.md``). They bind a local port, so they
live in the integration tier; the unit tier pins the same contract through an
in-memory metric buffer in ``tests/unit/execution/test_activity_slots_limit_gauge.py``.
"""

from __future__ import annotations

import re
import socket
import time
import urllib.request
from unittest import mock

import pytest
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

from application_sdk.execution._temporal.worker import _publish_activity_slots_limit

pytestmark = pytest.mark.integration

_SERIES = re.compile(
    r"^temporal_worker_activity_slots_limit\{(?P<labels>[^}]*)\} (?P<value>\S+)$"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prometheus_runtime(metric_prefix: str | None = None) -> tuple[Runtime, int]:
    port = _free_port()
    metrics = PrometheusConfig(bind_address=f"127.0.0.1:{port}")
    if metric_prefix is None:
        telemetry = TelemetryConfig(metrics=metrics)
    else:
        telemetry = TelemetryConfig(metrics=metrics, metric_prefix=metric_prefix)
    return Runtime(telemetry=telemetry), port


def _client_on(runtime: Runtime) -> mock.MagicMock:
    client = mock.MagicMock()
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
