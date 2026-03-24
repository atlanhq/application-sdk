"""Lakehouse loading activities — submit MDLH /load jobs and prepare raw data.

This module contains the core implementation for:
- submit_and_poll_mdlh_load: POST to MDLH /load, poll until completion
- convert_raw_parquet_to_parquet: enrich raw parquet with metadata columns
"""

import asyncio
import os
from time import time
from typing import Any, Dict, List

import aiohttp

from application_sdk.activities.common.models import (
    ActivityStatistics,
    LhLoadRequest,
    LhLoadResponse,
    LhLoadStatusResponse,
)
from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.common.error_codes import ActivityError
from application_sdk.common.file_ops import SafeFileOps
from application_sdk.constants import (
    APP_TENANT_ID,
    LH_LOAD_MAX_POLL_ATTEMPTS,
    LH_LOAD_POLL_INTERVAL_SECONDS,
    MDLH_BASE_URL,
)
from application_sdk.io.utils import download_files
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 405, 422}
_TERMINAL_FAILURE_STATES = {"FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}

# Maps SDK typename to the raw column that holds the entity name.
_RAW_ENTITY_NAME_FIELDS: Dict[str, str] = {
    "database": "database_name",
    "schema": "schema_name",
    "table": "table_name",
    "column": "column_name",
}


# ---------------------------------------------------------------------------
# Tenant-level lakehouse availability check
# ---------------------------------------------------------------------------


_lakehouse_enabled_cache: Dict[str, Any] = {"value": None, "expires_at": 0.0}
_HEALTH_CHECK_TTL_SECONDS = 60


async def check_lakehouse_enabled() -> bool:
    """Check whether the lakehouse service is available on this tenant.

    Hits the MDLH Spring Boot actuator health endpoint.  Returns True if
    MDLH is reachable and healthy, False otherwise.  This lets the SDK
    gracefully skip lakehouse loading on tenants where MDLH is not deployed
    (i.e. lakehouse-secrets / lakehouse-config are not mounted).

    Results are cached for 60 seconds to avoid redundant health checks
    when multiple load calls happen within the same workflow run.
    """
    now = time()
    if (
        _lakehouse_enabled_cache["value"] is not None
        and now < _lakehouse_enabled_cache["expires_at"]
    ):
        return _lakehouse_enabled_cache["value"]

    result = await _fetch_lakehouse_health()
    _lakehouse_enabled_cache["value"] = result
    _lakehouse_enabled_cache["expires_at"] = now + _HEALTH_CHECK_TTL_SECONDS
    return result


async def _fetch_lakehouse_health() -> bool:
    """Hit the MDLH actuator health endpoint and return availability."""
    health_url = f"{MDLH_BASE_URL}/actuator/health"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as session:
            async with session.get(health_url) as resp:
                if resp.status == 200:
                    return True
                logger.info(
                    f"MDLH health check returned {resp.status}, "
                    "lakehouse not available on this tenant"
                )
                return False
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        logger.info(f"MDLH not reachable ({e}), lakehouse not available on this tenant")
        return False


# ---------------------------------------------------------------------------
# MDLH /load: submit + poll
# ---------------------------------------------------------------------------


async def submit_and_poll_mdlh_load(
    workflow_args: Dict[str, Any],
) -> ActivityStatistics:
    """Submit a load job to MDLH and poll until completion.

    Reads ``lh_load_config`` from *workflow_args* containing output_path,
    namespace, table_name, mode, and file_extension.  Builds an S3 glob
    pattern, POSTs to ``/atlan/lh/v1/{tenant}/load``, then polls the
    status endpoint until COMPLETED or a terminal failure state.
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

    # Pre-flight: verify MDLH is deployed on this tenant
    if not await check_lakehouse_enabled():
        logger.info("Lakehouse load skipped — MDLH not available on this tenant")
        return ActivityStatistics(typename="lakehouse-load-skipped")

    s3_prefix = get_object_store_prefix(output_path)
    pattern = f"{s3_prefix}/**/*{file_extension}"

    request = LhLoadRequest(
        file_keys=[],
        patterns=[pattern],
        namespace=namespace,
        table_name=table_name,
        mode=mode,
    )
    request_payload = request.model_dump(by_alias=True, exclude_none=True)

    base_url = f"{MDLH_BASE_URL}/atlan/lh/v1/{APP_TENANT_ID}/load"
    headers = {"X-Atlan-Tenant-Id": APP_TENANT_ID}

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
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

    # Poll for completion — reuse a single session across all poll iterations
    status_url = f"{base_url}/{job_id}/status"
    poll_headers = {**headers, "X-Lakehouse-Job-Id": job_id}
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as poll_session:
        for attempt in range(LH_LOAD_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(LH_LOAD_POLL_INTERVAL_SECONDS)
            try:
                async with poll_session.get(status_url, headers=poll_headers) as resp:
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


# ---------------------------------------------------------------------------
# Raw parquet → enriched parquet (with metadata columns)
# ---------------------------------------------------------------------------


async def convert_raw_parquet_to_parquet(
    workflow_args: Dict[str, Any],
    typenames: List[str],
) -> str:
    """Enrich raw parquet files with metadata columns for lakehouse ingestion.

    Reads metadata from workflow_args:
        - output_path → raw parquet location ({output_path}/raw/{typename}/)
        - workflow_id, workflow_run_id → provenance
        - connection.connection_qualified_name → join key
        - tenant_id from APP_TENANT_ID constant

    For each typename's parquet files, adds metadata columns and a
    ``raw_record`` column (JSON string of the original row) to produce
    the per-connector raw table schema::

        typename, connection_qualified_name, workflow_id, workflow_run_id,
        extracted_at, tenant_id, entity_name, raw_record (JSON string)

    Output parquet files are written to ``{output_path}/raw_lakehouse/{typename}/``.

    Returns the root output directory.
    """
    import json

    output_path = workflow_args.get("output_path", "")
    raw_output_path = os.path.join(output_path, "raw")
    workflow_id = workflow_args.get("workflow_id", "")
    workflow_run_id = workflow_args.get("workflow_run_id", "")
    connection_qualified_name = workflow_args.get("connection", {}).get(
        "connection_qualified_name", ""
    )
    extracted_at = int(time() * 1000)

    if not typenames:
        logger.info("No typenames to process, skipping raw lakehouse preparation")
        return os.path.normpath(os.path.join(raw_output_path, "..", "raw_lakehouse"))

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

            if n_rows == 0:
                continue

            # Build raw_record and entity_name lists vectorized over rows
            raw_records = []
            entity_names = []
            for i in range(n_rows):
                raw_row = {col: rows[col][i] for col in col_names}
                raw_records.append(json.dumps(raw_row, default=str))
                entity_names.append(
                    str(raw_row.get(entity_name_field, "")) if entity_name_field else ""
                )

            enriched = {
                "typename": [typename] * n_rows,
                "connection_qualified_name": [connection_qualified_name] * n_rows,
                "workflow_id": [workflow_id] * n_rows,
                "workflow_run_id": [workflow_run_id] * n_rows,
                "extracted_at": [extracted_at] * n_rows,
                "tenant_id": [APP_TENANT_ID] * n_rows,
                "entity_name": entity_names,
                "raw_record": raw_records,
            }

            import pyarrow as pa
            import pyarrow.parquet as pq

            table = pa.table(enriched)
            out_file = os.path.join(out_dir, f"chunk-{chunk_idx}.parquet")
            pq.write_table(table, out_file)

            chunk_idx += 1
            logger.info(
                f"Prepared {n_rows} raw rows for lakehouse: "
                f"typename={typename}, file={out_file}"
            )

    # Upload parquet files to object store so MDLH can read them from S3
    logger.info(f"Uploading raw_lakehouse parquet files to object store: {base_dir}")
    await ObjectStore.upload_prefix(source=base_dir, destination=base_dir)
    logger.info("raw_lakehouse parquet upload complete")

    return base_dir
