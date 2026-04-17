"""Lakehouse consumer poll loop."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from application_sdk.lakehouse.catalog_client import PolarisCatalogClient
from application_sdk.lakehouse.models import ProcessingResult
from application_sdk.lakehouse.outcome_writer import OutcomeWriter
from application_sdk.lakehouse.work_query import WorkQueryRunner

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog

    from application_sdk.lakehouse.protocols import BatchProcessor

logger = logging.getLogger(__name__)


class LakehouseConsumer:
    """Poll-based consumer for lakehouse event tables."""

    def __init__(
        self,
        processor: "BatchProcessor",
        catalog: "Catalog",
        namespace: str,
        table_name: str,
        max_retries: int = 5,
        batch_limit: int = 1000,
    ) -> None:
        self._processor = processor
        self._catalog = catalog
        self._namespace = namespace
        self._table_name = table_name
        self._max_retries = max_retries
        self._batch_limit = batch_limit

    async def run(
        self,
        max_duration_seconds: float = 3480,
        poll_interval_seconds: float = 60,
    ) -> None:
        catalog_client = PolarisCatalogClient(
            self._catalog,
            self._namespace,
            self._table_name,
        )
        work_query = WorkQueryRunner(max_retries=self._max_retries)
        last_snapshot_id: int | None = None
        start_time = time.monotonic()

        await self._processor.setup()
        try:
            while (time.monotonic() - start_time) < max_duration_seconds:
                try:
                    if not catalog_client.has_changed(last_snapshot_id):
                        logger.debug("No snapshot change, sleeping")
                        await asyncio.sleep(poll_interval_seconds)
                        continue

                    last_snapshot_id = catalog_client.get_current_snapshot_id()
                    table = catalog_client.load_table()
                    outcome_writer = OutcomeWriter(table)

                    scan = table.scan()
                    arrow_table = scan.to_arrow()

                    if arrow_table.num_rows == 0:
                        logger.debug("No rows in scan window, sleeping")
                        await asyncio.sleep(poll_interval_seconds)
                        continue

                    events = work_query.run(arrow_table, limit=self._batch_limit)
                    if not events:
                        logger.debug("No pending events, sleeping")
                        await asyncio.sleep(poll_interval_seconds)
                        continue

                    logger.info("Processing %d events", len(events))

                    try:
                        results = await self._processor.process_batch(events)
                    except Exception:
                        logger.error(
                            "Unhandled error in process_batch, marking all %d events as RETRY",
                            len(events),
                            exc_info=True,
                        )
                        results = [
                            ProcessingResult(
                                status="RETRY",
                                error_message="batch processing failed",
                            )
                            for _ in events
                        ]

                    if len(results) != len(events):
                        logger.error(
                            "process_batch returned %d results for %d events, marking all as RETRY",
                            len(results),
                            len(events),
                        )
                        results = [
                            ProcessingResult(
                                status="RETRY",
                                error_message="result count mismatch",
                            )
                            for _ in events
                        ]

                    for event, result in zip(events, results):
                        outcome_writer.add_outcome(
                            event,
                            result,
                            retry_count=event.get("current_retry_count", 0),
                        )

                    flushed = outcome_writer.flush()
                    logger.info("Wrote %d outcome rows", flushed)

                    if len(events) >= self._batch_limit:
                        continue

                except asyncio.CancelledError:
                    logger.info("Consumer cancelled, exiting")
                    raise
                except Exception:
                    logger.error("Error in poll loop iteration", exc_info=True)

                await asyncio.sleep(poll_interval_seconds)

        finally:
            await self._processor.teardown()
            logger.info("Consumer shut down")
