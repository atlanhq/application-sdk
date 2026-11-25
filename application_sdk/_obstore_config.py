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

* ``ATLAN_OBSTORE_READ_TIMEOUT`` — **progress-based** read timeout (default
  ``"90s"``). This is the primary liveness bound for large transfers: obstore
  resets it after every successful read, so a slow-but-*progressing* GB-class
  download stays alive indefinitely and only fails when no bytes arrive for
  the window. This is what keeps large downloads alive instead of relying on a
  bigger overall cap (BLDX-1513).
* ``ATLAN_OBSTORE_TIMEOUT`` — **overall-request** timeout (default ``"30m"``).
  This is the total wall-clock cap from connect to last byte, so it scales with
  file size, not throughput. It is now a generous *backstop* — ``read_timeout``
  above does the real stall detection. A prior default of ``"90s"`` killed
  ~250 MB connection-cache downloads on a slow-egress tenant even while bytes
  were still flowing (~1 MiB/s); bump this env for multi-GB single objects.
* ``ATLAN_OBSTORE_CONNECT_TIMEOUT`` — connect-phase timeout (default ``"30s"``).
* ``ATLAN_OBSTORE_POOL_IDLE_TIMEOUT`` — pool idle timeout (default ``"90s"``).
* ``ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST`` — pool size per host (unset by
  default — leaves the obstore default).
* ``ATLAN_OBSTORE_HTTP2_KEEP_ALIVE_TIMEOUT`` — H2 keep-alive ack timeout
  (default ``"30s"``).
* ``ATLAN_OBSTORE_USER_AGENT`` — custom UA string.  Defaults to
  ``"atlan-application-sdk/{version}"`` so tenant operators can identify
  SDK traffic in S3 access logs.

Proxy / TLS (obstore reads none of these itself, so we plumb them explicitly):

* ``HTTPS_PROXY``, then ``ALL_PROXY`` — obstore's ``proxy_url`` (resolved by
  :mod:`application_sdk._proxy`, the rule every SDK connection follows).
* ``NO_PROXY`` — obstore's ``proxy_excludes`` (only when a proxy is set).
* ``SSL_CERT_DIR`` — custom CA certs for private-CA stores (e.g. self-hosted
  MinIO), loaded into obstore's ``root_certificate``. Same env var the SDK's httpx
  clients and Temporal honor via ``common/ssl_utils.py``.

Retry config (passed via ``S3Store(retry_config=…)``):

* ``ATLAN_OBSTORE_RETRY_MAX_RETRIES`` — overrides obstore's default of 10.
* ``ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS`` — overrides obstore's default of 3 min
  (integer seconds).

We deliberately do **not** add a Python-level retry loop on top — the Rust
layer already retries with backoff and we'd just multiply the failure time
without changing the outcome (see BLDX-1155 review thread). The one exception
is ``ops.upload_file``'s retry of the blobstorage gateway's transient
``404 "no Route matched"``: the Rust layer never retries a 404, so that loop
adds a retry where there was none rather than multiplying one.
"""

from __future__ import annotations

import contextlib
import os
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlsplit, urlunsplit

if TYPE_CHECKING:
    # obstore TypedDicts — not importable at runtime.
    from obstore.store import ClientConfig, ObjectStore, RetryConfig

from application_sdk._deprecation import deprecated
from application_sdk._env import env_duration, env_int, env_str
from application_sdk._logging import get_logger
from application_sdk._proxy import no_proxy, proxy_for

logger = get_logger(__name__)


def _report(message: str) -> None:
    """Log a rejected env value the way :mod:`application_sdk.common._env` does.

    This module is in the API host's core install, which does not carry
    ``application_sdk.common``, so it reads through :mod:`application_sdk._env`
    and reports to its own logger.
    """
    logger.warning("Ignoring env var: %s", message)


def _duration(key: str, default: str) -> str:
    return env_duration(key, default, report=_report)


# Overall-request cap: from connect to last byte, so it scales with file size,
# not throughput. Kept generous — ``read_timeout`` below is the real liveness
# bound. See BLDX-1513: a 90s value here killed slow-but-progressing GB-class
# downloads even while bytes were flowing.
_DEFAULT_TIMEOUT = "30m"
# Progress-based read timeout: resets after every successful read, so it detects
# a genuinely *stalled* connection (no bytes for the window) without penalising a
# transfer that is merely slow. This is what keeps large downloads alive.
_DEFAULT_READ_TIMEOUT = "90s"
_DEFAULT_CONNECT_TIMEOUT = "30s"
_DEFAULT_POOL_IDLE_TIMEOUT = "90s"
_DEFAULT_HTTP2_KEEP_ALIVE_TIMEOUT = "30s"


def _default_user_agent() -> str:
    """Return the default ``user_agent`` string.

    Imported lazily so that ``application_sdk.version`` doesn't pull this
    module into circular-import paths during package init.
    """
    try:
        from application_sdk.version import (  # noqa: PLC0415
            __version__,
        )

        return f"atlan-application-sdk/{__version__}"
    except Exception:  # pragma: no cover — defensive
        logger.warning(
            "Failed to derive User-Agent from version; using fallback", exc_info=True
        )
        return "atlan-application-sdk"


def _obstore_proxy_url() -> str | None:
    """Return the proxy URL for object-store traffic, or None.

    obstore doesn't read proxy env vars itself, so we pass it explicitly.
    Object stores are reached over HTTPS, so this is the ``https`` proxy
    (``HTTPS_PROXY``, then ``ALL_PROXY``); there is no fallback to
    ``HTTP_PROXY``. No host is given: obstore applies ``NO_PROXY`` itself.
    """
    return proxy_for("https")


def _obstore_proxy_excludes() -> str | None:
    """Return NO_PROXY (obstore's ``proxy_excludes``), or None.

    Unlike Temporal — which knows its single target host and passes it to
    ``_proxy.proxy_for`` — obstore matches NO_PROXY per-host
    at request time, so we just hand it the raw list.
    """
    return no_proxy()


def obstore_client_options() -> ClientConfig:
    """Build an obstore ``ClientConfig`` from environment variables.

    Returns:
        A ``ClientConfig`` (obstore TypedDict) suitable for passing as
        ``client_options=`` to S3Store / GCSStore / AzureStore constructors.
        Always contains at least the SDK defaults — the dict is never empty.
    """
    # Unset or empty keeps the default, and a value obstore cannot parse as a
    # duration warns and keeps it too: passed through, either one fails every
    # store construction with "failed to parse ... as Duration".
    opts: ClientConfig = {
        "timeout": _duration("ATLAN_OBSTORE_TIMEOUT", _DEFAULT_TIMEOUT),
        "read_timeout": _duration("ATLAN_OBSTORE_READ_TIMEOUT", _DEFAULT_READ_TIMEOUT),
        "connect_timeout": _duration(
            "ATLAN_OBSTORE_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT
        ),
        "pool_idle_timeout": _duration(
            "ATLAN_OBSTORE_POOL_IDLE_TIMEOUT", _DEFAULT_POOL_IDLE_TIMEOUT
        ),
        "http2_keep_alive_timeout": _duration(
            "ATLAN_OBSTORE_HTTP2_KEEP_ALIVE_TIMEOUT",
            _DEFAULT_HTTP2_KEEP_ALIVE_TIMEOUT,
        ),
        "user_agent": env_str("ATLAN_OBSTORE_USER_AGENT", _default_user_agent()),
    }
    pool_max = os.getenv("ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST")
    if pool_max:
        opts["pool_max_idle_per_host"] = pool_max
    proxy_url = _obstore_proxy_url()
    if proxy_url:
        opts["proxy_url"] = proxy_url
        proxy_excludes = _obstore_proxy_excludes()
        if proxy_excludes:
            opts["proxy_excludes"] = proxy_excludes

    from application_sdk.common.ssl_utils import (  # noqa: PLC0415
        get_custom_ca_cert_bytes,
    )

    root_certificate = get_custom_ca_cert_bytes()
    if root_certificate:
        opts["root_certificate"] = root_certificate
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
    # Unset means obstore's own default. A malformed or negative value warns
    # and also keeps obstore's default.
    max_retries = env_int(
        "ATLAN_OBSTORE_RETRY_MAX_RETRIES", None, minimum=0, report=_report
    )
    if max_retries is not None:
        cfg["max_retries"] = max_retries

    retry_timeout = env_int(
        "ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", None, minimum=0, report=_report
    )
    if retry_timeout is not None:
        cfg["retry_timeout"] = timedelta(seconds=retry_timeout)

    return cfg or None


# ---------------------------------------------------------------------------
# Store settings: the one shape every front-end translates into
# ---------------------------------------------------------------------------
#
# Three front-ends build stores from credentials: the Dapr binding
# (``storage/binding.py``), ``CloudStore`` (``storage/cloud.py``) and the API
# host (``handler/object_routes.py``). Each reads its own input format and
# fills one of the settings dataclasses below; ``build_*_store`` is the only
# code that turns settings into obstore ``config`` / ``client_options``. A fix
# to that translation (an endpoint rule, a header a store rejects, a credential
# key) therefore reaches every front-end at once.
#
# What stays with each front-end is what depends on its input: field names,
# defaults (e.g. where a blank region comes from), auth-mode precedence and the
# warnings that name the front-end's own fields.


def endpoint_is_aws(endpoint: str) -> bool:
    """Return True when *endpoint* points to real AWS (or is empty).

    Handles every scheme form an endpoint may carry:
    * ``https://s3.amazonaws.com`` — standard AWS public endpoint
    * ``http://...`` — e.g. LocalStack over plain HTTP
    * scheme-less ``host:port`` — e.g. ``minio.local:9000``
    * SDK-internal URI forms: ``s3://``, ``objectstore://``, ``adls://``

    Any endpoint whose hostname ends in ``.amazonaws.com`` or
    ``.amazonaws.com.cn`` is treated as AWS. An absent/empty endpoint means
    the caller is relying on the default AWS endpoint — also AWS. Everything
    else (MinIO, Ceph, R2, B2, GCS-S3-interop, custom CNAMEs) is non-AWS.
    """
    if not endpoint:
        return True  # no custom endpoint → real AWS
    # Ensure urlparse can extract the hostname regardless of scheme.
    normalised = endpoint if "//" in endpoint else f"//{endpoint}"
    host = (urlparse(normalised).hostname or "").rstrip(".")
    return host.endswith((".amazonaws.com", ".amazonaws.com.cn"))


@dataclass(frozen=True)
class S3StoreSettings:
    """Everything an ``S3Store`` is built from, whichever front-end read it.

    Empty strings mean "not set": obstore's default applies. Credential fields
    are excluded from ``repr`` so a logged settings object carries no secret.
    """

    bucket: str
    region: str = ""
    endpoint: str = ""
    """Custom endpoint (MinIO, R2, GCS interop, ...). An ``http://`` endpoint
    turns on ``allow_http``; a non-AWS endpoint turns off object tagging unless
    *disable_tagging* says otherwise."""
    allow_http: bool = False
    allow_invalid_certificates: bool = False
    force_path_style: bool = False
    disable_tagging: bool | None = None
    """``None`` auto-detects: tagging is disabled on any non-AWS endpoint,
    because many S3-compatible stores reject the ``x-amz-tagging`` header."""
    user_agent: str = ""
    """Overrides the SDK's default ``User-Agent`` when set."""
    access_key_id: str = field(default="", repr=False)
    secret_access_key: str = field(default="", repr=False)
    session_token: str = field(default="", repr=False)
    """Sent with static keys, so temporary (STS) credentials work. Static keys
    are ignored when *credential_provider* is set: the provider owns auth."""
    conditional_put: str = ""
    copy_if_not_exists: str = ""
    checksum_algorithm: str = ""
    server_side_encryption: str = ""
    sse_kms_key_id: str = ""
    credential_provider: object = field(default=None, repr=False, compare=False)


def s3_store_args(
    settings: S3StoreSettings, client_options: ClientConfig
) -> tuple[dict[str, str] | None, ClientConfig]:
    """Translate *settings* into obstore S3 ``(config, client_options)``.

    *client_options* is the base (SDK defaults, or a caller override) and is
    not mutated. ``config`` is ``None`` when nothing is set, so obstore falls
    back to its own credential chain.
    """
    config: dict[str, str] = {}
    opts: dict[str, object] = dict(client_options)
    if settings.region:
        config["aws_region"] = settings.region
    endpoint = settings.endpoint
    if endpoint:
        config["aws_endpoint"] = endpoint
        # obstore's reqwest client is https-only by default and rejects an
        # http:// endpoint at request-build time ("HTTP error: builder error").
        if endpoint.lower().startswith("http://"):
            opts["allow_http"] = True
    if settings.allow_http:
        opts["allow_http"] = True
    if settings.allow_invalid_certificates:
        opts["allow_invalid_certificates"] = True
    if settings.user_agent:
        opts["user_agent"] = settings.user_agent
    if settings.force_path_style:
        config["aws_virtual_hosted_style_request"] = "false"

    # obstore sends x-amz-tagging by default; many S3-compatible stores (B2,
    # R2, GCS-S3-interop) hard-reject the header. Never log the endpoint: on
    # the secure k8s path it can come from the secret store.
    if settings.disable_tagging is None:
        if not endpoint_is_aws(endpoint):
            config["aws_disable_tagging"] = "true"
    elif settings.disable_tagging:
        config["aws_disable_tagging"] = "true"
    elif not endpoint_is_aws(endpoint):
        logger.warning(
            "Object tagging explicitly enabled on a non-AWS endpoint "
            "(endpoint_configured=%s) — obstore will send x-amz-tagging, which "
            "many S3-compatible stores (B2, R2, GCS-interop) reject",
            bool(endpoint),
        )

    for key, value in (
        ("aws_conditional_put", settings.conditional_put),
        ("aws_copy_if_not_exists", settings.copy_if_not_exists),
        ("aws_checksum_algorithm", settings.checksum_algorithm),
        ("aws_server_side_encryption", settings.server_side_encryption),
        ("aws_sse_kms_key_id", settings.sse_kms_key_id),
    ):
        if value:
            config[key] = value

    if (
        settings.credential_provider is None
        and settings.access_key_id
        and settings.secret_access_key
    ):
        config["aws_access_key_id"] = settings.access_key_id
        config["aws_secret_access_key"] = settings.secret_access_key
        if settings.session_token:
            config["aws_session_token"] = settings.session_token

    return (config or None), opts  # pyright: ignore[reportReturnType]


def _new_s3_store(
    bucket: str,
    config: dict[str, str] | None,
    client_options: ClientConfig,
    credential_provider: object,
    label: str,
) -> ObjectStore:
    """Construct the S3Store with the SDK retry config, logging what is set."""
    from obstore.store import S3Store  # noqa: PLC0415

    retry = obstore_retry_config()
    log_obstore_config(label, client_options=client_options, retry_config=retry)
    kw: dict[str, object] = dict(
        bucket=bucket, config=config, client_options=client_options, retry_config=retry
    )
    if credential_provider is not None:
        kw["credential_provider"] = credential_provider
    return S3Store(**kw)  # pyright: ignore[reportArgumentType]


def build_s3_store(
    settings: S3StoreSettings,
    *,
    label: str = "s3",
    client_options: ClientConfig | None = None,
) -> ObjectStore:
    """Create an S3Store from *settings* with SDK-default client/retry config.

    Every front-end (Dapr binding, ``CloudStore``, the API host) builds its
    store here, so timeouts, retry budgets, the endpoint / tagging rules and
    log output stay consistent across all S3 paths. Pass *client_options* to
    replace the SDK default client options before *settings* adjust them.
    """
    base = client_options if client_options is not None else obstore_client_options()
    config, opts = s3_store_args(settings, base)
    return _new_s3_store(
        settings.bucket, config, opts, settings.credential_provider, label
    )


@deprecated(
    "make_s3_store is deprecated; use build_s3_store with an S3StoreSettings, "
    "which applies the shared endpoint, tagging and credential rules — will be "
    "removed in v4.0.0."
)
def make_s3_store(
    bucket: str,
    config: dict[str, str] | None = None,
    *,
    label: str = "s3",
    client_options: ClientConfig | None = None,
    credential_provider: object = None,
) -> ObjectStore:
    """Create an S3Store from a raw obstore *config*.

    .. deprecated:: 3.60
        Use :func:`build_s3_store`. Will be removed in v4.0.0.
    """
    opts = client_options if client_options is not None else obstore_client_options()
    return _new_s3_store(bucket, config, opts, credential_provider, label)


# obstore-rs reads ``AZURE_*`` from the environment at AzureStore construction
# *unconditionally*, and its credential precedence (account key > SAS > client secret >
# workload identity) lets a value injected into the host environment outrank the
# credential the caller passed in ``config``. On an Azure-hosted node the pod carries
# its own identity in ``AZURE_STORAGE_ACCESS_KEY`` and in ``AZURE_FEDERATED_TOKEN_FILE``
# + ``AZURE_CLIENT_ID``; either silently overrides an explicit caller credential and
# signs requests as the wrong principal (403 AuthenticationFailed / 401 AADSTS70025).
# So when the caller supplies a standalone credential in ``config`` we construct the
# store with all ``AZURE_*`` stripped from the environment, leaving obstore to resolve
# from our ``config`` alone. Callers that pass no credential (managed / workload
# identity) are left untouched so the ambient identity still applies.
#
# TODO: retire this env-mutation workaround once obstore-rs honours explicit ``config``
# credentials with higher precedence than ambient ``AZURE_*`` env vars.  File an issue
# at https://github.com/developmentseed/obstore/issues and replace the placeholder
# below with the specific issue URL — the workaround must not be deleted until that
# gate is closed.
# Upstream issue: <not yet filed>
_AZURE_CREDENTIAL_CONFIG_KEYS = (
    "azure_storage_account_key",
    "azure_storage_client_secret",
    "azure_storage_sas_key",
    "azure_storage_token",
)

# os.environ is process-global, so serialize make_azure_store callers: an isolated build
# (env temporarily stripped) must never overlap a concurrent build that relies on the
# ambient AZURE_* env.  Note: this lock only protects callers of make_azure_store — other
# in-process readers (azure-identity, OpenTelemetry exporters, subprocesses spawned during
# the window, sidecar SDKs) can still observe the stripped env during the brief window.
_azure_store_build_lock = threading.Lock()


def _azure_config_has_explicit_credential(config: dict[str, str] | None) -> bool:
    """True if *config* carries a standalone Azure credential (account key / client
    secret / SAS / bearer token) — i.e. the caller wants that credential used, not the
    host's ambient identity."""
    return bool(config) and any(config.get(k) for k in _AZURE_CREDENTIAL_CONFIG_KEYS)


@contextlib.contextmanager
def _suppress_ambient_azure_env(active: bool):
    """Temporarily drop all ambient ``AZURE_*`` env vars when *active*.

    Scoped to the synchronous AzureStore constructor (where obstore reads the
    environment). Serialized by ``_azure_store_build_lock`` so an isolated build can't
    strip the environment out from under a concurrent non-isolated build.
    """
    with _azure_store_build_lock:
        if not active:
            yield
            return
        saved = {
            k: os.environ.pop(k) for k in list(os.environ) if k.startswith("AZURE_")
        }
        if saved:
            logger.debug(
                "make_azure_store: suppressed %d ambient AZURE_* env var(s)", len(saved)
            )
        try:
            yield
        finally:
            os.environ.update(saved)


@dataclass(frozen=True)
class AzureStoreSettings:
    """Everything an ``AzureStore`` is built from, whichever front-end read it.

    The front-end picks one auth mode and sets only its fields; this class
    maps fields to obstore keys and does not arbitrate between modes.
    Credential fields are excluded from ``repr``.
    """

    container: str
    account_name: str = ""
    endpoint: str = ""
    """Custom endpoint (Azurite, private link, ...). ``http://`` turns on
    ``allow_http``."""
    use_emulator: bool = False
    authority_host: str = ""
    account_key: str = field(default="", repr=False)
    sas_key: str = field(default="", repr=False)
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    federated_token_file: str = ""
    msi_endpoint: str = ""
    msi_resource_id: str = ""
    credential_provider: object = field(default=None, repr=False, compare=False)


def azure_store_args(
    settings: AzureStoreSettings, client_options: ClientConfig
) -> tuple[dict[str, str] | None, ClientConfig]:
    """Translate *settings* into obstore Azure ``(config, client_options)``."""
    config: dict[str, str] = {}
    opts: dict[str, object] = dict(client_options)
    if settings.endpoint:
        config["azure_storage_endpoint"] = settings.endpoint
        if settings.endpoint.lower().startswith("http://"):
            opts["allow_http"] = True
    if settings.use_emulator:
        config["azure_storage_use_emulator"] = "true"
        opts["allow_http"] = True
    for key, value in (
        ("azure_storage_account_name", settings.account_name),
        ("azure_storage_authority_host", settings.authority_host),
        ("azure_storage_account_key", settings.account_key),
        ("azure_storage_sas_key", settings.sas_key),
        ("azure_storage_tenant_id", settings.tenant_id),
        ("azure_storage_client_id", settings.client_id),
        ("azure_storage_client_secret", settings.client_secret),
        ("azure_storage_federated_token_file", settings.federated_token_file),
        ("azure_storage_msi_endpoint", settings.msi_endpoint),
        ("azure_storage_msi_resource_id", settings.msi_resource_id),
    ):
        if value:
            config[key] = value
    return (config or None), opts  # pyright: ignore[reportReturnType]


def _new_azure_store(
    container: str,
    config: dict[str, str] | None,
    client_options: ClientConfig,
    credential_provider: object,
    label: str,
) -> ObjectStore:
    """Construct the AzureStore, isolated from ambient ``AZURE_*`` when the
    caller supplies its own credential (see ``_suppress_ambient_azure_env``)."""
    from obstore.store import AzureStore  # noqa: PLC0415

    retry = obstore_retry_config()
    log_obstore_config(label, client_options=client_options, retry_config=retry)
    kw: dict[str, object] = dict(
        container_name=container,
        config=config,
        client_options=client_options,
        retry_config=retry,
    )
    if credential_provider is not None:
        kw["credential_provider"] = credential_provider
    isolate = (
        _azure_config_has_explicit_credential(config) or credential_provider is not None
    )
    with _suppress_ambient_azure_env(isolate):
        return AzureStore(**kw)  # pyright: ignore[reportArgumentType]


def build_azure_store(
    settings: AzureStoreSettings,
    *,
    label: str = "azure",
    client_options: ClientConfig | None = None,
) -> ObjectStore:
    """Create an AzureStore from *settings* with SDK-default client/retry config.

    See :func:`build_s3_store` for rationale. When the settings carry an
    explicit credential *or* a ``credential_provider``, the ambient
    ``AZURE_*`` environment is suppressed for the construction so the host's
    own identity can't override it (see ``_suppress_ambient_azure_env``). The
    cert-auth path in ``binding.py`` uses ``credential_provider`` instead of
    a key in ``config``, so both cases activate env stripping.
    """
    base = client_options if client_options is not None else obstore_client_options()
    config, opts = azure_store_args(settings, base)
    return _new_azure_store(
        settings.container, config, opts, settings.credential_provider, label
    )


@deprecated(
    "make_azure_store is deprecated; use build_azure_store with an "
    "AzureStoreSettings, which applies the shared endpoint and credential "
    "rules — will be removed in v4.0.0."
)
def make_azure_store(
    container: str,
    config: dict[str, str] | None = None,
    *,
    label: str = "azure",
    client_options: ClientConfig | None = None,
    credential_provider: object = None,
) -> ObjectStore:
    """Create an AzureStore from a raw obstore *config*.

    .. deprecated:: 3.60
        Use :func:`build_azure_store`. Will be removed in v4.0.0.
    """
    opts = client_options if client_options is not None else obstore_client_options()
    return _new_azure_store(container, config, opts, credential_provider, label)


@dataclass(frozen=True)
class GCSStoreSettings:
    """Everything a ``GCSStore`` is built from, whichever front-end read it.

    With neither a service-account key nor a credential provider, obstore
    resolves Application Default Credentials itself.
    """

    bucket: str
    service_account_key: str = field(default="", repr=False)
    """Service-account JSON, as text."""
    credential_provider: object = field(default=None, repr=False, compare=False)


def _new_gcs_store(
    bucket: str,
    config: dict[str, str] | None,
    client_options: ClientConfig,
    credential_provider: object,
    label: str,
) -> ObjectStore:
    """Construct the GCSStore with the SDK retry config, logging what is set."""
    from obstore.store import GCSStore  # noqa: PLC0415

    retry = obstore_retry_config()
    log_obstore_config(label, client_options=client_options, retry_config=retry)
    kw: dict[str, object] = dict(
        bucket=bucket, config=config, client_options=client_options, retry_config=retry
    )
    if credential_provider is not None:
        kw["credential_provider"] = credential_provider
    return GCSStore(**kw)  # pyright: ignore[reportArgumentType]


def build_gcs_store(
    settings: GCSStoreSettings,
    *,
    label: str = "gcs",
    client_options: ClientConfig | None = None,
) -> ObjectStore:
    """Create a GCSStore from *settings* with SDK-default client/retry config.

    See :func:`build_s3_store` for rationale. On the ADC / Workload-Identity
    path ``binding.py`` passes a ``credential_provider`` (obstore.auth.google,
    backed by google-auth) instead of a key: obstore's built-in GCS
    credential-file decoder only understands ``service_account`` /
    ``authorized_user`` files, NOT a Workload Identity Federation
    ``external_account`` file — google-auth handles all of them.
    """
    opts = client_options if client_options is not None else obstore_client_options()
    # ``None`` (not {}) so obstore uses Application Default Credentials.
    config = (
        {"google_service_account_key": settings.service_account_key}
        if settings.service_account_key
        else None
    )
    return _new_gcs_store(
        settings.bucket, config, opts, settings.credential_provider, label
    )


@deprecated(
    "make_gcs_store is deprecated; use build_gcs_store with a GCSStoreSettings "
    "— will be removed in v4.0.0."
)
def make_gcs_store(
    bucket: str,
    config: dict[str, str] | None = None,
    *,
    label: str = "gcs",
    client_options: ClientConfig | None = None,
    credential_provider: object = None,
) -> ObjectStore:
    """Create a GCSStore from a raw obstore *config*.

    .. deprecated:: 3.60
        Use :func:`build_gcs_store`. Will be removed in v4.0.0.
    """
    opts = client_options if client_options is not None else obstore_client_options()
    return _new_gcs_store(bucket, config, opts, credential_provider, label)


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
        _redact_proxy_userinfo(client_options) if client_options else {},
        dict(retry_config)
        if retry_config
        else "default(max_retries=10, retry_timeout=3m)",
    )


def _redact_proxy_userinfo(client_options: ClientConfig) -> dict:
    """Return a log-safe copy of *client_options* (the real config is untouched).

    Masks ``proxy_url`` credentials and shrinks ``root_certificate`` to a byte
    count, so logs never carry a proxy password or a full PEM bundle.
    """
    opts = dict(client_options)
    proxy = opts.get("proxy_url")
    if isinstance(proxy, str):
        p = urlsplit(proxy)
        if p.username or p.password:
            host = p.hostname or ""
            netloc = f"***@{host}:{p.port}" if p.port else f"***@{host}"
            opts["proxy_url"] = urlunsplit(
                (p.scheme, netloc, p.path, p.query, p.fragment)
            )
    root_cert = opts.get("root_certificate")
    if isinstance(root_cert, (bytes, str)):
        opts["root_certificate"] = f"<{len(root_cert)} bytes>"
    return opts
