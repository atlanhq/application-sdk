"""Unit tests for the deprecated v2 FastAPI utility functions and models.

The behaviour tests below pin what the deprecated names still do until their
v4.0 removal; ``TestDeprecationNotices`` pins that each one warns. The
``download_file_from_upload_response`` tests live with that function, in
``tests/unit/common/test_utils.py``.
"""

import importlib
import inspect
import json
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import UploadFile

from application_sdk.server.fastapi import models as v2_models
from application_sdk.server.fastapi.utils import (
    _build_object_store_key,
    _resolve_extension,
    upload_file_to_object_store,
)

# The behaviour tests call deprecated names on purpose.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_V2_MODELS = (
    "TestAuthRequest",
    "TestAuthResponse",
    "MetadataType",
    "FetchMetadataRequest",
    "FetchMetadataResponse",
    "PreflightCheckRequest",
    "PreflightCheckResponse",
    "WorkflowRequest",
    "EventWorkflowRequest",
    "WorkflowData",
    "WorkflowResponse",
    "EventWorkflowResponse",
    "WorkflowConfigRequest",
    "WorkflowConfigResponse",
    "ConfigMapResponse",
    "Subscription",
    "FileUploadResponse",
)


class TestInternalServerErrorHandler:
    """Test cases for internal_server_error_handler."""

    def test_does_not_leak_exception_details(self):
        """Regression test: 500 handler must not expose internal error details to clients."""
        from application_sdk.server.fastapi.utils import internal_server_error_handler

        sensitive_exc = Exception(
            "OperationalError: connection to host 10.0.1.42:5432 failed: "
            "password authentication failed for user 'admin'"
        )
        response = internal_server_error_handler(None, sensitive_exc)
        body = json.loads(response.body)
        assert response.status_code == 500
        assert body["success"] is False
        assert "10.0.1.42" not in body.get("details", "")
        assert "password" not in body.get("details", "")
        assert body["error"] == "An internal error has occurred."


class TestResolveExtension:
    """Test cases for _resolve_extension helper."""

    def test_extension_from_filename(self):
        """Test that extension is taken from filename when present."""
        assert _resolve_extension("report.csv", "application/octet-stream") == ".csv"

    def test_extension_from_filename_multiple_dots(self):
        """Test that only the final suffix is returned."""
        assert _resolve_extension("archive.tar.gz", "application/octet-stream") == ".gz"

    def test_fallback_to_content_type(self):
        """Test fallback to content type when filename has no extension."""
        assert _resolve_extension("testfile", "text/csv") == ".csv"

    def test_empty_when_no_extension_and_unknown_content_type(self):
        """Test empty string when neither filename nor content type yields an extension."""
        assert _resolve_extension("testfile", "application/x-unknown-type") == ""

    def test_filename_extension_takes_precedence(self):
        """Test that filename extension wins over content type."""
        assert _resolve_extension("data.json", "text/csv") == ".json"


class TestBuildObjectStoreKey:
    """Test cases for _build_object_store_key helper."""

    def test_key_with_prefix(self):
        """Test key includes prefix when provided."""
        assert (
            _build_object_store_key("abc123.csv", "workflow_file_upload")
            == "workflow_file_upload/abc123.csv"
        )

    def test_key_without_prefix_none(self):
        """Test key is just the filename when prefix is None."""
        assert _build_object_store_key("abc123.csv", None) == "abc123.csv"

    def test_key_without_prefix_empty(self):
        """Test key is just the filename when prefix is empty string."""
        assert _build_object_store_key("abc123.csv", "") == "abc123.csv"

    def test_key_with_nested_prefix(self):
        """Test key with a multi-level prefix."""
        assert (
            _build_object_store_key("abc123.csv", "uploads/2024")
            == "uploads/2024/abc123.csv"
        )


class TestUploadFileToObjectStore:
    """Test cases for upload_file_to_object_store utility function."""

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_success(self, mock_objectstore):
        """Test successful file upload."""

        file_content = b"test file content"
        filename = "test.csv"
        content_type = "text/csv"

        # Create mock UploadFile
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        result = await upload_file_to_object_store(
            file=mock_file,
            prefix="workflow_file_upload",
        )

        # Verify ObjectStore was called with upstream object store

        mock_objectstore.assert_called_once()
        call_args = mock_objectstore.call_args
        assert call_args[1]["content"] == file_content
        assert call_args[1]["key"].endswith(".csv")
        assert "workflow_file_upload" in call_args[1]["key"]

        # Verify response structure
        assert result.id is not None
        assert result.version is not None
        assert result.isActive is True
        assert result.createdAt is not None
        assert result.updatedAt is not None
        assert result.fileName is not None
        assert result.rawName == filename
        assert result.key is not None
        assert result.extension == ".csv"
        assert result.contentType == content_type
        assert result.fileSize == len(file_content)
        assert result.isEncrypted is False
        assert result.redirectUrl == ""
        assert result.isUploaded is True
        assert result.uploadedAt is not None
        assert result.isArchived is False

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_with_explicit_filename(self, mock_objectstore):
        """Test that explicit filename parameter takes precedence over file.filename."""

        file_content = b"test file content"
        explicit_filename = "explicit_name.csv"
        file_filename = "file_name.csv"  # Different from explicit
        content_type = "text/csv"

        # Create mock UploadFile with different filename
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = file_filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        result = await upload_file_to_object_store(
            file=mock_file,
            filename=explicit_filename,  # Explicit filename should be used
            prefix="workflow_file_upload",
        )

        # Verify that explicit filename is used, not file.filename
        assert result.rawName == explicit_filename
        assert result.rawName != file_filename

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_filename_fallback(self, mock_objectstore):
        """Test filename fallback: explicit -> file.filename -> 'uploaded_file'."""

        file_content = b"test content"
        content_type = "text/csv"

        # Test 1: No filename provided, file.filename is None -> should use "uploaded_file"
        mock_file1 = MagicMock(spec=UploadFile)
        mock_file1.filename = None
        mock_file1.content_type = content_type
        mock_file1.read = AsyncMock(return_value=file_content)

        result1 = await upload_file_to_object_store(
            file=mock_file1,
            filename=None,
            prefix="workflow_file_upload",
        )
        assert result1.rawName == "uploaded_file"

        # Test 2: No explicit filename, but file.filename exists -> should use file.filename
        mock_file2 = MagicMock(spec=UploadFile)
        mock_file2.filename = "file_name.csv"
        mock_file2.content_type = content_type
        mock_file2.read = AsyncMock(return_value=file_content)

        result2 = await upload_file_to_object_store(
            file=mock_file2,
            filename=None,
            prefix="workflow_file_upload",
        )
        assert result2.rawName == "file_name.csv"

        # Test 3: Explicit filename provided -> should use explicit filename
        mock_file3 = MagicMock(spec=UploadFile)
        mock_file3.filename = "file_name.csv"
        mock_file3.content_type = content_type
        mock_file3.read = AsyncMock(return_value=file_content)

        result3 = await upload_file_to_object_store(
            file=mock_file3,
            filename="explicit_name.csv",
            prefix="workflow_file_upload",
        )
        assert result3.rawName == "explicit_name.csv"

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_raw_name_preserved(self, mock_objectstore):
        """Test that rawName preserves original filename (force=false behavior)."""

        file_content = b"test content"
        filename = "original.txt"
        content_type = "text/plain"

        # Create mock UploadFile
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        result = await upload_file_to_object_store(
            file=mock_file,
            prefix="workflow_file_upload",
        )

        # When force=false (default), rawName should be the original filename
        assert result.rawName == filename
        assert result.rawName != result.fileName  # fileName is UUID-based

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_extension_from_content_type(self, mock_objectstore):
        """Test extension determination from content type when filename has no extension."""

        file_content = b"test content"
        filename = "testfile"  # No extension
        content_type = "text/csv"

        # Create mock UploadFile
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        result = await upload_file_to_object_store(
            file=mock_file,
            prefix="workflow_file_upload",
        )

        # Extension should be determined from content type
        assert result.extension == ".csv"
        assert result.fileName.endswith(".csv")

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_key_generation(self, mock_objectstore):
        """Test that key includes prefix when provided."""

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"

        # Create mock UploadFile
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        result = await upload_file_to_object_store(
            file=mock_file,
            prefix="some-prefix",
        )

        # Key should include prefix: key = prefix + "/" + fileName
        assert result.key == f"some-prefix/{result.fileName}"
        assert "/" in result.key  # Should have path separator
        assert result.key.endswith(".csv")
        assert result.key.startswith("some-prefix/")

        # Verify ObjectStore was called with the key including prefix
        call_args = mock_objectstore.call_args
        assert call_args[1]["key"] == result.key
        assert call_args[1]["key"].startswith("some-prefix/")

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_key_without_prefix(self, mock_objectstore):
        """Test that key is just fileName when no prefix is provided."""

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"

        # Create mock UploadFile
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        result = await upload_file_to_object_store(
            file=mock_file,
            prefix="",  # Empty prefix
        )

        # Key should be just fileName when prefix is empty
        assert result.key == result.fileName
        assert "/" not in result.key
        assert result.key.endswith(".csv")

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_default_prefix(self, mock_objectstore):
        """Test that default prefix is used when prefix is None."""

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"

        # Create mock UploadFile
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        result = await upload_file_to_object_store(
            file=mock_file,
            # prefix not provided, should default to "workflow_file_upload"
        )

        # Key should include default prefix
        assert result.key.startswith("workflow_file_upload/")
        assert result.key.endswith(".csv")

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_upload_error(self, mock_objectstore):
        """Test file upload error handling."""
        mock_objectstore.side_effect = Exception("Upload failed")

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"

        # Create mock UploadFile
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        with pytest.raises(Exception, match="Upload failed"):
            await upload_file_to_object_store(
                file=mock_file,
                prefix="workflow_file_upload",
            )

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_uuid_generation(self, mock_objectstore):
        """Test that each upload generates a unique UUID."""

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"

        # Create mock UploadFile for first upload
        mock_file1 = MagicMock(spec=UploadFile)
        mock_file1.filename = filename
        mock_file1.content_type = content_type
        mock_file1.read = AsyncMock(return_value=file_content)

        result1 = await upload_file_to_object_store(
            file=mock_file1,
            prefix="workflow_file_upload",
        )

        # Create mock UploadFile for second upload
        mock_file2 = MagicMock(spec=UploadFile)
        mock_file2.filename = filename
        mock_file2.content_type = content_type
        mock_file2.read = AsyncMock(return_value=file_content)

        result2 = await upload_file_to_object_store(
            file=mock_file2,
            prefix="workflow_file_upload",
        )

        # Each upload should have a unique ID and key
        assert result1.id != result2.id
        assert result1.key != result2.key
        assert result1.fileName != result2.fileName

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_version_generation(self, mock_objectstore):
        """Test that version is generated from first 8 chars of UUID."""

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"

        # Create mock UploadFile
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        result = await upload_file_to_object_store(
            file=mock_file,
            prefix="workflow_file_upload",
        )

        # Version should be first 8 chars of UUID
        assert result.version == result.id[:8]
        assert len(result.version) == 8

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_timestamp_generation(self, mock_objectstore):
        """Test that timestamps are generated correctly."""

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"

        # Create mock UploadFile
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = content_type
        mock_file.read = AsyncMock(return_value=file_content)

        # The module's wall clock is frozen, so the stamp is an exact value
        # rather than "within the last minute" of a second reading.
        frozen = SimpleNamespace(time=lambda: 1_700_000_000.1234)
        with patch("application_sdk.server.fastapi.utils.time", frozen):
            result = await upload_file_to_object_store(
                file=mock_file,
                prefix="workflow_file_upload",
            )

        # Timestamps are the clock's reading in whole milliseconds
        assert result.createdAt == 1_700_000_000_123
        assert result.updatedAt == result.createdAt

        # uploadedAt should be ISO format with Z
        assert result.uploadedAt.endswith("Z")
        assert "T" in result.uploadedAt

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_explicit_content_type(self, mock_objectstore):
        """Test that explicit content type parameter is used when provided."""

        file_content = b"certificate content"
        filename = "dev-atlan-anaplan.cer"
        explicit_content_type = "application/x-x509-ca-cert"
        file_content_type = "text/plain"  # Different from explicit

        # Create mock UploadFile with different content type
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = filename
        mock_file.content_type = file_content_type
        mock_file.read = AsyncMock(return_value=file_content)

        result = await upload_file_to_object_store(
            file=mock_file,
            prefix="workflow_file_upload",
            content_type=explicit_content_type,
        )

        # Verify explicit content type is used, not file.content_type
        assert result.contentType == explicit_content_type
        assert result.contentType != file_content_type

    @patch("application_sdk.server.fastapi.utils.upload_file_from_bytes")
    async def test_upload_file_content_type_fallback_chain(self, mock_objectstore):
        """Test content type fallback chain: explicit → file.content_type → default."""

        file_content = b"test content"
        filename = "test.csv"

        # Test 1: Explicit content type provided
        mock_file1 = MagicMock(spec=UploadFile)
        mock_file1.filename = filename
        mock_file1.content_type = "text/plain"
        mock_file1.read = AsyncMock(return_value=file_content)

        result1 = await upload_file_to_object_store(
            file=mock_file1,
            content_type="application/x-csv",
        )
        assert result1.contentType == "application/x-csv"

        # Test 2: No explicit content type, use file.content_type
        mock_file2 = MagicMock(spec=UploadFile)
        mock_file2.filename = filename
        mock_file2.content_type = "text/csv"
        mock_file2.read = AsyncMock(return_value=file_content)

        result2 = await upload_file_to_object_store(
            file=mock_file2,
        )
        assert result2.contentType == "text/csv"

        # Test 3: No explicit content type, file.content_type is None, use default
        mock_file3 = MagicMock(spec=UploadFile)
        mock_file3.filename = filename
        mock_file3.content_type = None
        mock_file3.read = AsyncMock(return_value=file_content)

        result3 = await upload_file_to_object_store(
            file=mock_file3,
        )
        assert result3.contentType == "application/octet-stream"

        # Test 4: Explicit content type is empty string, fallback to file.content_type
        mock_file4 = MagicMock(spec=UploadFile)
        mock_file4.filename = filename
        mock_file4.content_type = "text/csv"
        mock_file4.read = AsyncMock(return_value=file_content)

        result4 = await upload_file_to_object_store(
            file=mock_file4,
            content_type="",
        )
        # Empty string is falsy, so should fallback to file.content_type
        assert result4.contentType == "text/csv"


class TestDeprecationNotices:
    """Every public name in the v2 server modules is deprecated for v4.0."""

    def test_every_public_model_is_marked(self) -> None:
        public = {
            name
            for name, obj in vars(v2_models).items()
            if inspect.isclass(obj)
            and name.isidentifier()
            and not name.startswith("_")
            and obj.__module__ == v2_models.__name__
        }
        assert public == set(_V2_MODELS)
        for name in _V2_MODELS:
            notice = getattr(v2_models, name).__deprecated__
            assert "deprecated; use " in notice, name
            assert "removed in v4.0" in notice, name

    def test_importing_the_models_module_does_not_warn(self) -> None:
        """A deprecated subclass of a deprecated model would warn at import."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            importlib.reload(v2_models)

    def test_constructing_a_model_warns(self) -> None:
        with pytest.warns(DeprecationWarning, match="TestAuthResponse is deprecated"):
            v2_models.TestAuthResponse(success=True, message="ok")

    async def test_upload_file_to_object_store_warns_once(self) -> None:
        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "a.csv"
        mock_file.content_type = "text/csv"
        mock_file.read = AsyncMock(return_value=b"x")
        with (
            patch("application_sdk.server.fastapi.utils.upload_file_from_bytes"),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            await upload_file_to_object_store(file=mock_file)
        messages = [str(w.message) for w in caught if w.category is DeprecationWarning]
        assert len(messages) == 1
        assert "upload_file_to_object_store is deprecated" in messages[0]

    def test_internal_server_error_handler_warns(self) -> None:
        from application_sdk.server.fastapi.utils import internal_server_error_handler

        with pytest.warns(DeprecationWarning, match="internal_server_error_handler"):
            internal_server_error_handler(None, Exception("boom"))

    @pytest.mark.parametrize(
        "name",
        [
            "auth_credentials_strategy",
            "metadata_strategy",
            "payload_strategy",
            "workflow_event_strategy",
            "event_data_strategy",
        ],
    )
    def test_hypothesis_strategies_warn_on_access(self, name: str) -> None:
        from application_sdk.testing.hypothesis.strategies.server import fastapi

        with pytest.warns(DeprecationWarning, match=f"{name} is deprecated"):
            strategy = getattr(fastapi, name)
        assert strategy is getattr(fastapi, f"_{name}")
