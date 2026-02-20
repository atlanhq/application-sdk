"""Tests for current state reader utilities.

Tests cover public functions with real business logic:
- download_current_state: First-run handling, S3 download, JSON counting
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.common.incremental.state.state_reader import (
    download_current_state,
)


class TestDownloadCurrentState:
    """Tests for download_current_state (S3 download with first-run handling)."""

    def _make_workflow_args(self, qualified_name="t/c/123"):
        return {
            "connection": {"connection_qualified_name": qualified_name},
            "application_name": "oracle",
        }

    @pytest.mark.asyncio
    async def test_first_run_returns_not_exists(self):
        """First run (S3 raises exception) returns exists=False."""
        args = self._make_workflow_args()

        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.ObjectStore"
            ) as mock_store,
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path"
            ) as mock_path,
        ):
            mock_store.download_prefix = AsyncMock(
                side_effect=FileNotFoundError("not found")
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir) / "current-state"
                state_dir.mkdir(parents=True)
                mock_path.return_value = state_dir

                _, _, exists, json_count = await download_current_state(args)

        assert exists is False
        assert json_count == 0

    @pytest.mark.asyncio
    async def test_existing_state_returns_exists(self):
        """Existing state with JSON files returns exists=True and file count."""
        args = self._make_workflow_args()

        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.ObjectStore"
            ) as mock_store,
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path"
            ) as mock_path,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir) / "current-state"
                state_dir.mkdir(parents=True)
                mock_path.return_value = state_dir

                # Simulate S3 download creating JSON files
                async def fake_download(**kwargs):
                    table_dir = state_dir / "table"
                    table_dir.mkdir(parents=True, exist_ok=True)
                    (table_dir / "chunk-0.json").write_text("{}")
                    (table_dir / "chunk-1.json").write_text("{}")

                mock_store.download_prefix = AsyncMock(side_effect=fake_download)

                dir_result, prefix, exists, json_count = await download_current_state(
                    args
                )

        assert exists is True
        assert json_count == 2

    @pytest.mark.asyncio
    async def test_empty_download_returns_not_exists(self):
        """Download that results in zero JSON files returns exists=False."""
        args = self._make_workflow_args()

        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.ObjectStore"
            ) as mock_store,
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path"
            ) as mock_path,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir) / "current-state"
                state_dir.mkdir(parents=True)
                mock_path.return_value = state_dir

                mock_store.download_prefix = AsyncMock()

                _, _, exists, json_count = await download_current_state(args)

        assert exists is False
        assert json_count == 0

    @pytest.mark.asyncio
    async def test_clears_stale_directory(self):
        """Clears existing stale directory before downloading."""
        args = self._make_workflow_args()

        with (
            patch(
                "application_sdk.common.incremental.state.state_reader.ObjectStore"
            ) as mock_store,
            patch(
                "application_sdk.common.incremental.state.state_reader."
                "get_persistent_artifacts_path"
            ) as mock_path,
        ):
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir) / "current-state"
                state_dir.mkdir(parents=True)
                # Create stale file
                (state_dir / "stale.json").write_text("{}")
                mock_path.return_value = state_dir

                mock_store.download_prefix = AsyncMock()

                _, _, exists, json_count = await download_current_state(args)

        # Stale file should be removed (directory was cleared)
        assert json_count == 0
