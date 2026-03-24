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

    Positive results are cached for 60 seconds to avoid redundant health
    checks when multiple load calls happen within the same workflow run.
    Negative results are never cached so transient outages (deploys, network
    blips) don't cause a 60-second window of silently skipped loads.
    """
    now = time()
    if (
        _lakehouse_enabled_cache["value"] is True
        and now < _lakehouse_enabled_cache["expires_at"]
    ):
        return True

    result = await _fetch_lakehouse_health()
    if result:
        _lakehouse_enabled_cache["value"] = True
        _lakehouse_enabled_cache["expires_at"] = now + _HEALTH_CHECK_TTL_SECONDS
    else:
        # Clear cache so next call retries immediately
        _lakehouse_enabled_cache["value"] = None
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

    # Poll for completion — reuse a single session; timeout is per-request, not session-wide
    status_url = f"{base_url}/{job_id}/status"
    poll_headers = {**headers, "X-Lakehouse-Job-Id": job_id}
    async with aiohttp.ClientSession() as poll_session:
        for attempt in range(LH_LOAD_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(LH_LOAD_POLL_INTERVAL_SECONDS)
            try:
                async with poll_session.get(
                    status_url, headers=poll_headers, timeout=_HTTP_TIMEOUT
                ) as resp:
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
        - output_path -> raw parquet location ({output_path}/raw/{typename}/)
        - workflow_id, workflow_run_id -> provenance
        - connection.connection_qualified_name -> join key
        - tenant_id from APP_TENANT_ID constant

    For each typename's parquet files, adds metadata columns and a
    ``raw_record`` column (JSON string of the original row) to produce
    the per-connector raw table schema::

        typename, connection_qualified_name, workflow_id, workflow_run_id,
        extracted_at, tenant_id, entity_name, raw_record (JSON string)

    Output parquet files are written to ``{output_path}/raw_lakehouse/{typename}/``.
    Typenames are processed in parallel for throughput.

    Returns the root output directory.
    """
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
    SafeFileOps.makedirs(base_dir, exist_ok=True)

    # Process all typenames in parallel — each is independent I/O + CPU work
    results = await asyncio.gather(
        *(
            _enrich_typename(
                typename=typename,
                raw_output_path=raw_output_path,
                base_dir=base_dir,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                connection_qualified_name=connection_qualified_name,
                extracted_at=extracted_at,
            )
            for typename in typenames
        )
    )

    total_files_written = sum(results)

    if total_files_written == 0:
        logger.info("No raw parquet files produced, skipping upload")
        return base_dir

    # Upload parquet files to object store so MDLH can read them from S3
    logger.info(f"Uploading raw_lakehouse parquet files to object store: {base_dir}")
    await ObjectStore.upload_prefix(source=base_dir, destination=base_dir)
    logger.info("raw_lakehouse parquet upload complete")

    return base_dir


async def _enrich_typename(
    typename: str,
    raw_output_path: str,
    base_dir: str,
    workflow_id: str,
    workflow_run_id: str,
    connection_qualified_name: str,
    extracted_at: int,
) -> int:
    """Enrich all parquet files for a single typename. Returns files written.

    Uses a single DuckDB COPY statement per file: read parquet, add metadata
    columns + raw_record via to_json(), write enriched parquet — all in C++
    with zero Python-level data copying.
    """
    import duckdb

    src_dir = os.path.join(raw_output_path, typename)
    parquet_files = await download_files(src_dir, ".parquet", file_names=None)

    if not parquet_files:
        logger.info(f"No raw parquet files for typename={typename}, skipping")
        return 0

    out_dir = os.path.join(base_dir, typename)
    SafeFileOps.makedirs(out_dir, exist_ok=True)

    entity_name_field = _RAW_ENTITY_NAME_FIELDS.get(typename)
    chunk_idx = 0
    total_rows = 0

    # Single DuckDB connection for all files of this typename
    conn = duckdb.connect()
    try:
        for pq_file in parquet_files:
            out_file = os.path.join(out_dir, f"chunk-{chunk_idx}.parquet")
            try:
                n_rows = _duckdb_enrich_file(
                    conn=conn,
                    pq_file=pq_file,
                    out_file=out_file,
                    typename=typename,
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run_id,
                    connection_qualified_name=connection_qualified_name,
                    extracted_at=extracted_at,
                    entity_name_field=entity_name_field,
                )
            except Exception as e:
                logger.warning(f"Failed to process parquet {pq_file}: {e}, skipping")
                continue

            if n_rows > 0:
                chunk_idx += 1
                total_rows += n_rows
    finally:
        conn.close()

    if total_rows > 0:
        logger.info(
            f"Prepared {total_rows} raw rows for lakehouse: "
            f"typename={typename}, chunks={chunk_idx}"
        )

    return chunk_idx


def _duckdb_enrich_file(
    conn: Any,
    pq_file: str,
    out_file: str,
    typename: str,
    workflow_id: str,
    workflow_run_id: str,
    connection_qualified_name: str,
    extracted_at: int,
    entity_name_field: str | None,
) -> int:
    """Read a parquet file, enrich with metadata columns, write output parquet.

    Performs the entire read-transform-write in a single DuckDB COPY statement
    so data never crosses the Python/C++ boundary. Returns the number of rows
    written (0 if the input was empty).
    """
    # Escape the file path for SQL string literal
    escaped_input = pq_file.replace("'", "''")
    escaped_output = out_file.replace("'", "''")

    # Peek at column names to build the struct for raw_record
    col_info = conn.sql(
        f"DESCRIBE SELECT * FROM read_parquet('{escaped_input}')"
    ).fetchall()
    col_names = [row[0] for row in col_info]

    if not col_names:
        return 0

    # Build DuckDB struct literal: {'col1': "col1", 'col2': "col2", ...}
    struct_fields = ", ".join(
        f"'{col.replace(chr(39), chr(39) + chr(39))}': "
        f'"{col.replace(chr(34), chr(34) + chr(34))}"'
        for col in col_names
    )

    # entity_name: extract from a known column or empty string
    if entity_name_field and entity_name_field in col_names:
        escaped_field = entity_name_field.replace('"', '""')
        entity_name_sql = f"COALESCE(CAST(\"{escaped_field}\" AS VARCHAR), '')"
    else:
        entity_name_sql = "''"

    # Escape string literals for SQL
    esc_typename = typename.replace("'", "''")
    esc_conn_qn = connection_qualified_name.replace("'", "''")
    esc_wf_id = workflow_id.replace("'", "''")
    esc_wf_run_id = workflow_run_id.replace("'", "''")
    esc_tenant = APP_TENANT_ID.replace("'", "''")

    # Single COPY statement: read parquet -> add columns -> write parquet.
    # All data stays in DuckDB's C++ engine; zero Python-level copying.
    conn.sql(
        f"COPY ("
        f"  SELECT"
        f"    '{esc_typename}' AS typename,"
        f"    '{esc_conn_qn}' AS connection_qualified_name,"
        f"    '{esc_wf_id}' AS workflow_id,"
        f"    '{esc_wf_run_id}' AS workflow_run_id,"
        f"    {extracted_at}::BIGINT AS extracted_at,"
        f"    '{esc_tenant}' AS tenant_id,"
        f"    {entity_name_sql} AS entity_name,"
        f"    to_json({{{struct_fields}}})::VARCHAR AS raw_record"
        f"  FROM read_parquet('{escaped_input}')"
        f") TO '{escaped_output}' (FORMAT PARQUET, COMPRESSION SNAPPY)"
    )

    # Get row count from output file metadata (cheaper than COUNT(*))
    result = conn.sql(
        f"SELECT count FROM parquet_file_metadata('{escaped_output}')"
    ).fetchone()
    return result[0] if result else 0
