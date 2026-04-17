"""Protocols for the lakehouse consumer framework."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from application_sdk.lakehouse.models import ProcessingResult


@runtime_checkable
class BatchProcessor(Protocol):
    """Protocol that source apps implement to process lakehouse events."""

    async def setup(self) -> None: ...

    async def process_batch(
        self, events: list[dict[str, Any]]
    ) -> list[ProcessingResult]: ...

    async def teardown(self) -> None: ...
