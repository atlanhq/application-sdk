"""Unit tests for BaseMetadataExtractor template."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from application_sdk.app.base import App
from application_sdk.app.task import is_task
from application_sdk.common.error_codes import ActivityError
from application_sdk.contracts.base import Input, Output
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

    def test_has_client_class_attribute(self) -> None:
        from application_sdk.clients.base import BaseClient

        assert BaseMetadataExtractor.client_class is BaseClient

    def test_has_transformer_class_none_by_default(self) -> None:
        assert BaseMetadataExtractor.transformer_class is None

    def test_upload_to_atlan_accepts_upload_input(self) -> None:
        from typing import get_type_hints

        hints = get_type_hints(BaseMetadataExtractor.upload_to_atlan)
        assert hints.get("input") is UploadInput

    def test_upload_to_atlan_returns_upload_output(self) -> None:
        from typing import get_type_hints

        hints = get_type_hints(BaseMetadataExtractor.upload_to_atlan)
        assert hints.get("return") is UploadOutput


# ---------------------------------------------------------------------------
# Functional tests for upload_to_atlan
# ---------------------------------------------------------------------------

MODULE = "application_sdk.templates.base_metadata_extractor"


class TestUploadToAtlan:
    @pytest.fixture
    def extractor(self):
        return _ConcreteExtractor()

    @patch(f"{MODULE}.create_store_from_binding")
    @patch(f"{MODULE}.list_keys")
    @patch(f"{MODULE}.download_file")
    @patch(f"{MODULE}.upload_file")
    async def test_success(
        self, mock_upload, mock_download, mock_list_keys, mock_create_store, extractor
    ):
        """All files migrate successfully → UploadOutput with correct counts."""
        mock_create_store.return_value = object()
        mock_list_keys.return_value = [f"file{i}.json" for i in range(10)]
        mock_download.return_value = None
        mock_upload.return_value = "sha256sum"

        result = await extractor.upload_to_atlan(
            UploadInput(output_path="/tmp/test/output")
        )

        assert isinstance(result, UploadOutput)
        assert result.migrated_files == 10
        assert result.total_files == 10
        assert mock_upload.call_count == 10

    @patch(f"{MODULE}.create_store_from_binding")
    @patch(f"{MODULE}.list_keys")
    @patch(f"{MODULE}.download_file")
    @patch(f"{MODULE}.upload_file")
    async def test_partial_failures_raise(
        self, mock_upload, mock_download, mock_list_keys, mock_create_store, extractor
    ):
        """Any upload failure raises ActivityError with failure counts."""
        mock_create_store.return_value = object()
        mock_list_keys.return_value = ["file1.json", "file2.json", "file3.json"]
        mock_download.return_value = None
        mock_upload.side_effect = [
            "sha256sum",
            Exception("Connection timeout"),
            Exception("Permission denied"),
        ]

        with pytest.raises(ActivityError) as exc_info:
            await extractor.upload_to_atlan(UploadInput(output_path="/tmp/test/output"))

        assert "Atlan upload failed with 2 errors" in str(exc_info.value)
        assert "Failed migrations: 2" in str(exc_info.value)

    @patch(f"{MODULE}.create_store_from_binding")
    @patch(f"{MODULE}.list_keys")
    async def test_no_files(self, mock_list_keys, mock_create_store, extractor):
        """Empty prefix returns zero counts without error."""
        mock_create_store.return_value = object()
        mock_list_keys.return_value = []

        result = await extractor.upload_to_atlan(
            UploadInput(output_path="/tmp/test/output")
        )

        assert isinstance(result, UploadOutput)
        assert result.migrated_files == 0
        assert result.total_files == 0

    @patch(f"{MODULE}.create_store_from_binding")
    @patch(f"{MODULE}.list_keys")
    async def test_uses_output_path_as_prefix(
        self, mock_list_keys, mock_create_store, extractor
    ):
        """list_keys is called with the exact output_path from UploadInput."""
        mock_create_store.return_value = object()
        mock_list_keys.return_value = []

        await extractor.upload_to_atlan(UploadInput(output_path="artifacts/run-42"))

        mock_list_keys.assert_called_once()
        call_args = mock_list_keys.call_args
        assert call_args.args[0] == "artifacts/run-42"
