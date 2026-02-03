#!/usr/bin/env python3
"""
Observability App - FastAPI service for workflow log streaming and querying.

Pure streaming design - no log storage in memory.
Logs are only streamed to active SSE subscribers, then discarded.
Historical queries go directly to Iceberg.

Endpoints:
- POST /v1/logs: OTLP log receiver (from collector otlphttp exporter)
- GET /sse/logs?trace_id=xxx: Server-Sent Events for live log streaming
- GET /api/logs?trace_id=xxx&application_name=yyy: Query logs from Iceberg
- GET /api/traces: List recently seen trace IDs
- GET /health: Health check

Filters:
- trace_id: Filter by trace/workflow ID
- application_name: Filter by application (e.g., 'mssql', 'postgres')
"""

import asyncio
import gzip
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# Configuration from environment
PORT = int(os.environ.get("PORT", "8080"))
POLARIS_CATALOG_URI = os.environ.get("POLARIS_CATALOG_URI", "")
POLARIS_CRED = os.environ.get("POLARIS_CRED", "")
CATALOG_NAME = os.environ.get("CATALOG_NAME", "context_store")
WAREHOUSE_NAME = os.environ.get("WAREHOUSE_NAME", "context_store")
NAMESPACE = os.environ.get("NAMESPACE", "Workflow_Log_Test")

app = FastAPI(
    title="Workflow Logs Observability",
    description="Live streaming and historical query service for workflow logs",
    version="1.0.0",
)

# Active subscribers per workflow_id
# workflow_id -> set of asyncio.Queue (one per subscriber)
subscribers: Dict[str, Set[asyncio.Queue]] = defaultdict(set)
# Track which workflows have been seen (for /api/workflows)
active_workflows: Dict[str, float] = {}  # workflow_id -> last_seen_timestamp


# ============================================
# OTLP JSON Models
# ============================================
class OTLPAttribute(BaseModel):
    key: str
    value: Dict[str, Any]


class OTLPLogRecord(BaseModel):
    timeUnixNano: Optional[str] = None
    observedTimeUnixNano: Optional[str] = None
    severityNumber: Optional[int] = None
    severityText: Optional[str] = None
    body: Optional[Dict[str, Any]] = None
    attributes: Optional[List[OTLPAttribute]] = None
    traceId: Optional[str] = None
    spanId: Optional[str] = None


class OTLPScopeLog(BaseModel):
    scope: Optional[Dict[str, Any]] = None
    logRecords: Optional[List[OTLPLogRecord]] = None


class OTLPResourceLog(BaseModel):
    resource: Optional[Dict[str, Any]] = None
    scopeLogs: Optional[List[OTLPScopeLog]] = None


class OTLPLogsRequest(BaseModel):
    resourceLogs: List[OTLPResourceLog]


# ============================================
# Helper Functions
# ============================================
def extract_attrs(attrs_list: Optional[List[OTLPAttribute]]) -> Dict[str, Any]:
    """Convert OTLP attributes list to dict."""
    if not attrs_list:
        return {}
    result = {}
    for attr in attrs_list:
        key = attr.key
        value = attr.value
        if "stringValue" in value:
            result[key] = value["stringValue"]
        elif "intValue" in value:
            result[key] = int(value["intValue"])
        elif "doubleValue" in value:
            result[key] = float(value["doubleValue"])
        elif "boolValue" in value:
            result[key] = value["boolValue"]
    return result


def parse_otlp_logs(data: OTLPLogsRequest) -> List[Dict[str, Any]]:
    """Parse OTLP logs request into normalized log records."""
    records = []

    for resource_log in data.resourceLogs:
        # Extract resource attributes
        resource_attrs = {}
        if resource_log.resource and resource_log.resource.get("attributes"):
            resource_attrs = extract_attrs(
                [OTLPAttribute(**a) for a in resource_log.resource["attributes"]]
            )

        for scope_log in resource_log.scopeLogs or []:
            for log_record in scope_log.logRecords or []:
                # Extract log attributes
                log_attrs = extract_attrs(log_record.attributes)

                # Get timestamp
                time_nano = (
                    log_record.timeUnixNano or log_record.observedTimeUnixNano or "0"
                )
                try:
                    timestamp = int(time_nano) / 1e9
                except (ValueError, TypeError):
                    timestamp = time.time()

                # Get message
                body = log_record.body or {}
                message = (
                    body.get("stringValue", "") if isinstance(body, dict) else str(body)
                )

                # Get trace_id (primary identifier for streaming)
                trace_id = (
                    log_attrs.get("trace_id")
                    or log_attrs.get("workflow_id")
                    or log_attrs.get("atlan-argo-workflow-name")
                    or "unknown"
                )

                # Get application_name from resource attributes
                application_name = resource_attrs.get("application.name", "unknown")

                # Build normalized record
                record = {
                    "timestamp": timestamp,
                    "timestamp_iso": datetime.utcfromtimestamp(timestamp).isoformat()
                    + "Z",
                    "level": log_record.severityText or "INFO",
                    "message": message,
                    "trace_id": trace_id,
                    "application_name": application_name,
                    "service_name": resource_attrs.get("service.name", "unknown"),
                    "activity_id": log_attrs.get("activity_id"),
                    "run_id": log_attrs.get("run_id"),
                    "attributes": log_attrs,
                }
                records.append(record)

    return records


async def broadcast_to_subscribers(
    workflow_id: str, records: List[Dict[str, Any]]
) -> int:
    """
    Broadcast log records to all active subscribers for a workflow.
    Returns number of subscribers that received the logs.
    """
    if workflow_id not in subscribers:
        return 0

    subscriber_queues = subscribers[workflow_id]
    if not subscriber_queues:
        return 0

    # Send to all subscribers
    for record in records:
        for queue in subscriber_queues:
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                # Subscriber is too slow, skip this log for them
                pass

    return len(subscriber_queues)


# ============================================
# Iceberg Query
# ============================================
_catalog = None
_table = None


def get_iceberg_table():
    """Get Iceberg table connection (lazy initialization)."""
    global _catalog, _table

    if not POLARIS_CATALOG_URI or not POLARIS_CRED:
        return None

    if _table is not None:
        return _table

    try:
        from pyiceberg.catalog import load_catalog

        _catalog = load_catalog(
            CATALOG_NAME,
            uri=POLARIS_CATALOG_URI,
            credential=POLARIS_CRED,
            warehouse=WAREHOUSE_NAME,
            scope="PRINCIPAL_ROLE:lake_writers",
        )
        table_name = f"{NAMESPACE}.workflow_logs_entity"
        _table = _catalog.load_table(table_name)
        return _table
    except Exception as e:
        print(f"Failed to connect to Iceberg: {e}")
        return None


def query_iceberg_logs(
    trace_id: Optional[str] = None,
    application_name: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Query logs from Iceberg by trace_id and/or application_name."""
    table = get_iceberg_table()
    if table is None:
        return []

    try:
        import pyarrow.compute as pc
        from pyiceberg.expressions import And, EqualTo

        # Build filter based on provided parameters
        filters = []
        if trace_id:
            filters.append(EqualTo("trace_id", trace_id))
        if application_name:
            filters.append(EqualTo("application_name", application_name))

        # Combine filters with AND
        row_filter = None
        if len(filters) == 1:
            row_filter = filters[0]
        elif len(filters) > 1:
            row_filter = And(*filters)

        # Scan with predicate pushdown
        scan = table.scan(
            row_filter=row_filter,
            selected_fields=(
                "timestamp",
                "level",
                "message",
                "logger_name",
                "trace_id",
                "application_name",
                "atlan_argo_workflow_name",
            ),
        )

        # Execute
        arrow_table = scan.to_arrow()

        # Sort by timestamp descending and limit
        if len(arrow_table) > 0:
            indices = pc.sort_indices(
                arrow_table, sort_keys=[("timestamp", "descending")]
            )
            if len(indices) > limit:
                indices = indices[:limit]
            result = arrow_table.take(indices)
            return result.to_pylist()
        return []
    except Exception as e:
        print(f"Iceberg query error: {e}")
        return []


# ============================================
# API Endpoints
# ============================================
@app.get("/health")
async def health():
    """Health check endpoint."""
    total_subscribers = sum(len(s) for s in subscribers.values())
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "active_traces": len(active_workflows),
        "total_subscribers": total_subscribers,
        "iceberg_connected": get_iceberg_table() is not None,
    }


@app.post("/v1/logs")
async def receive_logs(request: Request):
    """
    OTLP log receiver endpoint.

    Receives logs from OTEL collector's otlphttp exporter.
    Broadcasts to active SSE subscribers, then discards (no storage).
    """
    try:
        # Read raw body
        raw_body = await request.body()

        # Check if content is gzip-compressed
        content_encoding = request.headers.get("content-encoding", "").lower()
        if content_encoding == "gzip" or (
            len(raw_body) >= 2 and raw_body[0:2] == b"\x1f\x8b"
        ):
            raw_body = gzip.decompress(raw_body)

        # Parse JSON
        body = json.loads(raw_body.decode("utf-8"))
        data = OTLPLogsRequest(**body)
        records = parse_otlp_logs(data)

        # Group by trace_id and broadcast to subscribers
        by_trace: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for record in records:
            trace_id = record["trace_id"]
            by_trace[trace_id].append(record)
            # Track active workflows
            active_workflows[trace_id] = time.time()

        total_delivered = 0
        for trace_id, trace_records in by_trace.items():
            delivered = await broadcast_to_subscribers(trace_id, trace_records)
            total_delivered += delivered * len(trace_records)

        return JSONResponse(
            status_code=200,
            content={
                "accepted": len(records),
                "delivered_to_subscribers": total_delivered,
            },
        )
    except Exception as e:
        print(f"Error receiving logs: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/sse/logs")
async def stream_logs(
    trace_id: str = Query(..., description="Trace ID to stream logs for"),
):
    """
    Server-Sent Events endpoint for live log streaming.

    Pure streaming - only receives logs that arrive AFTER connection.
    No history replay. For historical logs, use /api/logs with source=iceberg.
    """

    async def event_generator() -> AsyncGenerator[dict, None]:
        # Create a queue for this subscriber
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        subscribers[trace_id].add(queue)

        try:
            # Send connection confirmation
            yield {
                "event": "connected",
                "data": json.dumps(
                    {
                        "trace_id": trace_id,
                        "timestamp": time.time(),
                        "message": "Streaming started. Only new logs will be shown.",
                    }
                ),
            }

            while True:
                try:
                    # Wait for new log with timeout
                    log = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": "log",
                        "data": json.dumps(log),
                    }
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield {
                        "event": "keepalive",
                        "data": json.dumps({"timestamp": time.time()}),
                    }

        finally:
            # Remove subscriber on disconnect
            subscribers[trace_id].discard(queue)
            if not subscribers[trace_id]:
                del subscribers[trace_id]

    return EventSourceResponse(event_generator())


@app.get("/api/logs")
async def query_logs(
    trace_id: Optional[str] = Query(None, description="Trace ID to filter logs"),
    application_name: Optional[str] = Query(
        None, description="Application name to filter logs (e.g., 'mssql')"
    ),
    limit: int = Query(100, description="Maximum number of logs to return"),
    source: str = Query("iceberg", description="Source: 'iceberg' (historical)"),
):
    """
    Query historical logs from Iceberg.

    Filter by trace_id and/or application_name.
    Note: Live streaming does not store logs. Use /sse/logs for real-time streaming.
    """
    if source != "iceberg":
        raise HTTPException(
            status_code=400,
            detail=f"Invalid source: {source}. Only 'iceberg' is supported. Use /sse/logs for live streaming.",
        )

    if not trace_id and not application_name:
        raise HTTPException(
            status_code=400,
            detail="At least one filter required: trace_id or application_name",
        )

    logs = query_iceberg_logs(
        trace_id=trace_id, application_name=application_name, limit=limit
    )
    return {
        "logs": logs,
        "count": len(logs),
        "source": "iceberg",
        "filters": {
            "trace_id": trace_id,
            "application_name": application_name,
        },
    }


@app.get("/api/traces")
async def list_traces():
    """List trace IDs that have been seen recently (last 5 minutes)."""
    now = time.time()
    cutoff = now - 300  # 5 minutes

    traces = []
    for trace_id, last_seen in active_workflows.items():
        if last_seen > cutoff:
            traces.append(
                {
                    "trace_id": trace_id,
                    "last_seen": last_seen,
                    "last_seen_iso": datetime.utcfromtimestamp(last_seen).isoformat()
                    + "Z",
                    "active_subscribers": len(subscribers.get(trace_id, set())),
                }
            )

    # Clean up old entries
    for trace_id in list(active_workflows.keys()):
        if active_workflows[trace_id] <= cutoff:
            del active_workflows[trace_id]

    return {"traces": traces}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
