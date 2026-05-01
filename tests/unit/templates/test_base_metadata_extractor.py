"""Unit tests for BaseMetadataExtractor template."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from application_sdk.app.base import App
from application_sdk.app.task import is_task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.storage import UploadInput as StorageUploadInput
from application_sdk.contracts.storage import UploadOutput as StorageUploadOutput
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.templates.base_metadata_extractor import BaseMetadataExtractor
from application_sdk.templates.contracts.base_metadata_extraction import (
    UploadInput,
    UploadOutput,
)

# ---------------------------------------------------------------------------
# Minimal concrete subclass (run() is required by App)
# ---------------------------------------------------------------------------


@dataclass
class _TestInput(Input):
    output_path: str = ""


@dataclass
class _TestOutput(Output):
    pass


class _ConcreteExtractor(BaseMetadataExtractor):
    async def run(self, input: _TestInput) -> _TestOutput:
        return _TestOutput()


# ---------------------------------------------------------------------------
# Structure tests
# ---------------------------------------------------------------------------


class TestBaseMetadataExtractorStructure:
    def test_is_app_subclass(self) -> None:
        assert issubclass(BaseMetadataExtractor, App)

    def test_has_upload_to_atlan_task(self) -> None:
        assert is_task(BaseMetadataExtractor.upload_to_atlan)

    def test_upload_to_atlan_accepts_upload_input(self) -> None:
        from typing import get_type_hints

        hints = get_type_hints(BaseMetadataExtractor.upload_to_atlan)
        assert hints.get("input") is UploadInput

    def test_upload_to_atlan_returns_upload_output(self) -> None:
        from typing import get_type_hints

        hints = get_type_hints(BaseMetadataExtractor.upload_to_atlan)
        assert hints.get("return") is UploadOutput


# ---------------------------------------------------------------------------
# Functional tests for upload_to_atlan — verifies the redirect to App.upload
# ---------------------------------------------------------------------------


class TestUploadToAtlan:
    @pytest.fixture
    def extractor(self):
        return _ConcreteExtractor()

    async def test_redirects_to_app_upload(self, extractor):
        """upload_to_atlan delegates to self.upload with the correct StorageUploadInput."""
        extractor.upload = AsyncMock(
            return_value=StorageUploadOutput(
                ref=FileReference(file_count=10), synced=True, reason="uploaded"
            )
        )

        result = await extractor.upload_to_atlan(
            UploadInput(output_path="/tmp/test/output")
        )

        extractor.upload.assert_awaited_once()
        forwarded = extractor.upload.await_args.args[0]
        assert isinstance(forwarded, StorageUploadInput)
        assert forwarded.local_path == "/tmp/test/output"
        assert forwarded.tier is StorageTier.RETAINED

        assert isinstance(result, UploadOutput)
        assert result.migrated_files == 10
        assert result.total_files == 10

    async def test_propagates_upload_exception(self, extractor):
        """If self.upload raises, upload_to_atlan propagates the exception."""
        extractor.upload = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await extractor.upload_to_atlan(UploadInput(output_path="/tmp/x"))
