"""OpenAPI Spec Loader Connector App.

Extracts metadata from an OpenAPI v3 spec document (APISpec + APIPath) and
transforms it to JSONL format compatible with publish-app.

The connector:
1. Fetches the OpenAPI spec document via a single HTTP GET
2. Parses the spec into APISpec (1) and APIPath (N) records — bundled in one task
3. Transforms to Atlan asset format using pyatlan built-in types
4. Uploads NDJSON to object storage and calls publish-app (if requested)

Extraction pattern: per-scope-item bundled (indivisible API — one GET returns
both APISpec and APIPath data; splitting would require downloading twice).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, TypeVar

import msgspec
from application_sdk.app import App, task
from application_sdk.common.asset_serialization import entity_bytes
from application_sdk.common.entity_envelope import EntityEnvelopePolicy, EnvelopeShape
from application_sdk.contracts.storage import UploadInput
from application_sdk.contracts.types import ConnectionRef, FileReference, StorageTier
from application_sdk.credentials import route_credentials
from application_sdk.credentials.ref import CredentialRef
from application_sdk.errors import InternalError
from application_sdk.errors.base import AppError, sanitize_cause_repr
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter as Logger
from application_sdk.outputs import Metric, get_outputs

from app.api_client import redact_url
from app.api_types import OpenAPIPathRecord, OpenAPISpecRecord
from app.asset_mapper import (
    build_api_spec_qn,
    map_api_path,
    map_api_spec,
    map_connection,
)
from app.cloud_store import CLOUD_SPEC_SUFFIXES, extra_is_malformed, has_valid_auth
from app.contracts import (
    DownloadCloudSpecInput,
    DownloadCloudSpecOutput,
    ExtractSpecInput,
    ExtractSpecOutput,
    OpenAPIConnectorInput,
    OpenAPIConnectorOutput,
    TransformInput,
    TransformOutput,
)
from app.errors import (
    CloudSpecLocationRequiredError,
    CloudSpecNotFoundError,
    ConnectionRequiredError,
    NoValidSpecsError,
    ObjectStoreCredentialError,
    SpecUrlRequiredError,
    TenantObjectStoreUnavailableError,
    UnknownImportTypeError,
)

# Re-exported so the SDK's convention-based handler discovery
# ({AppClassName}Handler in the App class's module) finds the preflight
# handler — the injected gate ran the no-op handler (READY, zero checks)
# without it. See app/handler.py for the checks (CONNECT-812).
from app.handler import OpenAPIConnectorHandler  # noqa: F401

T = TypeVar("T")

# =============================================================================
# Module-level constants
# =============================================================================

# How every transformed JSONL line is shaped (FND-2724). FLATTENED puts
# relationship refs (APIPath.apiSpec) under ``attributes``, which is what
# atlan-publish-app's relationship diffing reads; it is also the SDK default,
# pinned explicitly so an SDK default change cannot flip the wire format.
ENTITY_ENVELOPE = EntityEnvelopePolicy(shape=EnvelopeShape.FLATTENED)


def _is_unsubstituted_placeholder(value: str) -> bool:
    """True if a value still carries mustache braces from an unresolved manifest
    template (e.g. ``"{{connection_qualified_name}}"``). Used to reject a REUSE
    run that never had a real connection selected, before the placeholder leaks
    into downstream object-store paths."""
    return "{{" in value or "}}" in value


def _enc_hook(obj: Any) -> Any:
    """Handle non-standard types during msgspec JSON encoding."""
    return str(obj)


_encoder = msgspec.json.Encoder(enc_hook=_enc_hook)


def _iter_jsonl(ref: FileReference | None, cls: type[T]) -> Any:
    """Yield typed objects from a JSONL file using msgspec for decoding."""
    if ref is None:
        return
    path = Path(ref.local_path or "")
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb") as f:
        for line in f:
            line = line.strip()
            if line:
                yield msgspec.json.decode(line, type=cls)


# =============================================================================
# HTTP methods tracked for APIPath operations
# =============================================================================

_TRACKED_METHODS = ("get", "post", "put", "patch", "delete")


# =============================================================================
# Extraction helper
# =============================================================================


async def _extract_spec_async(
    spec_url: str,
    connection_qualified_name: str,
    auth_header: str,
    logger: Logger,
) -> tuple[FileReference, FileReference, int, int]:
    """Fetch the OpenAPI spec and write APISpec + APIPath JSONL files.

    Returns:
        Tuple of (api_spec_file, api_path_file, api_spec_count, api_path_count).
    """
    from app.api_client import OpenAPIApiClient

    out_dir = Path(tempfile.mkdtemp(prefix="openapi-extract-"))

    client = OpenAPIApiClient(auth_header=auth_header)
    try:
        specs = await client.fetch_spec(spec_url)
    finally:
        await client.close()

    spec_file_path = out_dir / "api_spec.jsonl"
    path_file_path = out_dir / "api_path.jsonl"
    spec_count = 0
    path_count = 0

    with spec_file_path.open("wb") as spec_f, path_file_path.open("wb") as path_f:
        for spec in specs:
            info = spec.get("info", {})
            title = info.get("title", "")
            if not title:
                logger.warning("skipping spec with missing 'info.title'")
                continue

            spec_qn = build_api_spec_qn(connection_qualified_name, title)

            # --- Write APISpec record ---
            spec_record = OpenAPISpecRecord(
                title=title,
                openapi_version=spec.get("openapi", spec.get("swagger", "")),
                description=info.get("description", ""),
                terms_of_service=info.get("termsOfService", ""),
                contact_name=info.get("contact", {}).get("name", "")
                if info.get("contact")
                else "",
                contact_email=info.get("contact", {}).get("email", "")
                if info.get("contact")
                else "",
                contact_url=info.get("contact", {}).get("url", "")
                if info.get("contact")
                else "",
                license_name=info.get("license", {}).get("name", "")
                if info.get("license")
                else "",
                license_url=info.get("license", {}).get("url", "")
                if info.get("license")
                else "",
                spec_version=info.get("version", ""),
                external_docs_url=spec.get("externalDocs", {}).get("url", "")
                if spec.get("externalDocs")
                else "",
                external_docs_description=spec.get("externalDocs", {}).get(
                    "description", ""
                )
                if spec.get("externalDocs")
                else "",
                spec_url=spec_url,
            )
            spec_f.write(_encoder.encode(spec_record) + b"\n")
            spec_count += 1

            # --- Write APIPath records ---
            # `spec.get("paths") or {}` handles both absent key and `"paths": null`.
            # OAS 3.1 specs may use `webhooks` instead of (or alongside) `paths`;
            # merge both so enterprise specs that moved to webhooks-only still produce
            # APIPath assets.
            paths: dict = spec.get("paths") or {}
            webhooks: dict = spec.get("webhooks") or {}
            all_path_items = {**paths, **webhooks}
            for path_url, path_item in all_path_items.items():
                if not isinstance(path_item, dict):
                    continue

                # Collect available operations (uppercase methods) — matches Kotlin's addOperationDetails
                # sorted() matches SPEC.md §4.3 hashable content for stable change detection.
                operations: list[str] = sorted(
                    m.upper() for m in _TRACKED_METHODS if path_item.get(m) is not None
                )

                # Build markdown description from path-item and operation details
                description_parts: list[str] = []

                # Include path-item level description if present
                path_description = (
                    path_item.get("description", "")
                    if isinstance(path_item, dict)
                    else ""
                )
                if path_description:
                    description_parts.append(path_description)

                # Build operations summary table
                if operations:
                    rows = ["| Method | Summary|", "|---|---|"]
                    for method in _TRACKED_METHODS:
                        op = path_item.get(method)
                        if op is not None:
                            op_summary = (
                                op.get("summary", "") if isinstance(op, dict) else ""
                            )
                            rows.append(f"| `{method.upper()}` |{op_summary} |")
                    description_parts.append("\n".join(rows))

                # Append operation-level descriptions
                for method in _TRACKED_METHODS:
                    op = path_item.get(method)
                    if op is not None and isinstance(op, dict):
                        op_desc = op.get("description", "")
                        if op_desc:
                            description_parts.append(
                                f"**{method.upper()}**\n\n{op_desc}"
                            )

                description = "\n\n".join(description_parts)

                path_record = OpenAPIPathRecord(
                    path_url=path_url,
                    spec_title=title,
                    spec_qualified_name=spec_qn,
                    summary=path_item.get("summary", ""),
                    available_operations=operations,
                    description=description,
                    is_templated="{" in path_url and "}" in path_url,
                )
                path_f.write(_encoder.encode(path_record) + b"\n")
                path_count += 1

    if spec_count == 0:
        # CONNECT-812 EP-03 class: every fetched document was skipped, so
        # returning zero records would let the run finish green having
        # extracted nothing — indistinguishable from an empty source on every
        # surface. fetch_spec never returns an empty list (a spec-less ZIP
        # already raises), so zero here always means "documents fetched, none
        # usable".
        raise NoValidSpecsError(
            message=(
                f"none of the {len(specs)} document(s) at {redact_url(spec_url)} "
                "is a usable OpenAPI spec — each is missing the required "
                "'info.title' field"
            ),
            field="spec_url",
            constraint=(
                "at least one document must carry info.title "
                "(required by the OpenAPI specification)"
            ),
        )

    logger.info(
        "spec extracted api_spec_count=%d api_path_count=%d",
        spec_count,
        path_count,
    )
    return (
        FileReference(local_path=str(spec_file_path), tier=StorageTier.RETAINED),
        FileReference(local_path=str(path_file_path), tier=StorageTier.RETAINED),
        spec_count,
        path_count,
    )


# =============================================================================
# Transform helper
# =============================================================================


def _transform_blocking(
    input: TransformInput,
    logger: Logger,
) -> TransformOutput:
    """Transform OpenAPI records to Atlan Atlas entity format."""
    out_dir = Path(tempfile.mkdtemp(prefix="openapi-transform-"))
    output_file = out_dir / "openapi_metadata.json"

    connection = input.connection
    if connection is None:
        raise ConnectionRequiredError(
            message="connection is required for transform",
            field="connection",
            constraint="required",
        )
    conn_qn: str = connection.attributes.qualified_name
    workflow_id = input.workflow_id
    workflow_type = input.workflow_type
    workflow_run_at_ms = input.workflow_run_at_ms

    api_spec_count = 0
    api_path_count = 0

    # Every record goes through the SDK's entity_bytes seam (FND-2724): it owns
    # the envelope, the unmappable-result check and placeholder-guid stripping.
    with output_file.open("wb") as out_f:
        # CONNECT-55: on REUSE (emit_connection=False) the connection already
        # exists and must not be re-upserted — emit only its child assets.
        if input.emit_connection:
            out_f.write(
                entity_bytes(
                    map_connection(connection),
                    entity_type="connection",
                    envelope=ENTITY_ENVELOPE,
                )
                + b"\n"
            )

        # Emit APISpec records
        for record in _iter_jsonl(input.api_spec_file, OpenAPISpecRecord):
            asset = map_api_spec(
                record, conn_qn, workflow_id, workflow_type, workflow_run_at_ms
            )
            out_f.write(
                entity_bytes(asset, entity_type="api_spec", envelope=ENTITY_ENVELOPE)
                + b"\n"
            )
            api_spec_count += 1

        # Emit APIPath records
        for record in _iter_jsonl(input.api_path_file, OpenAPIPathRecord):
            asset = map_api_path(
                record, conn_qn, workflow_id, workflow_type, workflow_run_at_ms
            )
            out_f.write(
                entity_bytes(asset, entity_type="api_path", envelope=ENTITY_ENVELOPE)
                + b"\n"
            )
            api_path_count += 1

    total = api_spec_count + api_path_count
    logger.info(
        "transform complete api_spec_count=%d api_path_count=%d total=%d",
        api_spec_count,
        api_path_count,
        total,
    )

    return TransformOutput(
        output_file=FileReference(
            local_path=str(output_file), tier=StorageTier.RETAINED
        ),
        api_spec_count=api_spec_count,
        api_path_count=api_path_count,
    )


# =============================================================================
# OpenAPI Connector App
# =============================================================================


class OpenAPIConnector(App):
    """OpenAPI Spec Loader connector app.

    Extracts APISpec + APIPath metadata from an OpenAPI spec document and
    transforms it to JSONL format compatible with publish-app.

    Extraction is bundled in a single task (indivisible API: one GET returns
    both APISpec and APIPath data in the same response).

    Tasks:
    1. extract_spec — fetch spec URL, emit api_spec.jsonl + api_path.jsonl
    2. transform — map to Atlan Atlas entity format
    3. publish — upload NDJSON to object storage and call publish-app (if load_to_atlan=True)
    """

    name = "openapi"

    passthrough_modules = {"app.asset_mapper"}  # noqa: RUF012

    @task(
        timeout_seconds=1800,
        heartbeat_timeout_seconds=120,
        auto_heartbeat_seconds=30,
    )
    async def download_cloud_spec(
        self, input: DownloadCloudSpecInput
    ) -> DownloadCloudSpecOutput:
        """Download OpenAPI spec from cloud storage. Runs as activity (has I/O)."""
        from application_sdk.storage.cloud import CloudStore

        credential_data = None
        if input.openapi_credential is not None:
            # Preferred: an object-store credential ref resolved once at the
            # workflow entrypoint. In SDR (agent) mode this is the agent-aware
            # ref built by CredentialRef.resolve(input) from the forwarded
            # agent_json; in direct/PKL mode it is the standard credential slot
            # ({{credential}} -> openapi_credential). Consumes CloudStore's
            # required raw dict via resolve_credential_raw.
            credential_data = await self.context.resolve_credential_raw(
                input.openapi_credential
            )
            self.logger.info(
                "resolved openapi_credential keys=%s",
                list(credential_data.keys()),
            )
        elif input.cloud_source:
            # Legacy fallback only, and it does NOT work for pre-migration
            # configs (CONNECT-800). Those stored a bare
            # csa-connectors-objectstore GUID here; this branch resolves strictly
            # by GUID. Agent (SDR) mode is handled upstream by
            # CredentialRef.resolve(input).
            #
            # GUID resolution reads
            # persistent-artifacts/apps/openapi/credentials/<guid>/config.json,
            # which the platform normally writes; a GUID predating the app's own
            # credential route has nothing there. To backfill one by hand, POST
            # /workflows/v1/config/<guid>?type=credentials on the app — the SDK
            # serves that route in any environment.
            #
            # The absent object reports as "credential-config store unreachable"
            # after a ~120s ladder, not as a not-found: Dapr maps the S3
            # NoSuchKey error to 500, and the SDK's binding path treats any 5xx
            # as a cold sidecar (is_dapr_transport_unavailable — the secrets path
            # deliberately does not). The vault's own not-found branch needs a
            # 2xx with an empty body, so it never runs here.
            ref = CredentialRef(credential_guid=input.cloud_source)
            credential_data = await self.context.resolve_credential_raw(ref)
            self.logger.info(
                "resolved cloud_source credential keys=%s",
                list(credential_data.keys()),
            )

        if credential_data is not None and extra_is_malformed(credential_data):
            # Otherwise the run silently reads the tenant store instead of the
            # customer's bucket. The value itself is never logged.
            self.logger.warning(
                "cloud_source credential 'extra' is not valid JSON; "
                "treating as no role auth"
            )
        if credential_data is not None and has_valid_auth(credential_data):
            self.logger.info(
                "using external cloud storage auth_type=%s",
                credential_data.get("authType")
                or credential_data.get("auth_type")
                or "unknown",
            )
            try:
                store = CloudStore.from_credentials(credential_data)
            except AppError:
                # Already typed and attributable — let the SDK's own error
                # through; the app cannot re-wrap these without losing their
                # retryable/audience semantics. This branch takes every
                # StorageConfigError from_credentials documents, so the broad
                # catch below only ever sees an *undocumented* escape.
                raise
            except Exception as exc:
                # Breadth is deliberate: CloudStore.from_credentials reaches
                # three cloud SDKs, and anything they raise that is not already
                # an AppError still has to become one typed error rather than
                # escape untyped. Narrowing this to a named set would let the
                # unnamed ones propagate untyped, which is strictly worse.
                #
                # CONNECT-812 PF-17: the chain is SEVERED (`from None`), and
                # tests/unit/test_connector.py::
                # test_rejected_credential_raises_typed_with_severed_chain
                # pins it. Do not "simplify" this to `from exc` to clear E004.
                #
                # The frame-variable axis is no longer the reason. Before SDK
                # 3.29.0 the sinks took loguru's own `diagnose` default of
                # True, which annotates each traceback frame with the *values*
                # of the names on its source line — a chained traceback then
                # rendered this frame's credential dict. From 3.29.0 the SDK
                # passes `diagnose=ENABLE_LOG_DIAGNOSE`, default False, and
                # pyproject floors the SDK there, so that axis is closed. It
                # was console-only in any case: the OTLP sink builds its own
                # traceback and never carried frame values.
                #
                # What still justifies severing is the axis `diagnose` does
                # NOT govern — the cause's own *message*.
                # `traceback.format_exception` follows `__cause__`, so a
                # chained cause's message reaches the OTLP sink whatever
                # `diagnose` says, and this catch is for *undocumented*
                # escapes from three third-party cloud SDKs whose message text
                # is not ours to constrain. Surveying today's shapes (they are
                # clean) would not bind tomorrow's.
                #
                # The cause survives as a redacted, length-capped summary on
                # the typed error below, which is the single record of this
                # failure (the caller logs it once — L009).
                #
                # This is why E004 still fires here. Every rule-clean
                # alternative costs more than the warning: `from exc` breaks
                # the contract above; narrowing the catch lets an unnamed
                # escape propagate untyped; a redaction-helper log takes
                # E004's other exemption but must be WARNING/ERROR to count,
                # which is L009.
                raise ObjectStoreCredentialError(
                    message=(
                        "object-store credential was rejected while building "
                        f"the cloud store client: {sanitize_cause_repr(exc)}"
                    ),
                    field="openapi_credential",
                    constraint=(
                        "must be a resolvable object-store credential "
                        "(authType s3/gcs/adls)"
                    ),
                ) from None
        else:
            if credential_data is not None:
                self.logger.info(
                    "credential has no key/role auth, falling back to tenant store"
                )
            else:
                self.logger.info("no cloud_source credential, using tenant store")
            if self.context.storage is None:
                raise TenantObjectStoreUnavailableError(
                    message=(
                        "No tenant object store available. Ensure Dapr objectstore "
                        "binding is configured on this deployment."
                    ),
                    service="dapr_objectstore",
                    retryable=False,
                    suggested_action=(
                        "Configure the Dapr objectstore binding on this deployment, "
                        "or supply an external object-store credential."
                    ),
                )
            store = CloudStore(self.context.storage, provider="tenant")

        tmp_dir = tempfile.mkdtemp(prefix="openapi-cloud-")
        prefix = input.spec_prefix.strip("/") if input.spec_prefix else ""
        key = input.spec_key.strip("/") if input.spec_key else ""
        if key:
            full_key = f"{prefix}/{key}" if prefix else key
            local_paths = await store.download(key=full_key, output_dir=tmp_dir)
        else:
            local_paths = await store.download(
                prefix=prefix,
                output_dir=tmp_dir,
                suffix_filter=set(CLOUD_SPEC_SUFFIXES),
            )
        return DownloadCloudSpecOutput(
            spec_files=[
                FileReference(local_path=str(p), tier=StorageTier.RETAINED)
                for p in local_paths
            ]
        )

    @task(
        timeout_seconds=3600,
        heartbeat_timeout_seconds=120,
        auto_heartbeat_seconds=30,
    )
    async def extract_spec(self, input: ExtractSpecInput) -> ExtractSpecOutput:
        """Fetch the OpenAPI spec URL and extract APISpec + APIPath records."""
        # Redacted: a spec URL is routinely pre-signed (Azure SAS, S3 presigned),
        # so the query string is a credential and logs are not a secret store.
        self.logger.info(
            "extract_spec task starting spec_url=%s", redact_url(input.spec_url)
        )

        if not input.spec_url:
            raise SpecUrlRequiredError(
                message="spec_url is required for extract_spec",
                field="spec_url",
                constraint="required",
            )

        spec_file, path_file, spec_count, path_count = await _extract_spec_async(
            spec_url=input.spec_url,
            connection_qualified_name=input.connection_qualified_name,
            auth_header="",
            logger=self.logger,
        )

        self.logger.info(
            "extract_spec task completed api_spec_count=%d api_path_count=%d",
            spec_count,
            path_count,
        )
        get_outputs().add_metric(
            Metric(
                name="specs-extracted",
                value=spec_count,
                display_name="API Specs Extracted",
            )
        )
        get_outputs().add_metric(
            Metric(
                name="endpoints-extracted",
                value=path_count,
                display_name="API Endpoints Extracted",
            )
        )
        return ExtractSpecOutput(
            api_spec_file=spec_file,
            api_path_file=path_file,
            api_spec_count=spec_count,
            api_path_count=path_count,
        )

    @task(
        timeout_seconds=1800,
        heartbeat_timeout_seconds=120,
        auto_heartbeat_seconds=30,
    )
    async def transform(self, input: TransformInput) -> TransformOutput:
        """Transform OpenAPI records to Atlan Atlas entity format."""
        self.logger.info("transform task starting")

        result = await self.run_in_thread(_transform_blocking, input, self.logger)

        self.logger.info(
            "transform task completed api_spec_count=%d api_path_count=%d",
            result.api_spec_count,
            result.api_path_count,
        )
        return result

    # The MRO really is OpenAPIConnectorInput -> AppInputContract ->
    # ExtractionInput -> Input; the base is declared in the generated module,
    # which the static check does not follow.
    # conformance: ignore[P013] subclasses Input via the generated base, pinned by test_run_input_subclasses_the_sdk_input
    async def run(self, input: OpenAPIConnectorInput) -> OpenAPIConnectorOutput:  # type: ignore[override]
        """Execute the OpenAPI Spec Loader connector workflow.

        Orchestration:
        1. Validate required fields
        2. extract_spec — fetch and parse the OpenAPI spec
        3. transform — map to Atlan Atlas entities (if records were extracted)
        4. publish — sync to Atlan via publish-app (if load_to_atlan=True)
        """
        # CONNECT-55: resolve the connection per connection_usage.
        #  * CREATE → build a NEW connection from the ConnectionCreator input,
        #    emit the Connection entity, and run the normal full-diff publish.
        #  * REUSE  → target an EXISTING connection selected via
        #    connection_qualified_name. Do NOT re-emit/modify it, and signal
        #    publish-app's assertion-only mode (upsert-only: no diff, no deletes)
        #    so assets owned by other sources in that shared connection are never
        #    archived. The existing connection must already exist in Atlan.
        if input.connection_usage == "REUSE":
            conn_qn = input.connection_qualified_name
            if not conn_qn:
                raise ConnectionRequiredError(
                    message="connection_qualified_name is required when connection_usage='REUSE'",
                    field="connection_qualified_name",
                    constraint="required when connection_usage='REUSE'",
                )
            # Guard against an unsubstituted manifest placeholder (e.g. a caller
            # that runs REUSE without selecting a connection, so
            # "{{connection_qualified_name}}" leaks through). Left unchecked it
            # becomes a real-looking QN that only fails deep in publish with a
            # cryptic FileNotFoundError on a path containing the braces.
            if _is_unsubstituted_placeholder(conn_qn):
                raise ConnectionRequiredError(
                    message=(
                        "connection_qualified_name is an unsubstituted placeholder "
                        f"({conn_qn!r}) — REUSE requires an existing connection to be "
                        "selected so its qualified name is provided."
                    ),
                    field="connection_qualified_name",
                    constraint="must be a resolved connection qualified name",
                )
            # Minimal ref: only the QN is needed to build child asset QNs. The
            # existing connection is left untouched (not emitted), so its
            # attributes/ACLs are never overwritten by a partial upsert.
            connection = ConnectionRef.model_validate(
                {"typeName": "Connection", "attributes": {"qualifiedName": conn_qn}}
            )
            emit_connection = False
            assertion_only_enabled = True
        else:  # CREATE (default path)
            connection = input.connection
            conn_qn = connection.attributes.qualified_name if connection else ""
            if not conn_qn:
                raise ConnectionRequiredError(
                    message="connection.qualified_name is required when connection_usage='CREATE'",
                    field="connection",
                    constraint="required when connection_usage='CREATE'",
                )
            emit_connection = True
            assertion_only_enabled = False

        if input.import_type == "CLOUD":
            if not input.spec_prefix and not input.spec_key:
                raise CloudSpecLocationRequiredError(
                    message="spec_prefix or spec_key required when import_type='CLOUD'",
                    field="spec_prefix|spec_key",
                    constraint="at least one is required when import_type='CLOUD'",
                )
            # Resolve the object-store credential ONCE, agent-aware, and thread
            # the ref into the download task. route_credentials is the SDK's one
            # router, and the preflight gate finds the same ref: a pre-built ref
            # (the openapi_credential slot, PKL/direct) wins, then agent_json /
            # credential_guid go through CredentialRef.resolve — in SDR (agent)
            # mode the platform forwards agent_json rather than a pre-built ref
            # or GUID. An input with no routable source yields no ref, and the
            # task falls back to the legacy cloud_source GUID. A misrouted input
            # (agent mode with an empty agent_json) raises here and names the
            # cause instead of silently using cloud_source.
            cloud_credential_ref, _ = route_credentials(input)
            # Download spec from cloud storage via task (credential resolution
            # and cloud I/O must run in an activity, not workflow code).
            cloud_result = await self.download_cloud_spec(
                DownloadCloudSpecInput(
                    openapi_credential=cloud_credential_ref,
                    cloud_source=input.cloud_source,
                    spec_prefix=input.spec_prefix,
                    spec_key=input.spec_key,
                )
            )
            spec_urls = [
                ref.local_path for ref in cloud_result.spec_files if ref.local_path
            ]
            if not spec_urls:
                # CONNECT-812 EP-03 class: a prefix/key that matches nothing
                # would otherwise skip extract, transform, and publish and
                # finish green with zero assets — no signal on any surface.
                raise CloudSpecNotFoundError(
                    message=(
                        "no spec files found in the object store at "
                        f"prefix={input.spec_prefix!r} key={input.spec_key!r}"
                    ),
                    resource_type="openapi_spec_file",
                    resource_identifier=(
                        f"{input.spec_prefix}/{input.spec_key}".strip("/")
                    ),
                    suggested_action=(
                        "Check the prefix and object key, and that the spec "
                        "files end in .json/.yaml/.yml/.zip."
                    ),
                )
        elif input.import_type == "URL":
            if not input.spec_url:
                raise SpecUrlRequiredError(
                    message="spec_url is required when import_type='URL'",
                    field="spec_url",
                    constraint="required when import_type='URL'",
                )
            spec_urls = [input.spec_url]
        else:
            raise UnknownImportTypeError(
                message=f"Unknown import_type: {input.import_type}",
                field="import_type",
                constraint="must be 'URL' or 'CLOUD'",
                value_summary=str(input.import_type),
            )

        self.logger.info(
            "openapi connector starting connection_qualified_name=%s spec_urls=%s load_to_atlan=%s connection_usage=%s assertion_only_enabled=%s",
            conn_qn,
            [redact_url(u) for u in spec_urls],
            input.load_to_atlan,
            input.connection_usage,
            assertion_only_enabled,
        )

        # ================================================================
        # Step 1: Extract APISpec + APIPath (bundled, one per spec file)
        # ================================================================
        total_scanned = 0
        all_extract_results: list[ExtractSpecOutput] = []

        for i, spec_url in enumerate(spec_urls):
            extract_result = await self.extract_spec(
                ExtractSpecInput(
                    spec_url=spec_url,
                    connection_qualified_name=conn_qn,
                )
            )
            all_extract_results.append(extract_result)
            total_scanned += (
                extract_result.api_spec_count + extract_result.api_path_count
            )

        # ================================================================
        # Step 2: Transform to Atlan Atlas entity format
        # ================================================================
        api_spec_count = 0
        api_path_count = 0
        output_file_ref: FileReference | None = None

        if total_scanned > 0:
            workflow_run_at_ms = int(self.context.started_at.timestamp() * 1000)
            for i, extract_result in enumerate(all_extract_results):
                if extract_result.api_spec_count + extract_result.api_path_count == 0:
                    continue
                transform_result = await self.transform(
                    TransformInput(
                        api_spec_file=extract_result.api_spec_file,
                        api_path_file=extract_result.api_path_file,
                        connection=connection,
                        connection_qualified_name=conn_qn,
                        emit_connection=emit_connection,
                        workflow_id=self.run_id,
                        workflow_type=self.context.app_name,
                        workflow_run_at_ms=workflow_run_at_ms,
                    )
                )
                output_file_ref = transform_result.output_file
                api_spec_count += transform_result.api_spec_count
                api_path_count += transform_result.api_path_count

        # ================================================================
        # Step 3: Upload and publish to Atlan (if requested)
        # ================================================================
        publish_completed = False
        output_file_path = output_file_ref.local_path if output_file_ref else ""

        transformed_data_prefix = ""

        if input.load_to_atlan and output_file_path:
            upload_result = await self.upload(
                UploadInput(
                    local_path=output_file_path,
                    storage_subdir="transformed",
                )
            )
            if not upload_result.ref.storage_path:
                raise InternalError(
                    message=(
                        "upload_result.ref.storage_path is None; "
                        "cannot derive transformed_data_prefix"
                    ),
                    component="connector.upload",
                    invariant="upload activity must populate ref.storage_path",
                    classification_pending=True,
                )
            transformed_data_prefix = str(Path(upload_result.ref.storage_path).parent)
            publish_completed = True
            self.logger.info(
                "upload complete transformed_data_prefix=%s", transformed_data_prefix
            )

        self.logger.info(
            "openapi connector completed api_spec_count=%d api_path_count=%d total_scanned=%d publish_completed=%s",
            api_spec_count,
            api_path_count,
            total_scanned,
            publish_completed,
        )

        return OpenAPIConnectorOutput(
            connection_qualified_name=conn_qn,
            transformed_data_prefix=transformed_data_prefix,
            api_spec_count=api_spec_count,
            api_path_count=api_path_count,
            output_file=output_file_ref,
            total_scanned=total_scanned,
            publish_completed=publish_completed,
            assertion_only_enabled=assertion_only_enabled,
        )
