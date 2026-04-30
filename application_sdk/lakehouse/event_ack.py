"""Generic event-acknowledgement Parquet writer.

Writes a Parquet ack file consumed by the automation engine after an app
processes an event batch. The path follows a date-partitioned layout the AE
expects::

    artifacts/<app_name>/<workflow_name>/<yyyy>/<mm>/<dd>/<run_id>/<filename>.parquet

The ack schema is intentionally minimal — one row per event with its
processing status and any error message — so the AE can mark events as
acknowledged without re-reading the lakehouse.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from application_sdk.lakehouse.models import ProcessingResult
from application_sdk.storage.batch import upload_file_from_bytes

logger = logging.getLogger(__name__)

_ACK_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("error_message", pa.string(), nullable=True),
    ]
)


def _ack_path(
    app_name: str,
    workflow_name: str,
    workflow_run_id: str,
    filename: str,
    now: datetime | None = None,
) -> str:
    ts = now or datetime.now(timezone.utc)
    return (
        f"artifacts/{app_name}/{workflow_name}/"
        f"{ts.year}/{ts.month:02d}/{ts.day:02d}/"
        f"{workflow_run_id}/{filename}"
    )


class EventAckWriter:
    """Writes a Parquet ack file for a processed event batch."""

    def __init__(
        self,
        app_name: str,
        workflow_name: str,
        filename: str = "events_ack.parquet",
    ) -> None:
        self._app_name = app_name
        self._workflow_name = workflow_name
        self._filename = filename

    async def write(
        self,
        events: list[dict[str, Any]],
        results: list[ProcessingResult],
        workflow_run_id: str,
    ) -> str:
        """Serialize the ack rows to Parquet and upload them. Returns the path."""
        ack_path = _ack_path(
            self._app_name, self._workflow_name, workflow_run_id, self._filename
        )
        arrow = _build_ack_arrow(events, results)

        buf = io.BytesIO()
        pq.write_table(arrow, buf)
        await upload_file_from_bytes(key=ack_path, content=buf.getvalue())
        logger.info("Wrote event ack: %d rows → %s", arrow.num_rows, ack_path)
        return ack_path


def _build_ack_arrow(
    events: list[dict[str, Any]],
    results: list[ProcessingResult],
) -> pa.Table:
    """Internal: build the ack arrow table from parallel events / results lists."""
    if len(events) != len(results):
        raise ValueError(
            f"events ({len(events)}) and results ({len(results)}) must align"
        )
    return pa.table(
        {
            "event_id": [e["event_id"] for e in events],
            "status": [r.status for r in results],
            "error_message": [r.error_message for r in results],
        },
        schema=_ACK_SCHEMA,
    )
