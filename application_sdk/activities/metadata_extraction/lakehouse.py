"""Lakehouse loading activities — submit MDLH /load jobs and prepare raw data.

This module contains the core implementation for:
- submit_and_poll_mdlh_load: POST to MDLH /load, poll until completion
- convert_raw_parquet_to_jsonl: wrap raw parquet rows with metadata columns
"""

import asyncio
import json
import os
from time import time
from typing import Any, Dict, List

import aiohttp
import orjson
from temporalio import activity

from application_sdk.activities.common.models import (
    ActivityStatistics,
    LhLoadRequest,
    LhLoadResponse,
    LhLoadStatusResponse,
)
from application_sdk.activities.common.utils import (
    auto_heartbeater,
    get_object_store_prefix,
)
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

    # Poll for completion
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


# ---------------------------------------------------------------------------
# Raw parquet → common-schema JSONL
# ---------------------------------------------------------------------------


async def convert_raw_parquet_to_jsonl(
    workflow_args: Dict[str, Any],
    typenames: List[str],
) -> str:
    """Convert raw parquet files into common-schema JSONL for lakehouse ingestion.

    Reads metadata from workflow_args:
        - output_path → raw parquet location ({output_path}/raw/{typename}/)
        - workflow_id, workflow_run_id → provenance
        - connection.connection_qualified_name → join key
        - tenant_id from APP_TENANT_ID constant

    For each typename's parquet files, every row is wrapped into the
    per-connector raw table schema::

        typename, connection_qualified_name, workflow_id, workflow_run_id,
        extracted_at, tenant_id, entity_name, raw_record (JSON string)

    Output JSONL files are written to ``{output_path}/raw_lakehouse/{typename}/``.

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
                        "tenant_id": APP_TENANT_ID,
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
