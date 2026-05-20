"""ObjectStore metric exporter for the OTel SDK.

Receives ``MetricsData`` from ``PeriodicExportingMetricReader`` on a
background thread, serializes each data point as an NDJSON record,
writes a ``.json.gz`` file to local disk, and uploads it to the
deployment and (optionally) upstream object stores.

Best-effort: upload failures are logged and swallowed — the exporter
always returns ``SUCCESS`` so a flaky store never blocks metric
collection.
"""

from __future__ import annotations

import gzip
import os
import posixpath
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    ObservableCounter,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import AggregationTemporality
from opentelemetry.sdk.metrics.export import Gauge as GaugeData
from opentelemetry.sdk.metrics.export import Histogram as HistogramData
from opentelemetry.sdk.metrics.export import (
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.metrics.export import Sum as SumData

from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_NAME,
    DEPLOYMENT_OBJECT_STORE_NAME,
    ENABLE_ATLAN_UPLOAD,
    ENABLE_OBSERVABILITY_STORE_SINK,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.observability import OBSERVABILITY_S3_PREFIX_MAP
from application_sdk.observability.utils import get_observability_dir

if TYPE_CHECKING:
    from obstore.store import ObjectStore
    from opentelemetry.sdk.metrics.export import HistogramDataPoint, NumberDataPoint
    from opentelemetry.sdk.resources import Resource

logger = get_logger(__name__)

#: S3 prefix for OTel-sourced Prometheus metrics.  Kept separate from the
#: ``metrics/`` prefix used by ``record_metric()`` data so consumers can
#: distinguish the two streams.
_S3_PREFIX = OBSERVABILITY_S3_PREFIX_MAP.get(
    "metrics", "artifacts/apps/observability/sdr/metrics"
).replace("/metrics", "/prometheus-metrics")


def _resource_attrs(resource: Resource) -> dict[str, str]:
    """Extract resource attributes as a plain string dict."""
    return {str(k): str(v) for k, v in resource.attributes.items()}


def _number_record(
    name: str,
    description: str | None,
    unit: str | None,
    metric_type: str,
    dp: NumberDataPoint,
    resource_attributes: dict[str, str],
    is_monotonic: bool | None = None,
    aggregation_temporality: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": dp.time_unix_nano / 1e9,
        "name": name,
        "type": metric_type,
        "value": dp.value,
        "attributes": dict(dp.attributes) if dp.attributes else {},
        "resource_attributes": resource_attributes,
    }
    if description:
        record["description"] = description
    if unit:
        record["unit"] = unit
    if is_monotonic is not None:
        record["is_monotonic"] = is_monotonic
    if aggregation_temporality:
        record["aggregation_temporality"] = aggregation_temporality
    return record


def _histogram_record(
    name: str,
    description: str | None,
    unit: str | None,
    dp: HistogramDataPoint,
    resource_attributes: dict[str, str],
    aggregation_temporality: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": dp.time_unix_nano / 1e9,
        "name": name,
        "type": "histogram",
        "value": {
            "count": dp.count,
            "sum": dp.sum,
            "bucket_counts": list(dp.bucket_counts),
            "explicit_bounds": list(dp.explicit_bounds),
        },
        "attributes": dict(dp.attributes) if dp.attributes else {},
        "resource_attributes": resource_attributes,
    }
    if description:
        record["description"] = description
    if unit:
        record["unit"] = unit
    if aggregation_temporality:
        record["aggregation_temporality"] = aggregation_temporality
    return record


def _temporality_name(t: AggregationTemporality) -> str:
    if t == AggregationTemporality.DELTA:
        return "delta"
    return "cumulative"


def _serialize_metrics_data(metrics_data: MetricsData | None) -> list[dict[str, Any]]:
    """Flatten ``MetricsData`` into a list of JSON-serializable dicts."""
    if metrics_data is None:
        return []
    records: list[dict[str, Any]] = []
    for rm in metrics_data.resource_metrics:
        res_attrs = _resource_attrs(rm.resource)
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                data = metric.data
                if isinstance(data, SumData):
                    temp = _temporality_name(data.aggregation_temporality)
                    for dp in data.data_points:
                        records.append(
                            _number_record(
                                metric.name,
                                metric.description,
                                metric.unit,
                                "sum",
                                dp,
                                res_attrs,
                                is_monotonic=data.is_monotonic,
                                aggregation_temporality=temp,
                            )
                        )
                elif isinstance(data, GaugeData):
                    for dp in data.data_points:
                        records.append(
                            _number_record(
                                metric.name,
                                metric.description,
                                metric.unit,
                                "gauge",
                                dp,
                                res_attrs,
                            )
                        )
                elif isinstance(data, HistogramData):
                    temp = _temporality_name(data.aggregation_temporality)
                    for dp in data.data_points:
                        records.append(
                            _histogram_record(
                                metric.name,
                                metric.description,
                                metric.unit,
                                dp,
                                res_attrs,
                                aggregation_temporality=temp,
                            )
                        )
    return records


def _write_ndjson_gz(records: Sequence[dict[str, Any]], path: str) -> None:
    """Write records as gzip-compressed NDJSON to *path*."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wb") as f:
        for record in records:
            f.write(orjson.dumps(record) + b"\n")


class ObjectStoreMetricExporter(MetricExporter):
    """Exports OTel metrics as NDJSON.gz files to ObjectStore.

    Intended to be paired with ``PeriodicExportingMetricReader``.
    Uses **delta** temporality for counters so each export is
    self-contained (no ever-growing cumulative values in files).
    """

    def __init__(self, *, data_dir: str | None = None) -> None:
        super().__init__(
            preferred_temporality={
                Counter: AggregationTemporality.DELTA,
                UpDownCounter: AggregationTemporality.DELTA,
                ObservableCounter: AggregationTemporality.DELTA,
                ObservableUpDownCounter: AggregationTemporality.DELTA,
                Histogram: AggregationTemporality.DELTA,
            },
        )
        self._data_dir = data_dir or get_observability_dir()
        self._deployment_store: ObjectStore | None = (
            self._resolve_store(DEPLOYMENT_OBJECT_STORE_NAME)
            if ENABLE_OBSERVABILITY_STORE_SINK
            else None
        )
        self._upstream_store: ObjectStore | None = (
            self._resolve_store(UPSTREAM_OBJECT_STORE_NAME)
            if ENABLE_OBSERVABILITY_STORE_SINK and ENABLE_ATLAN_UPLOAD
            else None
        )

    def _resolve_store(self, name: str) -> ObjectStore | None:
        from application_sdk.storage.binding import (  # noqa: PLC0415 — deferred to break the observability→storage circular import
            create_store_from_binding,
        )
        from application_sdk.storage.errors import (  # noqa: PLC0415 — deferred to break the observability→storage circular import
            StorageConfigError,
        )

        try:
            return create_store_from_binding(name)
        except StorageConfigError:
            logger.warning(
                "Object store '%s' not configured; metric upload disabled",
                name,
            )
            return None
        except Exception:
            logger.warning(
                "Object store '%s' setup failed; metric upload disabled",
                name,
                exc_info=True,
            )
            return None

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: Any,
    ) -> MetricExportResult:
        if not ENABLE_OBSERVABILITY_STORE_SINK:
            return MetricExportResult.SUCCESS

        try:
            records = _serialize_metrics_data(metrics_data)
            if not records:
                return MetricExportResult.SUCCESS

            now = datetime.now()
            ts_ns = int(now.timestamp() * 1e9)
            filename = f"{ts_ns}_{DEPLOYMENT_NAME}_{APPLICATION_NAME}.json.gz"

            local_dir = os.path.join(
                self._data_dir,
                "sdr" if ENABLE_ATLAN_UPLOAD else "non-sdr",
                "prometheus-metrics",
                f"year={now.year}",
                f"month={now.month:02d}",
                f"day={now.day:02d}",
                f"hour={now.hour:02d}",
            )
            local_path = os.path.join(local_dir, filename)

            remote_key = posixpath.join(
                _S3_PREFIX,
                f"year={now.year}",
                f"month={now.month:02d}",
                f"day={now.day:02d}",
                f"hour={now.hour:02d}",
                filename,
            )

            _write_ndjson_gz(records, local_path)

            try:
                if self._deployment_store is not None:
                    try:
                        self._deployment_store.put(remote_key, Path(local_path))
                    except Exception:
                        logger.warning(
                            "ObjectStore metric upload to deployment store failed",
                            exc_info=True,
                        )
                if self._upstream_store is not None:
                    try:
                        self._upstream_store.put(remote_key, Path(local_path))
                    except Exception:
                        logger.warning(
                            "ObjectStore metric upload to upstream store failed",
                            exc_info=True,
                        )
            finally:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass  # best-effort local-file cleanup; never block metric export

        except Exception:
            logger.warning("ObjectStore metric export failed", exc_info=True)

        # Always succeed — never block metric collection.
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        pass
