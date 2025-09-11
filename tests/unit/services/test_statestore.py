"""Unit tests for StateStore services."""

import json
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from application_sdk.services.statestore import (
    StateStore,
    StateType,
    build_state_store_path,
)


class TestStateStore:
    """Test suite for StateStore class."""

    @pytest.mark.asyncio
    @patch(
        "application_sdk.services.statestore.ObjectStore.get_content",
        new_callable=AsyncMock,
    )
    async def test_get_state_success(self, mock_get_content: AsyncMock) -> None:
        """Test successful state retrieval from object store."""
        test_state = {"status": "running", "progress": 50}
        mock_get_content.return_value = json.dumps(test_state).encode("utf-8")

        result = await StateStore.get_state("test-id", StateType.WORKFLOWS)

        assert result == test_state
        mock_get_content.assert_called_once()
        # Verify suppress_error=True is passed
        call_args = mock_get_content.call_args
        assert call_args.kwargs.get("suppress_error") is True

    @pytest.mark.asyncio
    @patch(
        "application_sdk.services.statestore.ObjectStore.get_content",
        new_callable=AsyncMock,
    )
    async def test_get_state_not_found_returns_empty_dict(
        self, mock_get_content: AsyncMock
    ) -> None:
        """Test get_state returns empty dict when file not found."""
        # Simulate file not found by returning None (suppress_error=True behavior)
        mock_get_content.return_value = None

        result = await StateStore.get_state("nonexistent-id", StateType.WORKFLOWS)

        assert result == {}
        mock_get_content.assert_called_once()
        # Verify suppress_error=True is passed
        call_args = mock_get_content.call_args
        assert call_args.kwargs.get("suppress_error") is True

    @pytest.mark.asyncio
    @patch(
        "application_sdk.services.statestore.ObjectStore.get_content",
        new_callable=AsyncMock,
    )
    async def test_get_state_json_decode_error(
        self, mock_get_content: AsyncMock
    ) -> None:
        """Test get_state raises exception on JSON decode error."""
        # Return invalid JSON
        mock_get_content.return_value = b"invalid json content"

        with pytest.raises(json.JSONDecodeError):
            await StateStore.get_state("test-id", StateType.WORKFLOWS)

    @pytest.mark.asyncio
    @patch(
        "application_sdk.services.statestore.ObjectStore.get_content",
        new_callable=AsyncMock,
    )
    async def test_get_state_object_store_error(
        self, mock_get_content: AsyncMock
    ) -> None:
        """Test get_state propagates object store errors."""
        mock_get_content.side_effect = Exception("Object store connection failed")

        with pytest.raises(Exception, match="Object store connection failed"):
            await StateStore.get_state("test-id", StateType.WORKFLOWS)

    @pytest.mark.asyncio
    @patch(
        "application_sdk.services.statestore.ObjectStore.upload_file",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.services.statestore.StateStore.get_state",
        new_callable=AsyncMock,
    )
    @patch("os.makedirs")
    async def test_save_state_success(
        self,
        mock_makedirs: MagicMock,
        mock_get_state: AsyncMock,
        mock_upload_file: AsyncMock,
    ) -> None:
        """Test successful state saving."""
        # Setup existing state
        existing_state = {"status": "running", "step": 1}
        mock_get_state.return_value = existing_state

        with patch("builtins.open", mock_open()) as mock_file:
            await StateStore.save_state("progress", 75, "test-id", StateType.WORKFLOWS)

        # Verify state was merged
        mock_get_state.assert_called_once_with("test-id", StateType.WORKFLOWS)

        # Verify file operations
        mock_makedirs.assert_called_once()
        mock_file.assert_called_once()

        # Verify upload was called
        mock_upload_file.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "application_sdk.services.statestore.ObjectStore.upload_file",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.services.statestore.StateStore.get_state",
        new_callable=AsyncMock,
    )
    @patch("os.makedirs")
    async def test_save_state_object_success(
        self,
        mock_makedirs: MagicMock,
        mock_get_state: AsyncMock,
        mock_upload_file: AsyncMock,
    ) -> None:
        """Test successful state object saving."""
        # Setup existing state
        existing_state = {"status": "running", "step": 1}
        mock_get_state.return_value = existing_state

        new_state = {"progress": 75, "current_task": "processing"}

        with patch("builtins.open", mock_open()) as mock_file:
            result = await StateStore.save_state_object(
                "test-id", new_state, StateType.WORKFLOWS
            )

        # Verify state was merged and returned
        expected_merged_state = {
            "status": "running",
            "step": 1,
            "progress": 75,
            "current_task": "processing",
        }
        assert result == expected_merged_state

        # Verify state was retrieved
        mock_get_state.assert_called_once_with("test-id", StateType.WORKFLOWS)

        # Verify file operations
        mock_makedirs.assert_called_once()
        mock_file.assert_called_once()

        # Verify upload was called
        mock_upload_file.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "application_sdk.services.statestore.StateStore.get_state",
        new_callable=AsyncMock,
    )
    async def test_save_state_get_state_failure(
        self, mock_get_state: AsyncMock
    ) -> None:
        """Test save_state propagates get_state failures."""
        mock_get_state.side_effect = Exception("Failed to retrieve existing state")

        with pytest.raises(Exception, match="Failed to retrieve existing state"):
            await StateStore.save_state("key", "value", "test-id", StateType.WORKFLOWS)

    @pytest.mark.asyncio
    @patch(
        "application_sdk.services.statestore.ObjectStore.upload_file",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.services.statestore.StateStore.get_state",
        new_callable=AsyncMock,
    )
    @patch("os.makedirs")
    async def test_save_state_upload_failure(
        self,
        mock_makedirs: MagicMock,
        mock_get_state: AsyncMock,
        mock_upload_file: AsyncMock,
    ) -> None:
        """Test save_state propagates upload failures."""
        mock_get_state.return_value = {}
        mock_upload_file.side_effect = Exception("Upload failed")

        with patch("builtins.open", mock_open()):
            with pytest.raises(Exception, match="Upload failed"):
                await StateStore.save_state(
                    "key", "value", "test-id", StateType.WORKFLOWS
                )

    def test_build_state_store_path_workflows(self) -> None:
        """Test build_state_store_path for workflows."""
        path = build_state_store_path("workflow-123", StateType.WORKFLOWS)
        assert "workflows" in path
        assert "workflow-123" in path
        assert path.endswith("config.json")

    def test_build_state_store_path_credentials(self) -> None:
        """Test build_state_store_path for credentials."""
        path = build_state_store_path("cred-456", StateType.CREDENTIALS)
        assert "credentials" in path
        assert "cred-456" in path
        assert path.endswith("config.json")

    def test_state_type_is_member_valid(self) -> None:
        """Test StateType.is_member with valid values."""
        assert StateType.is_member("workflows") is True
        assert StateType.is_member("credentials") is True

    def test_state_type_is_member_invalid(self) -> None:
        """Test StateType.is_member with invalid values."""
        assert StateType.is_member("invalid") is False
        assert StateType.is_member("") is False
        assert StateType.is_member("WORKFLOWS") is False  # Case sensitive
