"""Base metadata extraction App — v3 implementation.

Provides the deprecated ``upload_to_atlan`` task as a thin redirect to
:meth:`App.upload`.  Subclasses must define their own ``run()`` with their
own Input/Output contracts.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

from typing_extensions import deprecated

from application_sdk import _deprecation
from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.contracts.storage import UploadInput as _UploadInput
from application_sdk.contracts.types import StorageTier
from application_sdk.templates.contracts.base_metadata_extraction import (
    UploadToAtlanInput,
    UploadToAtlanOutput,
)

__all__ = [
    "BaseMetadataExtractor",
]

#: This module imported these contracts under their old names, so they were
#: importable from here. name -> (replacement, why).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "UploadInput": (
        "application_sdk.templates.contracts.base_metadata_extraction.UploadToAtlanInput",
        "the same class, renamed so it no longer shares a name with application_sdk.contracts.storage.UploadInput",
    ),
    "UploadOutput": (
        "application_sdk.templates.contracts.base_metadata_extraction.UploadToAtlanOutput",
        "the same class, renamed so it no longer shares a name with application_sdk.contracts.storage.UploadOutput",
    ),
}

if TYPE_CHECKING:
    UploadInput = UploadToAtlanInput
    UploadOutput = UploadToAtlanOutput

__getattr__ = _deprecation.deprecated_module_getattr(
    __name__, _DEPRECATED_CONSTANTS, remove_in="4.0.0"
)


class BaseMetadataExtractor(App):
    """Base App for all metadata extraction connectors.

    .. deprecated::
        Will be removed in v4.0.0. Use :class:`application_sdk.app.App`
        for non-SQL sources, or :class:`application_sdk.templates.SqlApp`
        for SQL sources, instead.

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
            "Use application_sdk.app.App for non-SQL sources, or "
            "application_sdk.templates.SqlApp for SQL sources, instead. "
            "Will be removed in v4.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )

    @deprecated(
        "upload_to_atlan is deprecated and will be removed in v4.0.0. "
        "Migrate to App.upload(UploadInput(local_path=..., tier=StorageTier.RETAINED)). "
        "See docs/concepts/file-reference.md for migration guidance."
    )
    @task(timeout_seconds=1800)
    async def upload_to_atlan(self, input: UploadToAtlanInput) -> UploadToAtlanOutput:
        """Deprecated: thin redirect to :meth:`App.upload`.

        Forwards ``input.output_path`` as ``local_path`` on the storage
        ``UploadInput`` with ``tier=StorageTier.RETAINED``.
        """
        result = await self.upload(
            _UploadInput(local_path=input.output_path, tier=StorageTier.RETAINED)
        )
        count = result.ref.file_count or 0
        return UploadToAtlanOutput(migrated_files=count, total_files=count)
