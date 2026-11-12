"""ObjectStore metric exporter for the OTel SDK.

Receives ``MetricsData`` from ``PeriodicExportingMetricReader`` on a
background thread, serializes each data point as an NDJSON record, and
hands the batch to the observability writer the ``AtlanObservability``
sinks use, which writes a ``.json.gz`` file and uploads it to the
deployment and (in SDR mode) upstream object stores.

Best-effort: upload failures are logged and swallowed — the exporter
always returns ``SUCCESS`` so a flaky store never blocks metric
collection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    ObservableCounter,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.metrics.export import Gauge as GaugeData
from opentelemetry.sdk.metrics.export import Histogram as HistogramData
from opentelemetry.sdk.metrics.export import Sum as SumData

from application_sdk.constants import ENABLE_OBSERVABILITY_STORE_SINK
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.observability import _write_and_upload_batch
from application_sdk.observability.utils import get_observability_dir

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics.export import HistogramDataPoint, NumberDataPoint
    from opentelemetry.sdk.resources import Resource

logger = get_logger(__name__)

#: Signal key in ``LOCAL_OBS_SUBDIR_MAP`` / ``OBSERVABILITY_S3_PREFIX_MAP`` for
#: OTel-sourced Prometheus metrics. Kept separate from the ``metrics`` signal
#: used by ``record_metric()`` data so consumers can tell the two streams apart.
_SIGNAL = "prometheus-metrics"


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
        # Stores are resolved by the shared writer on each export, never here
        # (BLDX-1567): this exporter can be built during app-server boot, before
        # the Dapr ``objectstore`` component is written.
        self._writer_seq = 0

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
            self._writer_seq += 1
            # The same writer the AtlanObservability sinks use: store bindings
            # resolved from DAPR_COMPONENTS_PATH with their put attributes
            # (``storageClass``), uploads through ``upload_file``, and a binding
            # still missing at boot retried on each export until it appears.
            _write_and_upload_batch(
                records,
                signal=_SIGNAL,
                data_dir=self._data_dir,
                seq=self._writer_seq,
                timeout=timeout_millis / 1000,
            )
        except Exception:
            logger.warning("ObjectStore metric export failed", exc_info=True)

        # Always succeed — never block metric collection.
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        pass
