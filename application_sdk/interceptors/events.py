"""Deprecated: interceptors are auto-registered by create_worker() — remove this import."""

import warnings

warnings.warn(
    "application_sdk.interceptors.events is deprecated and will be removed in v3.1.0. "
    "Interceptors are auto-registered by create_worker() — remove this import.",
    DeprecationWarning,
    stacklevel=2,
)

from application_sdk.execution._temporal.interceptors.events import *  # noqa: E402, F401, F403
from application_sdk.execution._temporal.interceptors.events import (  # noqa: E402, F401
    EventActivityInboundInterceptor,
    EventInterceptor,
    EventWorkflowInboundInterceptor,
    _enrich_event_metadata,
    _publish_event_via_binding,
    _send_lifecycle_event_to_segment,
    publish_event,
)
