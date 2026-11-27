"""The request seam every :mod:`application_sdk.storage.ops` store call goes through.

Two pieces, both shared by the upload, download, listing and HEAD paths:

* :func:`_past_gateway_no_route` — rides out the gateway's transient
  "no Route matched" 404, with :data:`_retry_sleep` as the indirection tests
  record the backoff through;
* :func:`_log_storage_event` — the per-attempt structured storage event, and
  its mirror onto the transfer metrics.

The event logger keeps the ``application_sdk.storage.ops`` name it has always
logged under, so log queries that select on it keep matching.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from application_sdk import _storage_classify
from application_sdk._install import api_only_install
from application_sdk._logging import get_logger
from application_sdk.storage._telemetry import (
    _record_transfer_metric,
    _throughput_mbps,
)

logger = get_logger("application_sdk.storage.ops")

_T = TypeVar("_T")


# Cap for the logged copy of a driver error. Larger than the wire envelope's
# cap on purpose — the point of the log copy is to outlive the truncation — but
# still bounded, because ``error_message`` promotes to an indexed OTLP
# attribute. Matches ``safe_traceback``'s ceiling.
_LOG_CAUSE_MAX_LEN = 8000


# A Kong gateway instance that joins the load balancer before its route table
# has synced answers every request with ``404 {"message":"no Route matched with
# those values"}`` for a few seconds. The rule and the backoff are the shared
# table's (:mod:`application_sdk._storage_classify`), so the API host rides the
# same answer out the same way.
_GATEWAY_NO_ROUTE_RETRY_DELAYS = _storage_classify.GATEWAY_NO_ROUTE_RETRY_DELAYS


# Indirection so tests can record the backoff without sleeping through it.
_retry_sleep = asyncio.sleep


async def _past_gateway_no_route(
    op: str, key: str, call: Callable[[], Awaitable[_T]]
) -> _T:
    """Run one store request, retrying the gateway's "no Route matched" 404.

    The store seam: every request this module sends goes through here (the
    upload attempt, HEAD, GET, PUT, DELETE and listing), so the post-upload
    readback, the integrity sidecar write, downloads and listings all ride
    out a gateway instance that has not synced its routes, not only the
    upload. Wrap the *request*, not a stream consumer: a retried ``call``
    must start over cleanly. Every other failure propagates on the first
    attempt, unchanged.
    """
    return await _storage_classify.past_gateway_no_route(
        op, key, call, sleep=_retry_sleep
    )


def _is_gateway_no_route(exc: BaseException) -> bool:
    """True for the gateway's transient "no Route matched" 404, and only that."""
    return _storage_classify.is_gateway_no_route(exc)


def _log_storage_event(
    level: int,
    op: str,
    store_path: str,
    *,
    outcome: str,
    elapsed_ms: float | None = None,
    size_bytes: int | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    """Emit a single structured per-attempt storage event.

    Fields are placed on ``extra`` so structured-log backends and pytest's
    caplog see them as record attributes; the human-readable message stays
    short for unstructured tail / grep workflows.
    """
    extra: dict[str, object] = {
        "storage_op": op,
        "store_path": store_path,
        "outcome": outcome,
    }
    tput: float | None = None
    if elapsed_ms is not None:
        extra["elapsed_ms"] = round(elapsed_ms, 3)
    if size_bytes is not None:
        extra["size_bytes"] = size_bytes
        if elapsed_ms is not None:
            tput = _throughput_mbps(size_bytes, elapsed_ms)
            if tput is not None:
                extra["throughput_mibps"] = tput
    if error_class is not None:
        extra["error_class"] = error_class
    if error_message is not None and not api_only_install():
        # Not on the API host: its failure logs carry only the classified
        # evidence (handler/object_routes.py), and it has no OTLP pipeline for
        # this copy to outlive the envelope's truncation in.
        #
        # The wire envelope length-caps ``cause_repr``, so without this the
        # provider's own explanation had nowhere to live: this event previously
        # recorded only ``error_class`` ("GenericError"), with no ``str(exc)``,
        # no ``exc_info`` and no ``safe_traceback``. A copy here gives a
        # truncated envelope a higher-fidelity counterpart in the logs.
        # ``error_message`` is already in ``_KNOWN_EXTRA_KEYS``, so it promotes
        # to an indexed OTLP attribute — hence the cap, for the same reason
        # ``safe_traceback`` has one. Callers pass secret-redacted text.
        # (FND-957)
        extra["error_message"] = error_message[:_LOG_CAUSE_MAX_LEN]
    msg = f"storage.{op} {outcome} path={store_path}"
    # The SDK logger spreads ``extra`` into loguru's record["extra"], and
    # _build_extra_dict in logger_adaptor promotes the keys to OTLP indexed
    # attributes (all are in _KNOWN_EXTRA_KEYS). On the API host it is a
    # stdlib logger, which sets them as record attributes.
    logger.log(level, msg, extra=extra)

    # Mirror the terminal event to metrics so throughput / failure rate are
    # dashboardable + alertable across the fleet (only for actual transfers).
    if op in ("download", "upload"):
        _record_transfer_metric(
            op,
            outcome=outcome,
            elapsed_ms=elapsed_ms,
            size_bytes=size_bytes,
            throughput_mibps=tput,
            error_class=error_class,
        )
