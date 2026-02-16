"""Metrics adaptor; uses light implementation when APPLICATION_MODE=SERVER to save memory."""

from application_sdk.constants import APPLICATION_MODE, ApplicationMode

if APPLICATION_MODE == ApplicationMode.SERVER:
    from application_sdk.observability.metrics_adaptor_light import get_metrics
    from application_sdk.observability.models import MetricRecord, MetricType
else:
    from application_sdk.observability.metrics_adaptor_full import (
        MetricRecord,
        MetricType,
        get_metrics,
    )
