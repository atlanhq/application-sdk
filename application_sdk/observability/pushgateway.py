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
import re
import socket
from collections.abc import Iterable
from typing import TYPE_CHECKING
from urllib.error import HTTPError

import httpx
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    delete_from_gateway,
    push_to_gateway,
)
from prometheus_client.exposition import default_handler
from prometheus_client.parser import text_string_to_metric_families

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.pushgateway_errors import (
    PushGatewayJobRequiredError,
    PushGatewayUrlRequiredError,
)

#: Regex extracting ``push_time_seconds{labels} value`` lines from a
#: Pushgateway ``/metrics`` response. The Pushgateway emits one such series
#: per ``{job, ...}`` group, recording when that group last received a push.
_PUSH_TIME_LINE = re.compile(
    r"^push_time_seconds\{(?P<labels>.*)\}\s+(?P<ts>[0-9.eE+\-]+)$",
    re.MULTILINE,
)
#: Regex parsing one ``key="value"`` pair out of a Prometheus label string.
_LABEL_PAIR = re.compile(r'(\w+)="([^"]*)"')

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

    def collect(self) -> Iterable[Metric]:
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                resp = client.get(self._url)
            if resp.status_code != 200:
                return
            yield from text_string_to_metric_families(resp.text)
        except Exception:
            # Best-effort — a transient Temporal core hiccup must not poison the push.
            logger.debug(
                "Temporal-core metric scrape failed; skipping this cycle",
                exc_info=True,
            )
            return


def _default_grouping_key(task_queue: str) -> dict[str, str]:
    # Do NOT include task_queue here: Temporal metrics already carry task_queue
    # as a metric label, and the Pushgateway rejects pushes where a label appears
    # in both the metric body and the grouping-key URL path (400 Bad Request).
    return {"instance": socket.gethostname()}


def _log_push_failure(error: HTTPError, request_body: bytes) -> None:
    """Log a Pushgateway 4xx with the response body and the offending request lines.

    The bare HTTPError discards the response body, which is where Pushgateway
    puts the rejection reason (e.g. "second TYPE line for metric name 'x'",
    "invalid metric name in comment"). When the reason references a line
    number, also include a ±2 line slice of the request body to make the
    cause visible without re-running.

    Wrapped so a bug in the diagnostic itself never replaces the HTTPError
    that ``_run`` expects to catch and retry.
    """
    try:
        resp = ""
        try:
            resp = error.read().decode("utf-8", errors="replace")[:2000]
        except Exception:  # noqa: S110 — diagnostic-only
            pass
        msg = (
            f"Pushgateway PUT {error.url} returned {error.code}: {resp or error.reason}"
        )
        m = re.search(r"line (\d+)", resp)
        if m and request_body:
            line_no = int(m.group(1))
            lines = request_body.decode("utf-8", errors="replace").splitlines()
            lo, hi = max(0, line_no - 3), min(len(lines), line_no + 2)
            snippet = "\n".join(
                f"{'>>>' if (lo + i + 1) == line_no else '   '} {lo + i + 1}: {ln}"
                for i, ln in enumerate(lines[lo:hi])
            )
            msg += f"\n{snippet}"
        logger.warning(msg)
    except Exception:
        logger.warning("Pushgateway PUT failed (%s)", error, exc_info=True)


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
        sweep_stale_on_start: bool = False,
        sweep_staleness_seconds: float = 300.0,
        http_timeout_s: float = 10.0,
        shutdown_delete_delay_s: float = 35.0,
        registry: CollectorRegistry = REGISTRY,
        task_queue: str = "",
    ) -> None:
        if not url:
            raise PushGatewayUrlRequiredError()
        if not job:
            raise PushGatewayJobRequiredError()
        self._url = url
        self._job = job
        self._grouping_key = grouping_key or _default_grouping_key(task_queue)
        self._interval_s = interval_s
        self._delete_on_shutdown = delete_on_shutdown
        self._sweep_stale_on_start = sweep_stale_on_start
        self._sweep_staleness_seconds = sweep_staleness_seconds
        self._http_timeout_s = http_timeout_s
        self._shutdown_delete_delay_s = shutdown_delete_delay_s
        self._registry = registry
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        # Sweep stale predecessor groups before the first push so the gateway
        # doesn't show ghost pods from previous OOM/eviction failures while
        # this pod is also pushing fresh data.
        if self._sweep_stale_on_start:
            try:
                await asyncio.to_thread(self._sweep_stale_predecessors_blocking)
            except Exception:
                logger.warning(
                    "Pushgateway startup sweep failed — proceeding with push",
                    exc_info=True,
                )
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
            except (asyncio.CancelledError, Exception):  # noqa: S110 — task cancellation is the goal; surface nothing on shutdown
                pass
            self._task = None

        # Final push to capture metrics emitted between the last interval tick
        # and shutdown.
        try:
            await self.push_now()
        except Exception:
            logger.warning("Final Pushgateway push failed", exc_info=True)

        if self._delete_on_shutdown:
            # Sleep before DELETE so Prometheus has at least one scrape window
            # to read the just-pushed final batch. Without this, the DELETE
            # fires within milliseconds of the push and Prometheus' next scrape
            # finds nothing — the last interval of metrics (often the failure
            # increments that triggered scale-down) is lost. Worker
            # terminationGracePeriodSeconds is 12h so the wait is negligible
            # against the kill timeout.
            if self._shutdown_delete_delay_s > 0:
                logger.info(
                    "Holding Pushgateway group for %.0fs before DELETE so "
                    "Prometheus can scrape the final push",
                    self._shutdown_delete_delay_s,
                )
                try:
                    await asyncio.sleep(self._shutdown_delete_delay_s)
                except asyncio.CancelledError:
                    # If shutdown is itself being cancelled (e.g. caller bound
                    # __aexit__ to a tighter timeout), skip the delay and the
                    # DELETE — the group will be reaped by the next pod's
                    # startup sweep instead.
                    raise
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
                except TimeoutError:
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
        captured: dict[str, bytes] = {"data": b""}

        def capturing_handler(url, method, timeout, headers, data):
            captured["data"] = data
            return default_handler(url, method, timeout, headers, data)

        try:
            push_to_gateway(
                self._url,
                job=self._job,
                registry=self._registry,
                grouping_key=self._grouping_key,
                timeout=self._http_timeout_s,
                handler=capturing_handler,
            )
        except HTTPError as error:
            _log_push_failure(error, captured["data"])
            raise

    def _delete_blocking(self) -> None:
        delete_from_gateway(
            self._url,
            job=self._job,
            grouping_key=self._grouping_key,
            timeout=self._http_timeout_s,
        )

    def _sweep_stale_predecessors_blocking(self) -> None:
        """Reap stale ``{job=mine, instance=other}`` groups left by previous
        worker pods that died before they could DELETE themselves (OOM kill,
        eviction, SIGKILL, node loss).

        Safety guards layered top-to-bottom:

        1. **Strict job-equality** — never touch groups with a different
           ``job`` label, even on a multi-tenant gateway.
        2. **Skip own grouping_key** — defensively avoid reaping ourselves
           if any pre-existing series remains under our own group.
        3. **Threshold gate** — leave any group whose last push is more
           recent than ``self._sweep_staleness_seconds``. This protects
           live siblings during Temporal Worker Deployments overlap (old
           pod still draining workflows pushes every 30 s).
        4. **Soft-fail per group** — one DELETE timing out / 4xx-ing must
           not abort the rest of the sweep.

        Runs once on startup before the periodic push loop. Failures here
        never block the worker — they only mean the leak persists until
        the next pod's sweep.
        """
        import time  # noqa: PLC0415 — cold path: only on worker startup

        # 1. Fetch the gateway's exposition. push_time_seconds is the
        # bookkeeping series the gateway emits per group; we use it to
        # discover groups and determine staleness in a single hop.
        try:
            with httpx.Client(timeout=self._http_timeout_s) as client:
                resp = client.get(f"{self._url}/metrics")
            # Mirror prometheus_client's symmetric behavior: any 2xx is
            # success, only 4xx/5xx triggers a skip. Pushgateway returns 200
            # for /metrics in practice, but accepting all 2xx is more robust
            # to proxies and edge-gateway behavior.
            resp.raise_for_status()
            text = resp.text
        except (httpx.HTTPError, OSError):
            logger.warning(
                "Pushgateway sweep skipped: GET /metrics failed", exc_info=True
            )
            return

        now = time.time()
        my_instance = self._grouping_key.get("instance")
        deleted = skipped_other_job = skipped_self = skipped_live = 0

        for match in _PUSH_TIME_LINE.finditer(text):
            try:
                last_push = float(match.group("ts"))
            except ValueError:
                continue
            labels = dict(_LABEL_PAIR.findall(match.group("labels")))
            # Guard 1: strict job-scope. Multi-tenant gateways host many
            # apps; we only reap our own ghosts.
            if labels.get("job") != self._job:
                skipped_other_job += 1
                continue
            # Guard 2: never reap our own group.
            if labels.get("instance") == my_instance:
                skipped_self += 1
                continue
            # Guard 3: leave live siblings alone.
            age = now - last_push
            if age <= self._sweep_staleness_seconds:
                skipped_live += 1
                continue
            # Build grouping_key (everything except job).
            group_key = {k: v for k, v in labels.items() if k != "job"}
            try:
                delete_from_gateway(
                    self._url,
                    job=self._job,
                    grouping_key=group_key,
                    timeout=self._http_timeout_s,
                )
                deleted += 1
                logger.info(
                    "Swept stale Pushgateway group",
                    extra={
                        "instance": labels.get("instance", "?"),
                        "stale_for_seconds": round(age, 1),
                        "job": self._job,
                    },
                )
            except Exception:
                # Guard 4: per-group soft-fail.
                logger.warning(
                    "Pushgateway sweep DELETE failed for instance=%s",
                    labels.get("instance", "?"),
                    exc_info=True,
                )

        logger.info(
            "Pushgateway startup sweep complete",
            extra={
                "deleted": deleted,
                "skipped_self": skipped_self,
                "skipped_live": skipped_live,
                "skipped_other_job": skipped_other_job,
                "job": self._job,
                "staleness_threshold_seconds": self._sweep_staleness_seconds,
            },
        )


__all__ = ["PushGatewayClient", "TemporalCoreCollector"]
