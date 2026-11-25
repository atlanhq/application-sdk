"""Smoke tests for EnrichedPrometheusMetricReader private-API contract.

These tests instantiate the reader for real (no mocks) so that breakage of the
private opentelemetry-exporter-prometheus symbols it depends on is caught at CI
time rather than at runtime.

If any test here fails after a dependency bump:
  - check that ``_CustomCollector`` still exists in
    ``opentelemetry.exporter.prometheus``
  - check that ``PrometheusMetricReader._collector`` is still an instance attr
  - check that ``_CustomCollector`` instances still have a ``._callback`` attr
See ``application_sdk/observability/_prometheus_enrichment.py`` for full context.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from prometheus_client.core import REGISTRY


@pytest.fixture()
def resource() -> Resource:
    return Resource.create({"app.name": "smoke-test", "service.name": "test"})


@pytest.fixture()
def reader(resource: Resource):
    from application_sdk.observability._prometheus_enrichment import (
        EnrichedPrometheusMetricReader,
    )

    r = EnrichedPrometheusMetricReader(resource=resource)
    yield r
    # Unregister in case no MeterProvider shutdown cleaned it up (e.g. when the
    # reader fixture is used without the provider fixture).
    try:
        REGISTRY.unregister(r._collector)
    except Exception:  # noqa: S110 — expected when provider.shutdown() already cleaned up
        pass


@pytest.fixture()
def provider(resource: Resource, reader):
    p = MeterProvider(resource=resource, metric_readers=[reader])
    yield p
    # shutdown() unregisters the collector from REGISTRY; the reader fixture
    # teardown then catches the redundant unregister silently.
    try:
        p.shutdown()
    except Exception:  # noqa: S110 — shutdown KeyError is benign if collector already unregistered
        pass


class TestPrivateAPIContract:
    """Verify the private symbols EnrichedPrometheusMetricReader depends on
    are still present after a version bump of opentelemetry-exporter-prometheus."""

    def test_custom_collector_importable(self) -> None:
        from opentelemetry.exporter.prometheus import _CustomCollector

        assert _CustomCollector is not None

    def test_collector_attr_exists_after_init(self, reader) -> None:
        assert hasattr(reader, "_collector"), (
            "PrometheusMetricReader no longer exposes ._collector — the swap "
            "in EnrichedPrometheusMetricReader.__init__ will silently no-op."
        )

    def test_collector_callback_callable(self, reader) -> None:
        cb = getattr(reader._collector, "_callback", None)
        assert callable(cb), (
            "_CustomCollector no longer has a ._callback attr — enrichment "
            "will never be triggered."
        )

    def test_collector_is_enriched_type(self, reader) -> None:
        from application_sdk.observability._prometheus_enrichment import (
            _EnrichedCollector,
        )

        assert isinstance(reader._collector, _EnrichedCollector), (
            "Collector swap failed: ._collector is still the base "
            f"_CustomCollector, not _EnrichedCollector. type={type(reader._collector)}"
        )


class TestEnrichmentBehavior:
    """Verify resource attributes actually appear as labels on emitted series."""

    def test_app_name_inlined_on_counter(self, provider, reader) -> None:
        meter = provider.get_meter("smoke_test")
        counter = meter.create_counter("smoke_hits")
        counter.add(3, {"endpoint": "/ping"})

        sample_labels: dict[str, str] = {}
        for family in reader._collector.collect():
            if "smoke_hits" in family.name:
                for sample in family.samples:
                    sample_labels = sample.labels
                    break

        assert "app_name" in sample_labels, (
            f"app.name was not inlined as app_name onto the counter series. "
            f"Labels seen: {sample_labels}"
        )
        assert sample_labels["app_name"] == "smoke-test"

    def test_metric_labels_win_over_enrichment(self, provider, reader) -> None:
        """Per-metric label for ``app_name`` must shadow the resource enrichment."""
        meter = provider.get_meter("smoke_test")
        counter = meter.create_counter("smoke_override")
        counter.add(1, {"app_name": "caller-wins"})

        for family in reader._collector.collect():
            if "smoke_override" in family.name:
                for sample in family.samples:
                    assert sample.labels.get("app_name") == "caller-wins", (
                        f"Resource enrichment overwrote per-metric label. "
                        f"Labels: {sample.labels}"
                    )
                    return

        pytest.fail("smoke_override metric not found in collected output")

    def test_target_info_carries_the_resource_unchanged(
        self, provider, reader, resource: Resource
    ) -> None:
        """target_info passes through as the full resource — no labels added.

        The exporter emits target_info only alongside at least one metric, so
        record one; without it the family is absent and nothing is checked.
        ``app_name`` is expected here: it is a resource attribute, so the
        exporter puts it on target_info itself.
        """
        provider.get_meter("smoke_test").create_counter("smoke_target").add(1)
        target_families = [f for f in reader._collector.collect() if f.name == "target"]
        assert target_families, "target_info family missing from collected output"

        expected = {k.replace(".", "_"): str(v) for k, v in resource.attributes.items()}
        for family in target_families:
            for sample in family.samples:
                assert sample.labels == expected
