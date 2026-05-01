"""Base metadata extraction App — v3 implementation.

Provides the deprecated ``upload_to_atlan`` task as a thin redirect to
:meth:`App.upload`.  Subclasses must define their own ``run()`` with their
own Input/Output contracts.
"""

from __future__ import annotations

from typing_extensions import deprecated

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

    ``upload_to_atlan`` is preserved as a thin deprecated wrapper so existing
    connectors continue to work.  Subclasses must implement ``run()`` with their
    own Input/Output contracts.
    """

    @deprecated(
        "upload_to_atlan is deprecated and will be removed in the next major SDK release. "
        "Migrate to App.upload(UploadInput(local_path=..., tier=StorageTier.RETAINED)). "
        "See docs/concepts/file-reference.md for migration guidance."
    )
    @task(timeout_seconds=1800)
    async def upload_to_atlan(self, input: UploadInput) -> UploadOutput:
        """Deprecated: thin redirect to :meth:`App.upload`.

        Forwards ``input.output_path`` as ``local_path`` on the storage
        ``UploadInput`` with ``tier=StorageTier.RETAINED``.
        """
        result = await self.upload(
            _UploadInput(local_path=input.output_path, tier=StorageTier.RETAINED)
        )
        count = result.ref.file_count or 0
        return UploadOutput(migrated_files=count, total_files=count)
