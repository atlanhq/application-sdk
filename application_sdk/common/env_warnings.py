"""Detect removed env vars and warn at startup.

When env vars are removed across SDK versions, deployment manifests still
carrying them silently no-op — the SDK behaves as if they were never set.
This module emits a single ``warning`` log line listing every detected
removed var so the mismatch is obvious in container logs and the dev knows
to add the newer SDK equivalents.

The intent is awareness, not cleanup: charts often span multiple SDK
versions and may need to keep the old vars set for older deployments.

Add new entries to ``_REMOVED_ENV_VARS`` whenever an env var is dropped.
We deliberately don't track replacements here — the dev can read the SDK /
changelog to find the new equivalent.
"""

from __future__ import annotations

import os

#: Env vars the SDK no longer reads. Keep curated — every entry that is set
#: in the environment becomes part of one warning line at startup.
_REMOVED_ENV_VARS: frozenset[str] = frozenset(
    {
        # App Vitals interceptor was replaced by the unified LogInterceptor
        # + lifecycle log lines.
        "ATLAN_ENABLE_APP_VITALS",
        # Bespoke OTLP metric exporter was removed; metrics now flow through
        # the Prometheus reader (FastAPI /metrics + Pushgateway).
        "ATLAN_ENABLE_OTLP_METRICS",
        # Renamed to ATLAN_ENABLE_TEMPORAL_CORE_METRICS — the previous name
        # implied control over all Prometheus metrics, but the flag now only
        # gates the Temporal Rust-core endpoint bind / proxy.
        "ATLAN_ENABLE_PROMETHEUS_METRICS",
        # The dedicated TraceAdapter and its parquet pipeline were removed
        # in favour of standard OTLP trace export.
        "ATLAN_TRACES_BATCH_SIZE",
        "ATLAN_TRACES_CLEANUP_ENABLED",
        "ATLAN_TRACES_FLUSH_INTERVAL_SECONDS",
        "ATLAN_TRACES_RETENTION_DAYS",
        # CorrelationContextInterceptor was folded into LogInterceptor;
        # no longer toggleable.
        "APPLICATION_SDK_ENABLE_CORRELATION_INTERCEPTOR",
    }
)


def warn_removed_env_vars() -> None:
    """Warn once at startup if any removed env vars are set.

    Emits a single ``warning`` log line listing every detected var.
    Call once at process startup (e.g. from ``run_main()``).
    """
    detected = sorted(name for name in _REMOVED_ENV_VARS if os.environ.get(name))
    if not detected:
        return

    # Lazy import: ``logger_adaptor`` is heavy and pulls in observability
    # bootstrap. We don't want to force-load it on processes that just
    # import this helper for testing.
    from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — startup-only lazy import
        get_logger,
    )

    get_logger(__name__).warning(
        "The SDK no longer reads these env vars (set in environment, values "
        "ignored): %s. Consult the SDK / changelog for current equivalents.",
        ", ".join(detected),
    )
