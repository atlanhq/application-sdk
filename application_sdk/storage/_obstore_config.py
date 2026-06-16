"""obstore client + retry configuration plumbed from environment variables.

obstore-rs (the Rust layer behind ``S3Store``/``GCSStore``/``AzureStore``)
already does best-in-class retry with exponential backoff and jitter.  What
we *cannot* tune from the SDK without these helpers is its **client-level
timeouts and connection pool**, which are sized for small objects by default
and cause GB-class downloads to fail with timeout-stalled streams.

A production RCA and the in-tree fivetran-app workaround
(``binding_cfg.config["client_options"] = {"timeout": "30m"}``) both point to
the same gap: ``client_options`` and ``retry_config`` are never plumbed
through ``application_sdk/storage/binding.py`` or
``application_sdk/storage/cloud.py``.

This module is the single, env-var-driven source of truth for those values.

Environment variables
---------------------

Client options (passed via ``S3Store(client_options=…)``):

* ``ATLAN_OBSTORE_TIMEOUT`` — per-request timeout (default ``"90s"``).
* ``ATLAN_OBSTORE_CONNECT_TIMEOUT`` — connect-phase timeout (default ``"30s"``).
* ``ATLAN_OBSTORE_POOL_IDLE_TIMEOUT`` — pool idle timeout (default ``"90s"``).
* ``ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST`` — pool size per host (unset by
  default — leaves the obstore default).
* ``ATLAN_OBSTORE_HTTP2_KEEP_ALIVE_TIMEOUT`` — H2 keep-alive ack timeout
  (default ``"30s"``).
* ``ATLAN_OBSTORE_USER_AGENT`` — custom UA string.  Defaults to
  ``"atlan-application-sdk/{version}"`` so tenant operators can identify
  SDK traffic in S3 access logs.

Retry config (passed via ``S3Store(retry_config=…)``):

* ``ATLAN_OBSTORE_RETRY_MAX_RETRIES`` — overrides obstore's default of 10.
* ``ATLAN_OBSTORE_RETRY_TIMEOUT`` — overrides obstore's default of 3 min.

We deliberately do **not** add a Python-level retry loop on top — the Rust
layer already retries with backoff and we'd just multiply the failure time
without changing the outcome (see BLDX-1155 review thread).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # obstore TypedDicts — not importable at runtime.
    from obstore.store import ClientConfig, ObjectStore, RetryConfig

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT = "90s"
_DEFAULT_CONNECT_TIMEOUT = "30s"
_DEFAULT_POOL_IDLE_TIMEOUT = "90s"
_DEFAULT_HTTP2_KEEP_ALIVE_TIMEOUT = "30s"


def _default_user_agent() -> str:
    """Return the default ``user_agent`` string.

    Imported lazily so that ``application_sdk.version`` doesn't pull this
    module into circular-import paths during package init.
    """
    try:
        from application_sdk.version import (  # noqa: PLC0415; type: ignore[import]
            __version__,
        )

        return f"atlan-application-sdk/{__version__}"
    except Exception:  # pragma: no cover — defensive
        logger.warning(
            "Failed to derive User-Agent from version; using fallback", exc_info=True
        )
        return "atlan-application-sdk"


def obstore_client_options() -> ClientConfig:
    """Build an obstore ``ClientConfig`` from environment variables.

    Returns:
        A ``ClientConfig`` (obstore TypedDict) suitable for passing as
        ``client_options=`` to S3Store / GCSStore / AzureStore constructors.
        Always contains at least the SDK defaults — the dict is never empty.
    """
    opts: ClientConfig = {
        "timeout": os.getenv("ATLAN_OBSTORE_TIMEOUT", _DEFAULT_TIMEOUT),
        "connect_timeout": os.getenv(
            "ATLAN_OBSTORE_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT
        ),
        "pool_idle_timeout": os.getenv(
            "ATLAN_OBSTORE_POOL_IDLE_TIMEOUT", _DEFAULT_POOL_IDLE_TIMEOUT
        ),
        "http2_keep_alive_timeout": os.getenv(
            "ATLAN_OBSTORE_HTTP2_KEEP_ALIVE_TIMEOUT",
            _DEFAULT_HTTP2_KEEP_ALIVE_TIMEOUT,
        ),
        "user_agent": os.getenv("ATLAN_OBSTORE_USER_AGENT", _default_user_agent()),
    }
    pool_max = os.getenv("ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST")
    if pool_max:
        opts["pool_max_idle_per_host"] = pool_max
    return opts


def obstore_retry_config() -> RetryConfig | None:
    """Build an obstore ``RetryConfig``, or ``None`` for upstream defaults.

    Returns:
        A ``RetryConfig`` (obstore TypedDict) suitable for passing as
        ``retry_config=`` to S3Store / GCSStore / AzureStore constructors,
        or ``None`` when no overrides have been set (so we don't fight the
        upstream defaults).
    """
    from datetime import timedelta  # noqa: PLC0415

    cfg: RetryConfig = {}
    raw_max = os.getenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES")
    if raw_max:
        try:
            cfg["max_retries"] = int(raw_max)
        except ValueError:
            logger.warning(
                "Invalid ATLAN_OBSTORE_RETRY_MAX_RETRIES=%r — using obstore default",
                raw_max,
                exc_info=True,
            )

    raw_timeout = os.getenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS")
    if raw_timeout:
        try:
            cfg["retry_timeout"] = timedelta(seconds=int(raw_timeout))
        except ValueError:
            logger.warning(
                "Invalid ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS=%r — using obstore default",
                raw_timeout,
                exc_info=True,
            )

    return cfg or None


def make_s3_store(
    bucket: str,
    config: dict[str, str] | None = None,
    *,
    label: str = "s3",
    client_options: ClientConfig | None = None,
    credential_provider: object = None,
) -> ObjectStore:
    """Create an S3Store with SDK-default client/retry config.

    Both ``binding.py`` and ``cloud.py`` delegate store construction here so
    timeouts, retry budgets, and log output stay consistent across all S3 paths.
    Pass *client_options* to override the SDK defaults (e.g., for insecureSSL).
    """
    from obstore.store import S3Store  # noqa: PLC0415

    opts = client_options if client_options is not None else obstore_client_options()
    retry = obstore_retry_config()
    log_obstore_config(label, client_options=opts, retry_config=retry)
    kw: dict[str, object] = dict(
        bucket=bucket, config=config, client_options=opts, retry_config=retry
    )
    if credential_provider is not None:
        kw["credential_provider"] = credential_provider
    return S3Store(**kw)  # type: ignore[arg-type]


def make_azure_store(
    container: str,
    config: dict[str, str] | None = None,
    *,
    label: str = "azure",
    client_options: ClientConfig | None = None,
    credential_provider: object = None,
) -> ObjectStore:
    """Create an AzureStore with SDK-default client/retry config.

    See :func:`make_s3_store` for rationale.
    """
    from obstore.store import AzureStore  # noqa: PLC0415

    opts = client_options if client_options is not None else obstore_client_options()
    retry = obstore_retry_config()
    log_obstore_config(label, client_options=opts, retry_config=retry)
    kw: dict[str, object] = dict(
        container_name=container, config=config, client_options=opts, retry_config=retry
    )
    if credential_provider is not None:
        kw["credential_provider"] = credential_provider
    return AzureStore(**kw)  # type: ignore[arg-type]


def make_gcs_store(
    bucket: str,
    config: dict[str, str] | None = None,
    *,
    label: str = "gcs",
    client_options: ClientConfig | None = None,
    credential_provider: object | None = None,
) -> ObjectStore:
    """Create a GCSStore with SDK-default client/retry config.

    See :func:`make_s3_store` for rationale.

    *credential_provider* is an optional obstore credential callback, used only
    for unauthenticated emulators (fake-gcs-server) so obstore skips the
    metadata-server / OAuth token dance. Real GCS leaves it None.
    """
    from obstore.store import GCSStore  # noqa: PLC0415

    opts = client_options if client_options is not None else obstore_client_options()
    retry = obstore_retry_config()
    log_obstore_config(label, client_options=opts, retry_config=retry)
    kwargs: dict[str, object] = dict(
        bucket=bucket, config=config, client_options=opts, retry_config=retry
    )
    if credential_provider is not None:
        kwargs["credential_provider"] = credential_provider
    return GCSStore(**kwargs)  # type: ignore[arg-type]


def log_obstore_config(
    provider: str,
    *,
    client_options: ClientConfig | None,
    retry_config: RetryConfig | None,
) -> None:
    """Log the configured client/retry options once at store creation time.

    A prior RCA was wrong-footed by these values being invisible — we said
    "single attempt" when ~5–6 attempts had actually happened in the Rust
    layer at obstore's default 10×3 min retry budget. Surfacing what's
    configured up front prevents that confusion next time.
    """
    logger.info(
        "obstore configured for %s: client=%s retry=%s",
        provider,
        dict(client_options) if client_options else {},
        dict(retry_config)
        if retry_config
        else "default(max_retries=10, retry_timeout=3m)",
    )
