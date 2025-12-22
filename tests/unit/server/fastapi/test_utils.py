"""Unit tests for FastAPI utility functions."""

from unittest.mock import AsyncMock, patch

import pytest

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
        file_size = len(file_content)

        result = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
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
        assert result.fileSize == file_size
        assert result.isEncrypted is False
        assert result.redirectUrl == ""
        assert result.isUploaded is True
        assert result.uploadedAt is not None
        assert result.isArchived is False

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_raw_name_preserved(self, mock_objectstore):
        """Test that rawName preserves original filename (force=false behavior)."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

        file_content = b"test content"
        filename = "original.txt"
        content_type = "text/plain"
        file_size = len(file_content)

        result = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
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
        file_size = len(file_content)

        result = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
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
        file_size = len(file_content)

        result = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
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
        file_size = len(file_content)

        result = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
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
        file_size = len(file_content)

        result = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
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
        file_size = len(file_content)

        with pytest.raises(Exception, match="Upload failed"):
            await upload_file_to_object_store(
                file_content=file_content,
                filename=filename,
                content_type=content_type,
                size=file_size,
                prefix="workflow_file_upload",
            )

    @patch("application_sdk.server.fastapi.utils.ObjectStore")
    async def test_upload_file_uuid_generation(self, mock_objectstore):
        """Test that each upload generates a unique UUID."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"
        file_size = len(file_content)

        result1 = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
            prefix="workflow_file_upload",
        )

        result2 = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
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
        file_size = len(file_content)

        result = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
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
        file_size = len(file_content)

        result = await upload_file_to_object_store(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,
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
