"""Unit tests for HandlerInterface upload_file method."""

from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.handlers import HandlerInterface


class ConcreteHandler(HandlerInterface):
    """Concrete implementation of HandlerInterface for testing."""

    async def load(self, *args, **kwargs):
        """Mock load method."""
        pass

    async def test_auth(self, *args, **kwargs):
        """Mock test_auth method."""
        return True

    async def preflight_check(self, *args, **kwargs):
        """Mock preflight_check method."""
        return {}

    async def fetch_metadata(self, *args, **kwargs):
        """Mock fetch_metadata method."""
        return {}


@pytest.fixture
def handler():
    """Create a ConcreteHandler instance for testing."""
    return ConcreteHandler()


@pytest.mark.asyncio
class TestHandlerInterfaceUploadFile:
    """Test cases for HandlerInterface upload_file method."""

    @patch("application_sdk.services.objectstore.ObjectStore")
    async def test_upload_file_success(self, mock_objectstore, handler):
        """Test successful file upload."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

        file_content = b"test file content"
        filename = "test.csv"
        content_type = "text/csv"
        file_size = len(file_content)

        result = await handler.upload_file(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,  # Model uses 'size', not 'file_size'
            prefix="workflow_file_upload",
        )

        # Verify ObjectStore was called
        mock_objectstore.upload_file_from_bytes.assert_called_once()
        call_args = mock_objectstore.upload_file_from_bytes.call_args
        assert call_args[1]["file_content"] == file_content
        assert call_args[1]["destination"].endswith(".csv")

        # Verify response structure
        assert "id" in result
        assert "version" in result
        assert "isActive" in result
        assert result["isActive"] is True
        assert "createdAt" in result
        assert "updatedAt" in result
        assert "fileName" in result
        assert "rawName" in result
        assert result["rawName"] == filename
        assert "key" in result
        assert "extension" in result
        assert result["extension"] == ".csv"
        assert "contentType" in result
        assert result["contentType"] == content_type
        assert "fileSize" in result
        assert result["fileSize"] == file_size
        assert "isEncrypted" in result
        assert result["isEncrypted"] is False
        assert "redirectUrl" in result
        assert result["redirectUrl"] == ""
        assert "isUploaded" in result
        assert result["isUploaded"] is True
        assert "uploadedAt" in result
        assert "isArchived" in result
        assert result["isArchived"] is False

    @patch("application_sdk.services.objectstore.ObjectStore")
    async def test_upload_file_raw_name_preserved(self, mock_objectstore, handler):
        """Test that rawName preserves original filename (force=false behavior)."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

        file_content = b"test content"
        filename = "original.txt"
        content_type = "text/plain"
        file_size = len(file_content)

        result = await handler.upload_file(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,  # Model uses 'size', not 'file_size'
            prefix="workflow_file_upload",
        )

        # When force=false (default), rawName should be the original filename
        assert result["rawName"] == filename
        assert result["rawName"] != result["fileName"]  # fileName is UUID-based

    @patch("application_sdk.services.objectstore.ObjectStore")
    async def test_upload_file_extension_from_content_type(
        self, mock_objectstore, handler
    ):
        """Test extension determination from content type when filename has no extension."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

        file_content = b"test content"
        filename = "testfile"  # No extension
        content_type = "text/csv"
        file_size = len(file_content)

        result = await handler.upload_file(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,  # Model uses 'size', not 'file_size'
            prefix="workflow_file_upload",
        )

        # Extension should be determined from content type
        assert result["extension"] == ".csv"
        assert result["fileName"].endswith(".csv")

    @patch("application_sdk.services.objectstore.ObjectStore")
    async def test_upload_file_key_generation(self, mock_objectstore, handler):
        """Test that key includes prefix when provided (matching heracles behavior)."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"
        file_size = len(file_content)

        result = await handler.upload_file(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,  # Model uses 'size', not 'file_size'
            prefix="some-prefix",
        )

        # Key should include prefix (matching heracles: key = prefix + "/" + fileName)
        assert result["key"] == f"some-prefix/{result['fileName']}"
        assert "/" in result["key"]  # Should have path separator
        assert result["key"].endswith(".csv")
        assert result["key"].startswith("some-prefix/")

        # Verify ObjectStore was called with the key including prefix
        call_args = mock_objectstore.upload_file_from_bytes.call_args
        assert call_args[1]["destination"] == result["key"]
        assert call_args[1]["destination"].startswith("some-prefix/")

    @patch("application_sdk.services.objectstore.ObjectStore")
    async def test_upload_file_key_without_prefix(self, mock_objectstore, handler):
        """Test that key is just fileName when no prefix is provided."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"
        file_size = len(file_content)

        result = await handler.upload_file(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,  # Model uses 'size', not 'file_size'
            prefix="",  # Empty prefix
        )

        # Key should be just fileName when prefix is empty (matching heracles)
        assert result["key"] == result["fileName"]
        assert "/" not in result["key"]
        assert result["key"].endswith(".csv")

    @patch("application_sdk.services.objectstore.ObjectStore")
    async def test_upload_file_upload_error(self, mock_objectstore, handler):
        """Test file upload error handling."""
        mock_objectstore.upload_file_from_bytes = AsyncMock(
            side_effect=Exception("Upload failed")
        )

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"
        file_size = len(file_content)

        with pytest.raises(Exception, match="Upload failed"):
            await handler.upload_file(
                file_content=file_content,
                filename=filename,
                content_type=content_type,
                size=file_size,  # Model uses 'size', not 'file_size'
                prefix="workflow_file_upload",
            )

    @patch("application_sdk.services.objectstore.ObjectStore")
    async def test_upload_file_uuid_generation(self, mock_objectstore, handler):
        """Test that each upload generates a unique UUID."""
        mock_objectstore.upload_file_from_bytes = AsyncMock()

        file_content = b"test content"
        filename = "test.csv"
        content_type = "text/csv"
        file_size = len(file_content)

        result1 = await handler.upload_file(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,  # Model uses 'size', not 'file_size'
            prefix="workflow_file_upload",
        )

        result2 = await handler.upload_file(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
            size=file_size,  # Model uses 'size', not 'file_size'
            prefix="workflow_file_upload",
        )

        # Each upload should have a unique ID and key
        assert result1["id"] != result2["id"]
        assert result1["key"] != result2["key"]
        assert result1["fileName"] != result2["fileName"]
