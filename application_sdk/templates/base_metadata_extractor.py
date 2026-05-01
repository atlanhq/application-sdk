"""Base metadata extraction App — v3 implementation.

Provides the upload_to_atlan task and client/handler/transformer class
attributes for non-SQL (REST/API-based) metadata extraction connectors.

Subclasses must define their own run() with their own Input/Output contracts::

    from dataclasses import dataclass
    from application_sdk.app import task
    from application_sdk.contracts.base import Input, Output
    from application_sdk.templates import BaseMetadataExtractor
    from application_sdk.templates.contracts.base_metadata_extraction import UploadInput

    @dataclass
    class MyInput(Input):
        output_path: str = ""
        # add connector-specific fields here

    @dataclass
    class MyOutput(Output):
        pass

    class MyConnectorExtractor(BaseMetadataExtractor):
        async def run(self, input: MyInput) -> MyOutput:
            # fetch data, transform, then upload
            await self.upload_to_atlan(UploadInput(output_path=input.output_path))
            return MyOutput()

Client, handler, and transformer are cached in self.app_state after the first
task that initialises them — use self.app_state.get/set("client", ...) in your
@task methods rather than re-initialising on every call.
"""

from __future__ import annotations

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.contracts.storage import UploadInput as _UploadInput
from application_sdk.contracts.types import StorageTier
from application_sdk.templates.contracts.base_metadata_extraction import (
    UploadInput,
    UploadOutput,
)


class BaseMetadataExtractor(App):
    """Base App for all metadata extraction connectors.

    Provides ``upload_to_atlan`` as a thin redirect to ``App.upload`` so the
    deprecated entry point and the supported one share a single transfer
    implementation.

    Subclasses must implement ``run()`` with their own Input/Output contracts.
    See module docstring for a usage example.
    """

    @task(timeout_seconds=1800)
    async def upload_to_atlan(self, input: UploadInput) -> UploadOutput:
        """Upload output files to the platform via :meth:`App.upload`."""
        result = await self.upload(
            _UploadInput(local_path=input.output_path, tier=StorageTier.RETAINED)
        )
        count = result.ref.file_count or 0
        return UploadOutput(migrated_files=count, total_files=count)
