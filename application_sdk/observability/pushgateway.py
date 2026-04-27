"""Prometheus Pushgateway client for short-lived workers.

Workers don't run a FastAPI server, so Prometheus has no scrape target.
Instead they push the global ``prometheus_client.REGISTRY`` to a Pushgateway
periodically and on shutdown.

The OTel ``PrometheusMetricReader`` already writes all OTel instruments
(MetricsInterceptor outputs + custom user metrics from
``application_sdk.observability.metrics``) into ``prometheus_client.REGISTRY``,
so a single push covers everything.

Temporal Rust-core metrics live on a separate localhost endpoint
(``127.0.0.1:9464``). ``TemporalCoreCollector`` registers a
``prometheus_client`` Collector that fetches and re-parses that endpoint on
each scrape, so Temporal core metrics flow through the same registry and ride
along with every push.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Iterable

import httpx
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    delete_from_gateway,
    push_to_gateway,
)
from prometheus_client.parser import text_string_to_metric_families

from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from prometheus_client.metrics_core import Metric

logger = get_logger(__name__)


class TemporalCoreCollector:
    """Bridge Temporal's Rust-core Prometheus endpoint into the OTel registry.

    Registered once with ``prometheus_client.REGISTRY``. On each scrape (i.e.
    each Pushgateway push or each ``/metrics`` GET on the server) it fetches
    the Temporal exposition text from a loopback URL, parses it, and yields
    the metric families. Failures are silent — we never want a flaky Temporal
    sidecar to break the entire metrics pipeline.
    """

    def __init__(self, url: str, timeout_s: float = 1.0) -> None:
        self._url = url
        self._timeout_s = timeout_s

    def collect(self) -> Iterable["Metric"]:
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                resp = client.get(self._url)
            if resp.status_code != 200:
                return
            yield from text_string_to_metric_families(resp.text)
        except Exception:
            # Silent — a transient Temporal core hiccup must not poison the push.
            return


def _default_grouping_key(task_queue: str) -> dict[str, str]:
    # Do NOT include task_queue here: Temporal metrics already carry task_queue
    # as a metric label, and the Pushgateway rejects pushes where a label appears
    # in both the metric body and the grouping-key URL path (400 Bad Request).
    return {"instance": socket.gethostname()}


class PushGatewayClient:
    """Periodic + shutdown Prometheus Pushgateway pusher.

    Usage::

        client = PushGatewayClient(
            url="http://pushgateway:9091",
            job="my-app-worker",
            grouping_key={"instance": "host-1", "task_queue": "default"},
            interval_s=30.0,
        )
        await client.start()
        ...
        await client.stop()
    """

    def __init__(
        self,
        *,
        url: str,
        job: str,
        grouping_key: dict[str, str] | None = None,
        interval_s: float = 30.0,
        delete_on_shutdown: bool = False,
        registry: CollectorRegistry = REGISTRY,
        task_queue: str = "",
    ) -> None:
        if not url:
            raise ValueError("PushGatewayClient requires a non-empty url")
        if not job:
            raise ValueError("PushGatewayClient requires a non-empty job")
        self._url = url
        self._job = job
        self._grouping_key = grouping_key or _default_grouping_key(task_queue)
        self._interval_s = interval_s
        self._delete_on_shutdown = delete_on_shutdown
        self._registry = registry
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="pushgateway-pusher")
        logger.info(
            "Pushgateway pusher started",
            extra={
                "url": self._url,
                "job": self._job,
                "interval_s": self._interval_s,
                "grouping_key": self._grouping_key,
            },
        )

    async def push_now(self) -> None:
        await asyncio.to_thread(self._push_blocking)

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

        # Final push to capture metrics emitted between the last interval tick
        # and shutdown.
        try:
            await self.push_now()
        except Exception:
            logger.warning("Final Pushgateway push failed", exc_info=True)

        if self._delete_on_shutdown:
            try:
                await asyncio.to_thread(self._delete_blocking)
            except Exception:
                logger.warning("Pushgateway delete on shutdown failed", exc_info=True)

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(), timeout=self._interval_s
                    )
                except asyncio.TimeoutError:
                    pass
                if self._stopped.is_set():
                    return
                try:
                    await self.push_now()
                except Exception:
                    logger.warning("Pushgateway push failed", exc_info=True)
        except asyncio.CancelledError:
            return

    def _push_blocking(self) -> None:
        push_to_gateway(
            self._url,
            job=self._job,
            registry=self._registry,
            grouping_key=self._grouping_key,
        )

    def _delete_blocking(self) -> None:
        delete_from_gateway(
            self._url,
            job=self._job,
            grouping_key=self._grouping_key,
        )


__all__ = ["PushGatewayClient", "TemporalCoreCollector"]
