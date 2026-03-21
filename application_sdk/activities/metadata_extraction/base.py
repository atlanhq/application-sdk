import asyncio
from typing import Any, Dict, Optional, Type

import aiohttp
from temporalio import activity

from application_sdk.activities import ActivitiesInterface, ActivitiesState
from application_sdk.activities.common.models import (
    ActivityStatistics,
    LhLoadRequest,
    LhLoadResponse,
    LhLoadStatusResponse,
)
from application_sdk.activities.common.utils import (
    auto_heartbeater,
    get_object_store_prefix,
    get_workflow_id,
)
from application_sdk.clients.base import BaseClient
from application_sdk.common.error_codes import ActivityError
from application_sdk.constants import (
    APP_TENANT_ID,
    APPLICATION_NAME,
    LH_LOAD_MAX_POLL_ATTEMPTS,
    LH_LOAD_POLL_INTERVAL_SECONDS,
    MDLH_BASE_URL,
)
from application_sdk.handlers.base import BaseHandler
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.atlan_storage import AtlanStorage
from application_sdk.services.secretstore import SecretStore
from application_sdk.transformers import TransformerInterface

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 405, 422}

logger = get_logger(__name__)
activity.logger = logger


class BaseMetadataExtractionActivitiesState(ActivitiesState):
    """State for base metadata extraction activities."""

    client: Optional[BaseClient] = None
    handler: Optional[BaseHandler] = None
    transformer: Optional[TransformerInterface] = None


class BaseMetadataExtractionActivities(ActivitiesInterface):
    """Base activities for non-SQL metadata extraction workflows."""

    _state: Dict[str, BaseMetadataExtractionActivitiesState] = {}

    client_class: Type[BaseClient] = BaseClient
    handler_class: Type[BaseHandler] = BaseHandler
    transformer_class: Optional[Type[TransformerInterface]] = None

    def __init__(
        self,
        client_class: Optional[Type[BaseClient]] = None,
        handler_class: Optional[Type[BaseHandler]] = None,
        transformer_class: Optional[Type[TransformerInterface]] = None,
    ):
        """Initialize the base metadata extraction activities.

        Args:
            client_class: Client class to use. Defaults to BaseClient.
            handler_class: Handler class to use. Defaults to BaseHandler.
            transformer_class: Transformer class to use. Users must provide their own transformer implementation.
        """
        if client_class:
            self.client_class = client_class
        if handler_class:
            self.handler_class = handler_class
        if transformer_class:
            self.transformer_class = transformer_class

        super().__init__()

    async def _set_state(self, workflow_args: Dict[str, Any]):
        """Set up the state for the current workflow.

        Args:
            workflow_args: Arguments for the workflow.
        """
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = BaseMetadataExtractionActivitiesState()

        await super()._set_state(workflow_args)

        state = self._state[workflow_id]

        # Initialize client
        client = self.client_class()
        # Extract credentials from state store if credential_guid is available
        if "credential_guid" in workflow_args:
            logger.info(
                f"Retrieving credentials for credential_guid: {workflow_args['credential_guid']}"
            )
            try:
                credentials = await SecretStore.get_credentials(
                    workflow_args["credential_guid"]
                )
                logger.info(
                    f"Successfully retrieved credentials with keys: {list(credentials.keys())}"
                )
                # Load the client with credentials
                await client.load(credentials=credentials)
            except Exception as e:
                logger.error(f"Failed to retrieve credentials: {e}")
                raise

        state.client = client

        # Initialize handler
        handler = self.handler_class(client=client)
        state.handler = handler

        # Initialize transformer if provided
        if self.transformer_class:
            transformer_params = {
                "connector_name": APPLICATION_NAME,
                "connector_type": APPLICATION_NAME,
                "tenant_id": APP_TENANT_ID,
            }
            state.transformer = self.transformer_class(**transformer_params)

    @activity.defn
    @auto_heartbeater
    async def upload_to_atlan(
        self, workflow_args: Dict[str, Any]
    ) -> ActivityStatistics:
        """Upload transformed data to Atlan storage.

        This activity uploads the transformed data from object store to Atlan storage
        (S3 via Dapr). It only runs if ENABLE_ATLAN_UPLOAD is set to true and the
        Atlan storage component is available.

        Args:
            workflow_args (Dict[str, Any]): Workflow configuration containing paths and metadata.

        Returns:
            ActivityStatistics: Upload statistics or skip statistics if upload is disabled.

        Raises:
            ValueError: If workflow_id or workflow_run_id are missing.
            ActivityError: If the upload fails with any migration errors when ENABLE_ATLAN_UPLOAD is true.
        """
        # Upload data from object store to Atlan storage
        # Use workflow_id/workflow_run_id as the prefix to migrate specific data
        migration_prefix = workflow_args["output_path"]
        logger.info(
            f"Starting migration from object store with prefix: {migration_prefix}"
        )
        upload_stats = await AtlanStorage.migrate_from_objectstore_to_atlan(
            prefix=migration_prefix
        )

        # Log upload statistics
        logger.info(
            f"Atlan upload completed: {upload_stats.migrated_files} files uploaded, "
            f"{upload_stats.failed_migrations} failed"
        )

        if upload_stats.failures:
            logger.error(f"Upload failed with {len(upload_stats.failures)} errors")
            for failure in upload_stats.failures:
                logger.error(f"Upload error: {failure}")

            # Mark activity as failed when there are upload failures
            raise ActivityError(
                f"{ActivityError.ATLAN_UPLOAD_ERROR}: Atlan upload failed with {len(upload_stats.failures)} errors. "
                f"Failed migrations: {upload_stats.failed_migrations}, "
                f"Total files: {upload_stats.total_files}"
            )

        return ActivityStatistics(
            total_record_count=upload_stats.migrated_files,
            chunk_count=upload_stats.total_files,
            typename="atlan-upload-completed",
        )

    @activity.defn
    @auto_heartbeater
    async def load_to_lakehouse(
        self, workflow_args: Dict[str, Any]
    ) -> ActivityStatistics:
        """Load data files to Iceberg lakehouse via MDLH REST API.

        Expects workflow_args to contain lh_load_config dict with:
            - output_path: str — local path prefix (will be converted to S3 key)
            - namespace: str — Iceberg namespace
            - table_name: str — Iceberg table name
            - mode: str — "APPEND" or "UPSERT"
            - file_extension: str — ".parquet" or ".jsonl"
        """
        return await do_lakehouse_load(workflow_args)


_TERMINAL_FAILURE_STATES = {"FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}


async def do_lakehouse_load(workflow_args: Dict[str, Any]) -> ActivityStatistics:
    """Shared implementation for lakehouse load activity.

    Called from both BaseMetadataExtractionActivities and
    BaseSQLMetadataExtractionActivities.
    """
    lh_config = workflow_args.get("lh_load_config")
    if not lh_config:
        raise ActivityError(
            f"{ActivityError.LAKEHOUSE_LOAD_ERROR}: Missing lh_load_config in workflow_args"
        )

    output_path = lh_config.get("output_path", "")
    namespace = lh_config.get("namespace", "")
    table_name = lh_config.get("table_name", "")
    mode = lh_config.get("mode", "APPEND")
    file_extension = lh_config.get("file_extension", "")

    if not namespace or not table_name or not output_path or not file_extension:
        raise ActivityError(
            f"{ActivityError.LAKEHOUSE_LOAD_ERROR}: "
            "Missing required fields in lh_load_config (namespace, table_name, output_path, file_extension)"
        )

    s3_prefix = get_object_store_prefix(output_path)
    pattern = f"{s3_prefix}/**/*{file_extension}"

    request = LhLoadRequest(
        patterns=[pattern],
        namespace=namespace,
        table_name=table_name,
        mode=mode,
    )
    request_payload = request.model_dump(by_alias=True, exclude_none=True)

    base_url = f"{MDLH_BASE_URL}/atlan/lh/v1/{APP_TENANT_ID}/load"
    headers = {"X-Atlan-Tenant-Id": APP_TENANT_ID}

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        # Submit load job
        async with session.post(
            base_url, json=request_payload, headers=headers
        ) as resp:
            if resp.status != 202:
                body = await resp.text()
                raise ActivityError(
                    f"{ActivityError.LAKEHOUSE_LOAD_API_ERROR}: "
                    f"MDLH load API returned {resp.status}: {body}"
                )
            response_data = await resp.json()

        load_response = LhLoadResponse.model_validate(response_data)
        job_id = load_response.job_id
        logger.info(f"Lakehouse load job submitted: job_id={job_id}")

    # Poll for completion (fresh session per poll to avoid stale connections)
    status_url = f"{base_url}/{job_id}/status"
    poll_headers = {**headers, "X-Lakehouse-Job-Id": job_id}
    for attempt in range(LH_LOAD_MAX_POLL_ATTEMPTS):
        await asyncio.sleep(LH_LOAD_POLL_INTERVAL_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.get(status_url, headers=poll_headers) as resp:
                    if resp.status in _NON_RETRYABLE_STATUS_CODES:
                        body = await resp.text()
                        raise ActivityError(
                            f"{ActivityError.LAKEHOUSE_LOAD_API_ERROR}: "
                            f"MDLH status poll returned non-retryable {resp.status}: {body}"
                        )
                    if resp.status != 200:
                        logger.warning(
                            f"Lakehouse load status poll returned {resp.status} "
                            f"(attempt {attempt + 1}/{LH_LOAD_MAX_POLL_ATTEMPTS}), retrying..."
                        )
                        continue
                    status_data = await resp.json()
        except ActivityError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(
                f"Lakehouse load status poll error (attempt {attempt + 1}/{LH_LOAD_MAX_POLL_ATTEMPTS}): {e}, retrying..."
            )
            continue

        status_response = LhLoadStatusResponse.model_validate(status_data)
        current_status = status_response.status.upper()

        if current_status == "COMPLETED":
            logger.info(f"Lakehouse load job completed: job_id={job_id}")
            return ActivityStatistics(typename="lakehouse-load-completed")

        if current_status in _TERMINAL_FAILURE_STATES:
            raise ActivityError(
                f"{ActivityError.LAKEHOUSE_LOAD_ERROR}: "
                f"Lakehouse load job {job_id} ended with status: {current_status}"
            )

    raise ActivityError(
        f"{ActivityError.LAKEHOUSE_LOAD_TIMEOUT_ERROR}: "
        f"Lakehouse load job {job_id} did not complete within "
        f"{LH_LOAD_MAX_POLL_ATTEMPTS * LH_LOAD_POLL_INTERVAL_SECONDS}s"
    )


# ---- Raw-to-lakehouse preparation ----

# Maps SDK typename to the raw column that holds the entity name.
_RAW_ENTITY_NAME_FIELDS: Dict[str, str] = {
    "database": "database_name",
    "schema": "schema_name",
    "table": "table_name",
    "column": "column_name",
}


@activity.defn
async def prepare_raw_for_lakehouse(
    raw_output_path: str,
    typenames: list[str],
    connection_qualified_name: str,
    workflow_id: str,
    workflow_run_id: str,
    extracted_at: int,
    tenant_id: str,
) -> str:
    """Convert raw parquet files into common-schema JSONL for lakehouse ingestion.

    For each typename's parquet files under ``raw_output_path/{typename}/``, every
    row is wrapped into a per-connector table schema:

        typename, connection_qualified_name,
        workflow_id, workflow_run_id, extracted_at, tenant_id,
        entity_name, raw_record (full row as JSON string)

    The table name is the connector name (e.g. ``entity_raw.redshift``).

    The output JSONL files are written next to the raw directory under
    ``{raw_output_path}/../raw_lakehouse/{typename}/``.

    Returns the root output directory (``…/raw_lakehouse``).
    """
    import json
    import os

    import orjson

    from application_sdk.common.file_ops import SafeFileOps
    from application_sdk.io.utils import download_files

    base_dir = os.path.normpath(os.path.join(raw_output_path, "..", "raw_lakehouse"))

    for typename in typenames:
        src_dir = os.path.join(raw_output_path, typename)
        parquet_files = await download_files(src_dir, ".parquet", file_names=None)

        if not parquet_files:
            logger.info(f"No raw parquet files for typename={typename}, skipping")
            continue

        out_dir = os.path.join(base_dir, typename)
        SafeFileOps.makedirs(out_dir, exist_ok=True)

        entity_name_field = _RAW_ENTITY_NAME_FIELDS.get(typename)
        chunk_idx = 0

        for pq_file in parquet_files:
            try:
                import daft

                df = daft.read_parquet(pq_file)
                rows = df.to_pydict()
                col_names = list(rows.keys())
                n_rows = len(rows[col_names[0]]) if col_names else 0
            except Exception as e:
                logger.warning(f"Failed to read parquet {pq_file}: {e}, skipping")
                continue

            out_file = os.path.join(out_dir, f"chunk-{chunk_idx}.jsonl")
            with open(out_file, "wb") as f:
                for i in range(n_rows):
                    raw_row = {col: rows[col][i] for col in col_names}
                    entity_name = (
                        str(raw_row.get(entity_name_field, ""))
                        if entity_name_field
                        else ""
                    )
                    record = {
                        "typename": typename,
                        "connection_qualified_name": connection_qualified_name,
                        "workflow_id": workflow_id,
                        "workflow_run_id": workflow_run_id,
                        "extracted_at": extracted_at,
                        "tenant_id": tenant_id,
                        "entity_name": entity_name,
                        "raw_record": json.dumps(raw_row, default=str),
                    }
                    f.write(orjson.dumps(record, option=orjson.OPT_APPEND_NEWLINE))

            chunk_idx += 1
            logger.info(
                f"Prepared {n_rows} raw rows for lakehouse: "
                f"typename={typename}, file={out_file}"
            )

    return base_dir
