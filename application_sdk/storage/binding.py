"""Parse Dapr component YAML files and create obstore stores.

Supports the following Dapr binding types:

    bindings.localstorage / bindings.local.storage → LocalStore
    bindings.aws.s3 / bindings.s3                  → S3Store
    bindings.azure.blobstorage                      → AzureStore
    bindings.gcp.bucket / bindings.gcs              → GCSStore

Dapr metadata fields that have no obstore equivalent are silently ignored:
  decodeBase64, encodeBase64 (S3/GCS/Azure — Dapr-layer payload transforms)
  contentType (GCS)
  publicAccessLevel, disableEntityManagement, getBlobRetryCount (Azure)

``storageClass`` is applied as a per-request put attribute on every write via
the infrastructure context, for all supported cloud backends:

  S3    → ``Storage-Class: <value>``         (e.g. STANDARD_IA, GLACIER_IR)
  Azure → ``x-ms-access-tier: <value>``      (e.g. Hot, Cool, Cold, Archive)
  GCS   → ``X-Goog-Storage-Class: <value>``  (e.g. NEARLINE, COLDLINE, ARCHIVE)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from obstore.store import ObjectStore

# Matches un-substituted Helm / Mustache template placeholders (e.g. {{tenant}}).
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}")

# GCP service account JSON fields injected from Kubernetes secret via Helm.
GCS_SERVICE_ACCOUNT_FIELDS: tuple[str, ...] = (
    "type",
    "project_id",
    "private_key_id",
    "private_key",
    "client_email",
    "client_id",
    "auth_uri",
    "token_uri",
    "auth_provider_x509_cert_url",
    "client_x509_cert_url",
    "universe_domain",
)

# Map Dapr binding type strings to store kind tokens.
BINDING_TYPE_MAP: dict[str, str] = {
    "bindings.localstorage": "local",
    "bindings.local.storage": "local",
    "bindings.aws.s3": "s3",
    "bindings.s3": "s3",
    "bindings.azure.blobstorage": "azure",
    "bindings.gcp.bucket": "gcs",
    "bindings.gcs": "gcs",
}

# Dapr azureEnvironment enum → obstore / Azure authority host URLs.
_AZURE_AUTHORITY_HOSTS: dict[str, str] = {
    "AzurePublicCloud": "https://login.microsoftonline.com",
    "AzureChinaCloud": "https://login.chinacloudapi.cn",
    "AzureUSGovernmentCloud": "https://login.microsoftonline.us",
    "AzureGermanCloud": "https://login.microsoftonline.de",
}

# Lazy logger — deferred to break circular import (observability ↔ storage).
_logger = None


def _get_logger():
    global _logger
    if _logger is None:
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415
            get_logger,
        )

        _logger = get_logger(__name__)
    return _logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nonempty(meta: dict[str, str], *keys: str) -> str:
    """Return the first non-empty string value for any of *keys*, or ''."""
    for k in keys:
        v = meta.get(k, "")
        if v:
            return v
    return ""


def _coerce_bool(value: str) -> bool:
    """Coerce a Dapr metadata bool string to Python bool (case-insensitive)."""
    return value.strip().lower() in {"true", "1", "yes"}


def _endpoint_is_aws(endpoint: str) -> bool:
    """Return True when *endpoint* points to real AWS (or the empty string).

    Handles every scheme form an endpoint may carry:
    * ``https://s3.amazonaws.com`` — standard AWS public endpoint
    * ``http://...`` — e.g. LocalStack over plain HTTP
    * scheme-less ``host:port`` — e.g. ``minio.local:9000``
    * SDK-internal URI forms: ``s3://``, ``objectstore://``, ``adls://``

    Any endpoint whose hostname ends in ``.amazonaws.com`` or
    ``.amazonaws.com.cn`` is treated as AWS. An absent/empty endpoint means
    the caller is relying on the default AWS credential chain — also AWS.
    Everything else (MinIO, Ceph, R2, B2, GCS-S3-interop, custom CNAMEs) is
    treated as non-AWS and obstore's default object-tagging will be disabled.
    """
    if not endpoint:
        return True  # no custom endpoint → real AWS
    # Ensure urlparse can extract the hostname regardless of scheme.
    normalised = endpoint if "//" in endpoint else f"//{endpoint}"
    host = (urlparse(normalised).hostname or "").rstrip(".")
    return host.endswith((".amazonaws.com", ".amazonaws.com.cn"))


def _resolve_metadata_value(item: dict) -> str:
    """Resolve a single Dapr metadata item to its string value.

    Supports plain ``value`` fields and ``secretKeyRef`` references.
    For ``secretKeyRef``, the secret is resolved from environment variables
    using the ref's ``key`` (falling back to ``name``).  This mirrors the
    behaviour of the ``secretstores.local.env`` Dapr component used in
    Docker Compose / SDR deployments where secrets are injected as env vars.
    """
    if "value" in item:
        return str(item["value"])

    secret_ref = item.get("secretKeyRef")
    if secret_ref:
        env_key = secret_ref.get("key") or secret_ref.get("name", "")
        if env_key:
            return os.environ.get(env_key, "")

    return ""


def _parse_dapr_metadata(metadata_list: list[dict[str, str]]) -> dict[str, str]:
    """Convert Dapr metadata list format to a flat dict.

    Handles both plain ``value`` entries and ``secretKeyRef`` entries
    (resolved via environment variables).
    """
    return {
        item["name"]: _resolve_metadata_value(item) for item in (metadata_list or [])
    }


def _find_broken_metadata_fields(metadata_list: list[dict]) -> list[str]:
    """Return names of metadata fields that clearly cannot be resolved.

    A field is considered broken when:
    - Its ``value`` contains an un-substituted template placeholder (e.g. ``{{tenant}}``)
    - It has a ``secretKeyRef`` whose referenced env var is not set
    """
    broken: list[str] = []
    for item in metadata_list or []:
        field_name = item.get("name", "<unknown>")
        if "value" in item:
            if _TEMPLATE_PLACEHOLDER_RE.search(str(item["value"])):
                broken.append(field_name)
        elif "secretKeyRef" in item:
            ref = item["secretKeyRef"]
            env_key = ref.get("key") or ref.get("name", "")
            if env_key and os.environ.get(env_key) is None:
                broken.append(field_name)
    return broken


def is_binding_configured(
    name: str,
    *,
    components_dir: Path | str = Path("./components"),
) -> bool:
    """Return True if a usable Dapr component named *name* exists.

    Parses component YAMLs without constructing a store object — a lightweight
    alternative to ``create_store_from_binding_optional(...) is not None`` when
    you only need to detect binding presence without the cost of initialising
    cloud SDK clients.

    Returns False when the component is absent, has an unsupported binding
    type, or has unresolvable metadata (template placeholders / missing env
    vars for secretKeyRef entries).
    """
    import yaml  # noqa: PLC0415 — defensive: keep inline

    components_path = Path(components_dir)
    for yaml_file in sorted(components_path.glob("*.yaml")):
        with yaml_file.open() as fh:
            doc = yaml.safe_load(fh)
        if (
            doc
            and doc.get("kind") == "Component"
            and doc.get("metadata", {}).get("name") == name
        ):
            spec = doc.get("spec", {})
            binding_type = spec.get("type", "")
            store_kind = BINDING_TYPE_MAP.get(binding_type)
            if store_kind is None:
                return False
            if store_kind != "local":
                raw_metadata = spec.get("metadata", [])
                if _find_broken_metadata_fields(raw_metadata):
                    return False
            return True
    return False


def _build_s3_config(
    name: str,
    meta: dict[str, str],
    sdk_client_options: dict[str, object],
) -> tuple[str, dict[str, str], dict[str, object], object, dict[str, str] | None]:
    """Translate S3 Dapr binding metadata into obstore S3Store constructor args.

    Returns *(bucket, config, client_options, credential_provider, put_attributes)*.

    *put_attributes* is a ``{"Storage-Class": "<class>"}`` dict when
    ``storageClass`` is set in the binding metadata, or ``None`` otherwise.
    The caller should propagate it to the infrastructure context so every
    write through that store automatically receives the correct storage class
    as an obstore put attribute (requires obstore ≥ 0.10.0).
    """
    from application_sdk.storage.errors import StorageConfigError  # noqa: PLC0415

    bucket = meta.get("bucket", "")
    config: dict[str, str] = {}
    client_options: dict[str, object] = dict(sdk_client_options)
    credential_provider = None

    # Region: explicit metadata wins; otherwise fall back to the standard AWS
    # env vars. Dapr's Go binding leaves region resolution to the AWS SDK
    # (LoadDefaultConfig → AWS_REGION / AWS_DEFAULT_REGION / shared config /
    # instance metadata), so ambient region (IRSA / EKS injection) worked in v2.
    # obstore reads neither env var with an explicit config and silently defaults
    # to us-east-1 → 301 PermanentRedirect for buckets in other regions.
    resolved_region = (
        _nonempty(meta, "region")
        or os.environ.get("AWS_REGION", "")
        or os.environ.get("AWS_DEFAULT_REGION", "")
    )
    if resolved_region:
        config["aws_region"] = resolved_region
    endpoint = _nonempty(meta, "endpoint")
    if endpoint:
        config["aws_endpoint"] = endpoint
        client_options["user_agent"] = "aws-sdk-go-v2 atlan-application-sdk"
        # Match Dapr (and the Azure branch below): an http:// endpoint means
        # plaintext, so infer allow_http from the scheme. obstore's reqwest
        # client is https-only by default and rejects http at request-build time
        # ("HTTP error: builder error") otherwise. An explicit disableSSL below
        # still works and is now redundant for http endpoints.
        if endpoint.startswith("http://"):
            client_options["allow_http"] = True
    if _coerce_bool(meta.get("forcePathStyle", "")):
        config["aws_virtual_hosted_style_request"] = "false"
    if _coerce_bool(meta.get("disableSSL", "")):
        client_options["allow_http"] = True
    if _coerce_bool(meta.get("insecureSSL", "")):
        client_options["allow_invalid_certificates"] = True
        _get_logger().warning(
            "insecureSSL=true disables certificate verification for S3 binding '%s'",
            name,
        )

    # --- Object tagging ---------------------------------------------------
    # obstore sends x-amz-tagging by default; many S3-compatible stores (B2,
    # R2, GCS-S3-interop) hard-reject the header.  Auto-disable it for any
    # non-AWS endpoint.  An explicit disableTagging field always takes precedence.
    if _nonempty(meta, "disableTagging"):
        if _coerce_bool(meta["disableTagging"]):
            config["aws_disable_tagging"] = "true"
        elif not _endpoint_is_aws(endpoint):
            _get_logger().warning(
                "disableTagging=false on non-AWS endpoint '%s' — obstore will send "
                "x-amz-tagging which many S3-compatible stores (B2, R2, GCS-interop) "
                "reject; set disableTagging=true to suppress the header",
                endpoint or "(none)",
            )
    elif not _endpoint_is_aws(endpoint):
        config["aws_disable_tagging"] = "true"

    # --- Optional pass-through overrides ----------------------------------
    # These fields are new additions; absent fields leave the obstore default
    # unchanged so existing customer configs are unaffected.
    if _nonempty(meta, "conditionalPut"):
        config["aws_conditional_put"] = meta["conditionalPut"]
    if _nonempty(meta, "copyIfNotExists"):
        config["aws_copy_if_not_exists"] = meta["copyIfNotExists"]
    if _nonempty(meta, "checksumAlgorithm"):
        config["aws_checksum_algorithm"] = meta["checksumAlgorithm"]
    if _nonempty(meta, "serverSideEncryption"):
        config["aws_server_side_encryption"] = meta["serverSideEncryption"]
    if _nonempty(meta, "sseKmsKeyId"):
        config["aws_sse_kms_key_id"] = meta["sseKmsKeyId"]

    if _nonempty(meta, "trustAnchorArn"):
        raise StorageConfigError(
            f"[{name}] IAM Roles Anywhere (trustAnchorArn) is not yet supported; "
            "use assumeRoleArn for STS or omit credentials for IRSA / instance-profile auth"
        )

    if _nonempty(meta, "assumeRoleArn"):
        from application_sdk.storage._credential_providers import (  # noqa: PLC0415
            make_s3_assume_role_provider,
        )

        base_access_key = _nonempty(meta, "accessKey") or None
        base_secret_key = _nonempty(meta, "secretKey") or None
        base_session_token = _nonempty(meta, "sessionToken") or None
        if bool(base_access_key) != bool(base_secret_key):
            _get_logger().warning(
                "S3 binding '%s': assumeRoleArn requires both accessKey and "
                "secretKey for base credentials; only one was set — "
                "accessKey, secretKey, and sessionToken all ignored",
                name,
            )
            base_access_key = None
            base_secret_key = None
            base_session_token = None

        credential_provider = make_s3_assume_role_provider(
            role_arn=meta["assumeRoleArn"],
            session_name=_nonempty(meta, "sessionName") or "atlan-application-sdk",
            region=resolved_region or None,
            base_access_key=base_access_key,
            base_secret_key=base_secret_key,
            base_session_token=base_session_token,
        )
    else:
        if _nonempty(meta, "accessKey"):
            config["aws_access_key_id"] = meta["accessKey"]
        if _nonempty(meta, "secretKey"):
            config["aws_secret_access_key"] = meta["secretKey"]
        if _nonempty(meta, "sessionToken"):
            config["aws_session_token"] = meta["sessionToken"]

    # --- Storage class (obstore ≥ 0.10.0 put attribute) -------------------
    storage_class = _nonempty(meta, "storageClass")
    put_attributes: dict[str, str] | None = (
        {"Storage-Class": storage_class} if storage_class else None
    )

    return bucket, config, client_options, credential_provider, put_attributes


def _build_azure_config(
    name: str,
    meta: dict[str, str],
    sdk_client_options: dict[str, object],
) -> tuple[str, dict[str, str], dict[str, object], object, dict[str, str] | None]:
    """Translate Azure Blob Dapr binding metadata into obstore AzureStore constructor args.

    Returns *(container, config, client_options, credential_provider, put_attributes)*.

    *put_attributes* is ``{"x-ms-access-tier": "<tier>"}`` when ``storageClass``
    is set in the binding metadata, or ``None`` otherwise.
    """
    from application_sdk.storage.errors import StorageConfigError  # noqa: PLC0415

    account = meta.get("accountName", "")
    container = meta.get("containerName", "")
    az_config: dict[str, str] = {}
    az_client_options: dict[str, object] = dict(sdk_client_options)
    az_credential_provider = None

    if account:
        az_config["azure_storage_account_name"] = account
    if _nonempty(meta, "endpoint"):
        az_config["azure_storage_endpoint"] = meta["endpoint"]
        if meta["endpoint"].startswith("http://"):
            az_client_options["allow_http"] = True
    if _coerce_bool(meta.get("useEmulator", "")):
        az_config["azure_storage_use_emulator"] = "true"
        az_client_options["allow_http"] = True

    if _nonempty(meta, "azureEnvironment"):
        env_name = meta["azureEnvironment"]
        host = _AZURE_AUTHORITY_HOSTS.get(env_name)
        if host is None:
            raise StorageConfigError(
                f"[{name}] Unknown azureEnvironment {env_name!r}. "
                f"Valid values: {', '.join(_AZURE_AUTHORITY_HOSTS)}"
            )
        az_config["azure_storage_authority_host"] = host

    tenant_id = _nonempty(meta, "azureTenantId", "tenantId")
    client_id = _nonempty(meta, "azureClientId", "clientId")
    client_secret = _nonempty(meta, "azureClientSecret")
    account_key = _nonempty(meta, "accountKey")
    sas_token = _nonempty(meta, "sasToken", "sasKey")
    cert_data = _nonempty(meta, "azureCertificate")
    cert_file = _nonempty(meta, "azureCertificateFile")
    cert_password = _nonempty(meta, "azureCertificatePassword")

    # Warn when an operator accidentally configures more than one mutually
    # exclusive auth mode — only the highest-priority one takes effect.
    # Cert auth requires tenant_id + client_id with no client_secret, so the WI
    # and MI classifiers must exclude the cert case to avoid a spurious warning.
    _has_cert = bool(cert_data or cert_file)
    _active_az_modes = sum(
        [
            bool(account_key),
            bool(sas_token),
            _has_cert,
            bool(tenant_id and client_id and client_secret),
            bool(tenant_id and client_id and not client_secret and not _has_cert),
            bool(client_id and not tenant_id and not client_secret and not _has_cert),
        ]
    )
    if _active_az_modes > 1:
        _get_logger().warning(
            "Azure binding '%s': multiple auth modes detected; "
            "using highest-priority "
            "(accountKey > sasToken > cert > service-principal > "
            "workload-identity > managed-identity)",
            name,
        )

    if account_key:
        az_config["azure_storage_account_key"] = account_key
    elif sas_token:
        az_config["azure_storage_sas_key"] = sas_token
    elif cert_data or cert_file:
        if not tenant_id or not client_id:
            raise StorageConfigError(
                f"[{name}] Azure certificate auth requires azureTenantId and azureClientId"
            )
        from application_sdk.storage._credential_providers import (  # noqa: PLC0415
            make_azure_certificate_provider,
        )

        # azureCertificate must be PEM text; for binary PFX supply
        # azureCertificateFile pointing to a file on disk instead.
        az_credential_provider = make_azure_certificate_provider(
            tenant_id=tenant_id,
            client_id=client_id,
            certificate_path=cert_file or None,
            certificate_data=cert_data.encode() if cert_data else None,
            certificate_password=cert_password or None,
            authority_host=az_config.get("azure_storage_authority_host"),
        )
    elif tenant_id and client_id and client_secret:
        az_config["azure_storage_tenant_id"] = tenant_id
        az_config["azure_storage_client_id"] = client_id
        az_config["azure_storage_client_secret"] = client_secret
    elif tenant_id and client_id:
        # AKS Workload Identity: the AAD webhook injects AZURE_FEDERATED_TOKEN_FILE;
        # obstore picks it up when tenant_id + client_id are set with no secret.
        az_config["azure_storage_tenant_id"] = tenant_id
        az_config["azure_storage_client_id"] = client_id
        if _nonempty(meta, "azureFederatedTokenFile"):
            az_config["azure_storage_federated_token_file"] = meta[
                "azureFederatedTokenFile"
            ]
    elif client_id:
        az_config["azure_storage_client_id"] = client_id

    if _nonempty(meta, "msiEndpoint"):
        az_config["azure_storage_msi_endpoint"] = meta["msiEndpoint"]
    if _nonempty(meta, "msiResourceId"):
        az_config["azure_storage_msi_resource_id"] = meta["msiResourceId"]

    # --- Storage class (Azure access tier) ------------------------------------
    storage_class = _nonempty(meta, "storageClass")
    put_attributes: dict[str, str] | None = (
        {"x-ms-access-tier": storage_class} if storage_class else None
    )

    return (
        container,
        az_config,
        az_client_options,
        az_credential_provider,
        put_attributes,
    )


def _build_gcs_config(
    meta: dict[str, str],
) -> tuple[str, dict[str, str], dict[str, str] | None]:
    """Translate GCS Dapr binding metadata into obstore GCSStore constructor args.

    Returns *(bucket, config, put_attributes)*.

    *put_attributes* is ``{"X-Goog-Storage-Class": "<class>"}`` when ``storageClass``
    is set in the binding metadata, or ``None`` otherwise.
    """
    import orjson  # noqa: PLC0415 — defensive: keep inline

    bucket = meta.get("bucket", "")
    gcs_config: dict[str, str] = {}

    # Build service-account JSON only when actual credential material is
    # supplied.  ``project_id`` alone is not credential material — daprd's
    # gcp-bucket binding schema requires ``project_id`` in component
    # metadata even on the ADC path (Workload Identity / GKE metadata
    # server / GOOGLE_APPLICATION_CREDENTIALS), so a config containing
    # only ``bucket`` + ``project_id`` must fall through to ADC rather
    # than be synthesised into a partial SA JSON that obstore would
    # reject for missing ``private_key``.
    if any(meta.get(f) for f in ("private_key", "private_key_id")):
        sa_data = {k: meta[k] for k in GCS_SERVICE_ACCOUNT_FIELDS if k in meta}
        # Normalize escaped newlines in private_key PEM blocks — Helm
        # templating from K8s secrets can produce literal two-char "\n".
        if "private_key" in sa_data:
            sa_data["private_key"] = sa_data["private_key"].replace("\\n", "\n")
        gcs_config["google_service_account_key"] = orjson.dumps(sa_data).decode()

    # --- Storage class --------------------------------------------------------
    storage_class = _nonempty(meta, "storageClass")
    put_attributes: dict[str, str] | None = (
        {"X-Goog-Storage-Class": storage_class} if storage_class else None
    )

    return bucket, gcs_config, put_attributes


def create_store_from_binding_with_put_attrs(
    name: str,
    *,
    components_dir: Path | str = Path("./components"),
) -> tuple[ObjectStore, dict[str, str] | None]:
    """Create an obstore store and any associated put attributes from a Dapr binding.

    Like :func:`create_store_from_binding` but also returns a put-attributes
    dict (e.g. ``{"Storage-Class": "STANDARD_IA"}``) that should be applied to
    every write through the returned store.  Returns ``(store, None)`` when no
    binding-level put attributes are configured.

    Use this in infrastructure-setup code (``main.py``) when you want to
    honour ``storageClass`` and similar per-write attributes without threading
    them through every call site.
    """
    store, put_attrs = _create_store_core(name, components_dir=components_dir)
    return store, put_attrs


def _create_store_core(
    name: str,
    *,
    components_dir: Path | str = Path("./components"),
) -> tuple[ObjectStore, dict[str, str] | None]:
    """Core implementation shared by the public binding-creation functions.

    Returns *(store, put_attributes)* where *put_attributes* is a dict of
    obstore put-option overrides (e.g. ``{"Storage-Class": "STANDARD_IA"}``)
    to apply on every write, or ``None`` when no overrides are needed.
    """
    import yaml  # noqa: PLC0415 — defensive: keep inline

    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageBindingBrokenError,
        StorageBindingNotFoundError,
        StorageConfigError,
    )

    components_path = Path(components_dir)
    component: dict | None = None

    for yaml_file in sorted(components_path.glob("*.yaml")):
        with yaml_file.open() as fh:
            doc = yaml.safe_load(fh)
        if (
            doc
            and doc.get("kind") == "Component"
            and doc.get("metadata", {}).get("name") == name
        ):
            component = doc
            break

    if component is None:
        raise StorageBindingNotFoundError(
            f"No Dapr component named '{name}' found in {components_path}",
            binding_name=name,
        )

    spec = component.get("spec", {})
    binding_type: str = spec.get("type", "")
    store_kind = BINDING_TYPE_MAP.get(binding_type)

    if store_kind is None:
        raise StorageConfigError(
            f"Unsupported binding type: {binding_type!r} (component={name})"
        )

    raw_metadata = spec.get("metadata", [])
    if store_kind != "local":
        broken_fields = _find_broken_metadata_fields(raw_metadata)
        if broken_fields:
            raise StorageBindingBrokenError(
                "Dapr component '%s' has unresolvable metadata "
                "(template placeholders or missing env vars): %s"
                % (name, ", ".join(broken_fields)),
                binding_name=name,
                broken_fields=broken_fields,
            )

    meta = _parse_dapr_metadata(raw_metadata)
    # Log binding resolution so objectstore routing can be verified in CI logs.
    endpoint = meta.get("endpoint", meta.get("accountName", ""))
    logger.info(
        "create_store_from_binding: name=%r type=%r store_kind=%r endpoint=%r",
        name,
        binding_type,
        store_kind,
        endpoint or "(none)",
    )

    if store_kind == "local":
        root_path = meta.get("rootPath", "./objectstore")
        from application_sdk.storage.factory import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            create_local_store,
        )

        return create_local_store(root_path), None

    # BLDX-1155: every store gets the SDK-default ClientConfig + RetryConfig so
    # timeouts and pool sizes are sized for GB-class transfers.  Store creation
    # (including logging and retry wiring) is delegated to make_*_store in
    # _obstore_config.py — the single source of truth shared with cloud.py.
    from application_sdk.storage._obstore_config import (  # noqa: PLC0415
        make_azure_store,
        make_gcs_store,
        make_s3_store,
        obstore_client_options,
    )

    sdk_client_options = obstore_client_options()

    if store_kind == "s3":
        bucket, config, client_options, credential_provider, put_attrs = (
            _build_s3_config(name, meta, sdk_client_options)
        )
        store = make_s3_store(
            bucket,
            config if config else None,
            client_options=client_options,
            credential_provider=credential_provider,
        )
        return store, put_attrs

    if store_kind == "azure":
        container, az_config, az_client_options, az_credential_provider, put_attrs = (
            _build_azure_config(name, meta, sdk_client_options)
        )
        store = make_azure_store(
            container,
            az_config if az_config else None,
            client_options=az_client_options,
            credential_provider=az_credential_provider,
        )
        return store, put_attrs

    if store_kind == "gcs":
        bucket, gcs_config, put_attrs = _build_gcs_config(meta)
        store = make_gcs_store(
            bucket,
            # Pass ``None`` (not {}) so obstore uses Application Default Credentials.
            gcs_config if gcs_config else None,
            client_options=sdk_client_options,
        )
        return store, put_attrs

    raise StorageConfigError(f"Store kind not implemented: {store_kind!r}")


def create_store_from_binding(
    name: str,
    *,
    components_dir: Path | str = Path("./components"),
) -> ObjectStore:
    """Create an obstore store from a Dapr component binding YAML file.

    Scans all ``*.yaml`` files in *components_dir* for a ``Component``
    whose ``metadata.name`` matches *name*, then creates the appropriate
    store based on ``spec.type``.

    Supported auth modes by store type:

    **S3** (``bindings.aws.s3`` / ``bindings.s3``):
    - Static access key: ``accessKey`` + ``secretKey`` (+ optional ``sessionToken``)
    - AssumeRole via STS: ``assumeRoleArn`` (+ ``sessionName``)
    - EC2 instance profile / IRSA / env vars: omit all credential fields

    **Azure Blob** (``bindings.azure.blobstorage``):
    - Account key: ``accountKey``
    - SAS token: ``sasToken`` / ``sasKey``
    - Service principal: ``azureTenantId`` + ``azureClientId`` + ``azureClientSecret``
    - AKS Workload Identity: ``azureTenantId`` + ``azureClientId`` (AZURE_FEDERATED_TOKEN_FILE injected by webhook)
    - User-assigned managed identity: ``azureClientId`` only
    - Certificate-based SP: ``azureCertificateFile`` or ``azureCertificate`` + ``azureTenantId`` + ``azureClientId``
    - System-assigned managed identity / DefaultAzureCredential: omit all credential fields

    **GCS** (``bindings.gcp.bucket`` / ``bindings.gcs``):
    - Inline SA key: any of the SA JSON fields that include ``private_key`` or ``private_key_id``
    - ADC / Workload Identity: ``bucket`` + ``project_id`` only (no private_key)

    Args:
        name: The Dapr component name (e.g. ``"objectstore"``).
        components_dir: Directory containing Dapr component YAML files.

    Returns:
        A configured obstore store instance.

    Raises:
        StorageConfigError: If no matching component is found, the
            binding type is not supported, or a required option is invalid.
    """
    store, _ = _create_store_core(name, components_dir=components_dir)
    return store


def create_store_from_binding_optional(
    name: str,
    *,
    components_dir: Path | str = Path("./components"),
) -> ObjectStore | None:
    """Create an obstore store from a Dapr component binding, or ``None`` if absent.

    Identical to :func:`create_store_from_binding` except that a missing
    component returns ``None`` instead of raising ``StorageConfigError``.
    Use this for optional bindings (e.g. ``UPSTREAM_OBJECT_STORE_NAME``) that
    are only present in certain deployment configurations.

    Args:
        name: The Dapr component name (e.g. ``"atlan-objectstore"``).
        components_dir: Directory containing Dapr component YAML files.

    Returns:
        A configured obstore store instance, or ``None`` if no component
        named *name* exists in *components_dir*.

    Raises:
        StorageConfigError: If the component exists but is genuinely
            misconfigured (unsupported binding type, missing required options,
            etc.).  Note that *broken* components — those with template
            placeholders or unresolvable ``secretKeyRef`` env vars — are
            treated as absent and return ``None`` with a warning instead of
            raising.
    """
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageBindingBrokenError,
        StorageBindingNotFoundError,
    )

    try:
        return create_store_from_binding(name, components_dir=components_dir)
    except StorageBindingNotFoundError:
        return None
    except StorageBindingBrokenError as exc:
        _get_logger().warning(
            "Dapr component '%s' has unresolvable configuration "
            "(broken fields: %s) — treating as absent",
            name,
            ", ".join(exc.broken_fields or []),
        )
        return None


def _create_store_from_binding_optional_with_put_attrs(
    name: str,
    *,
    components_dir: Path | str = Path("./components"),
) -> tuple[ObjectStore, dict[str, str] | None] | tuple[None, None]:
    """Create an obstore store and put attributes from a Dapr binding, or ``(None, None)`` if absent.

    Internal helper for infrastructure-wiring code (``main.py``).

    A missing component logs at INFO (absent is the normal non-SDR path).
    A broken component (template placeholders, unresolvable secretKeyRef) logs
    at WARNING and is treated as absent.

    Args:
        name: The Dapr component name (e.g. ``"atlan-objectstore"``).
        components_dir: Directory containing Dapr component YAML files.

    Returns:
        ``(store, put_attributes)`` on success, or ``(None, None)`` if the
        component is absent or has unresolvable configuration.

    Raises:
        StorageConfigError: If the component exists but is genuinely
            misconfigured (unsupported binding type, missing required options,
            etc.).  Broken components are treated as absent and return
            ``(None, None)`` with a warning instead of raising.
    """
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageBindingBrokenError,
        StorageBindingNotFoundError,
    )

    try:
        return create_store_from_binding_with_put_attrs(
            name, components_dir=components_dir
        )
    except StorageBindingNotFoundError:
        _get_logger().info(
            "No Dapr component named '%s' found — App.upload/download will use "
            "the deployment store. Configure this binding in SDR deployments to "
            "route to the upstream bucket.",
            name,
        )
        return None, None
    except StorageBindingBrokenError as exc:
        _get_logger().warning(
            "Dapr component '%s' has unresolvable configuration "
            "(broken fields: %s) — treating as absent",
            name,
            ", ".join(exc.broken_fields or []),
        )
        return None, None
