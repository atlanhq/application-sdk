"""Unit tests for FastAPI utility functions."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import UploadFile

from application_sdk.server.fastapi.utils import upload_file_to_object_store


@pytest.mark.asyncio
class TestUploadFileToObjectStore:
    """Test cases for upload_file_to_object_store utility function."""

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_success(self, mock_objectstore):
        """Test successful file upload."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

        # Verify ObjectStore was called
        mock_objectstore.upload_file_from_bytes.assert_called_once()
        call_args = mock_objectstore.upload_file_from_bytes.call_args
        assert call_args[1]["file_content"] == file_content
        assert call_args[1]["destination"].endswith(".csv")
        assert "workflow_file_upload" in call_args[1]["destination"]

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_with_explicit_filename(self, mock_objectstore):
        """Test that explicit filename parameter takes precedence over file.filename."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_filename_fallback(self, mock_objectstore):
        """Test filename fallback: explicit -> file.filename -> 'uploaded_file'."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_raw_name_preserved(self, mock_objectstore):
        """Test that rawName preserves original filename (force=false behavior)."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_extension_from_content_type(self, mock_objectstore):
        """Test extension determination from content type when filename has no extension."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_key_generation(self, mock_objectstore):
        """Test that key includes prefix when provided."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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
        call_args = mock_objectstore.upload_file_from_bytes.call_args
        assert call_args[1]["destination"] == result.key
        assert call_args[1]["destination"].startswith("some-prefix/")

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_key_without_prefix(self, mock_objectstore):
        """Test that key is just fileName when no prefix is provided."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_default_prefix(self, mock_objectstore):
        """Test that default prefix is used when prefix is None."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_upload_error(self, mock_objectstore):
        """Test file upload error handling."""
        mock_objectstore.upload_file_from_bytes = AsyncMock(
            side_effect=Exception("Upload failed")
        )

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_uuid_generation(self, mock_objectstore):
        """Test that each upload generates a unique UUID."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_version_generation(self, mock_objectstore):
        """Test that version is generated from first 8 chars of UUID."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_timestamp_generation(self, mock_objectstore):
        """Test that timestamps are generated correctly."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

        # Timestamps should be in milliseconds
        assert result.createdAt > 0
        assert result.updatedAt > 0
        assert result.createdAt == result.updatedAt
        # Should be recent (within last minute)
        import time

        current_ms = int(time.time() * 1000)
        assert abs(result.createdAt - current_ms) < 60000

        # uploadedAt should be ISO format with Z
        assert result.uploadedAt.endswith("Z")
        assert "T" in result.uploadedAt

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_explicit_content_type(self, mock_objectstore):
        """Test that explicit content type parameter is used when provided."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_content_type_fallback_chain(self, mock_objectstore):
        """Test content type fallback chain: explicit → file.content_type → default."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

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
