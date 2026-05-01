"""Base metadata extraction App — v3 implementation.

Subclasses must define their own run() with their own Input/Output contracts.
Push files to the platform via :meth:`App.upload`::

    await self.upload(UploadInput(local_path=..., tier=StorageTier.RETAINED))
"""

from __future__ import annotations

from application_sdk.app.base import App


class BaseMetadataExtractor(App):
    """Base App for all metadata extraction connectors.

    Subclasses must implement ``run()`` with their own Input/Output contracts.
    """
