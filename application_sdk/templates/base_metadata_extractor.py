"""Base metadata extraction App — v3 implementation.

Provides the deprecated ``upload_to_atlan`` task as a thin redirect to
:meth:`App.upload`.  Subclasses must define their own ``run()`` with their
own Input/Output contracts.
"""

from __future__ import annotations

import warnings
from typing import Any

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

    .. deprecated::
        Will be removed in v4.0.0. Use
        :class:`application_sdk.templates.SqlApp` instead.

    ``upload_to_atlan`` is preserved as a thin deprecated wrapper so existing
    connectors continue to work.  Subclasses must implement ``run()`` with their
    own Input/Output contracts.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Skip intra-SDK chains (SqlMetadataExtractor, SqlQueryExtractor, …)
        # and skip when a more-specific deprecated parent already warned
        # — so a connector subclassing SqlMetadataExtractor sees one
        # warning, not two.
        if cls.__module__.startswith("application_sdk."):
            return
        if BaseMetadataExtractor not in cls.__bases__:
            return
        warnings.warn(
            f"{cls.__name__} subclasses BaseMetadataExtractor which is deprecated. "
            "Use application_sdk.templates.SqlApp instead. "
            "Will be removed in v4.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )

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
