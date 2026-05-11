"""Resource-attribute enrichment for the Prometheus metric reader.

OTel's default Prometheus exporter exposes resource attributes via a single
``target_info`` series, requiring consumers to JOIN at query time:

    rate(http_server_duration_milliseconds_count[5m])
        * on (instance) group_left(app_name) target_info

For ergonomics, we eagerly inline a bounded subset of resource attributes
onto every emitted series so that simple PromQL like
``http_server_duration_milliseconds_count{app_name="teradata"}`` works
without joins.

Cardinality discipline: only attributes with bounded *per-process*
cardinality should be enriched. ``app.name`` is constant per pod (always
``"teradata"`` for teradata-app), so adding it as a label multiplies the
series count by 1. ``app.version`` is bounded by the number of releases
ever scraped — small. Pod-level resource attrs (``k8s.pod.name``,
``k8s.pod.uid``) are deliberately NOT enriched because the ``instance``
label Prometheus auto-adds at scrape time already covers that dimension;
duplicating it would just bloat exposition body size.

If a metric already carries one of the enrichment keys via
``record_metric()`` or an OTel attribute, the existing value wins (we
merge with the metric's labels taking precedence over the resource).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# NOTE: ``_CustomCollector`` and ``self._collector`` / ``self._collector._callback``
# (used in ``EnrichedPrometheusMetricReader.__init__`` below) are **private APIs**
# of ``opentelemetry-exporter-prometheus``. The package is intentionally pinned
# tightly in pyproject.toml; any version bump must re-verify these symbols still
# exist and behave the same.  Verified intact through 0.62b1.
# ``tests/unit/observability/test_prometheus_enrichment.py`` instantiates the
# reader for real (no mocks) and will fail fast if the symbols disappear.
from opentelemetry.exporter.prometheus import PrometheusMetricReader, _CustomCollector
from prometheus_client.core import REGISTRY as _PROM_REGISTRY
from prometheus_client.core import Metric as _PrometheusMetric
from prometheus_client.core import Sample

from application_sdk.observability.utils import METRIC_ENRICHMENT_KEYS

#: target_info family name to skip during enrichment (it carries the full
#: resource already; touching its samples would shadow that information).
_TARGET_INFO_FAMILY = "target"


class _EnrichedCollector(_CustomCollector):
    """Wraps the upstream ``_CustomCollector`` to inject bounded resource
    attributes as labels on every emitted sample (other than target_info)."""

    def __init__(
        self, disable_target_info: bool, enrichment: Mapping[str, str]
    ) -> None:
        super().__init__(disable_target_info)
        self._enrichment = dict(enrichment)

    def collect(self) -> Iterable[_PrometheusMetric]:
        for family in super().collect():
            if family.name == _TARGET_INFO_FAMILY:
                yield family
                continue
            family.samples = [
                Sample(
                    s.name,
                    {**self._enrichment, **s.labels},
                    s.value,
                    s.timestamp,
                    s.exemplar,
                )
                for s in family.samples
            ]
            yield family


class EnrichedPrometheusMetricReader(PrometheusMetricReader):
    """Drop-in replacement for ``PrometheusMetricReader`` that inlines a
    bounded subset of the OTel resource onto every metric series.

    Construct with the same OTel ``Resource`` you pass to ``MeterProvider``:

        reader = EnrichedPrometheusMetricReader(resource=build_otel_resource())
        provider = MeterProvider(resource=resource, metric_readers=[reader])
    """

    def __init__(self, *, resource, disable_target_info: bool = False) -> None:
        # Build the enrichment dict from the resource. Prometheus label keys
        # cannot contain dots, so transliterate to underscores, matching how
        # the OTel exporter does it for target_info.
        enrichment = {
            k.replace(".", "_"): str(v)
            for k, v in resource.attributes.items()
            if k in METRIC_ENRICHMENT_KEYS
        }
        # Run the parent __init__ (it constructs and registers a default
        # collector). Then swap that collector out for our enriching one.
        # Using the parent's register/unregister flow keeps us robust to
        # upstream changes in the parent's preferred_temporality dict.
        super().__init__(disable_target_info=disable_target_info)
        _PROM_REGISTRY.unregister(self._collector)
        self._collector = _EnrichedCollector(disable_target_info, enrichment)
        _PROM_REGISTRY.register(self._collector)
        self._collector._callback = self.collect
