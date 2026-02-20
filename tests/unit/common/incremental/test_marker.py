"""Tests for incremental extraction marker management.

Tests cover public functions with real business logic:
- process_marker_timestamp: Conditional normalization and preponing
- fetch_marker_from_storage: Multi-source fallback logic (args -> S3 -> None)
- persist_marker_to_storage: Local write + S3 upload with error handling
"""

from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.common.incremental.marker import (
    fetch_marker_from_storage,
    persist_marker_to_storage,
    process_marker_timestamp,
)

# ---------------------------------------------------------------------------
# process_marker_timestamp
# ---------------------------------------------------------------------------


class TestProcessMarkerTimestamp:
    """Tests for process_marker_timestamp (conditional normalization + preponing)."""

    def test_normalizes_without_prepone(self):
        """When prepone is disabled, only normalization is applied."""
        result = process_marker_timestamp(
            "2025-01-15T10:30:00.123456789Z",
            prepone_enabled=False,
        )
        assert result == "2025-01-15T10:30:00Z"

    def test_normalizes_and_prepones(self):
        """When prepone is enabled, normalizes first then moves back."""
        result = process_marker_timestamp(
            "2025-01-15T10:30:00.123456789Z",
            prepone_enabled=True,
            prepone_hours=3,
        )
        assert result == "2025-01-15T07:30:00Z"

    def test_prepone_enabled_but_zero_hours(self):
        """When prepone is enabled but hours=0, no preponing occurs."""
        result = process_marker_timestamp(
            "2025-01-15T10:30:00Z",
            prepone_enabled=True,
            prepone_hours=0,
        )
        assert result == "2025-01-15T10:30:00Z"

    def test_defaults_no_prepone(self):
        """Default parameters disable preponing."""
        result = process_marker_timestamp("2025-01-15T10:30:00Z")
        assert result == "2025-01-15T10:30:00Z"

    def test_already_clean_marker_unchanged(self):
        """Clean markers pass through normalization unchanged."""
        result = process_marker_timestamp("2025-01-15T10:30:00Z", prepone_enabled=False)
        assert result == "2025-01-15T10:30:00Z"


# ---------------------------------------------------------------------------
# fetch_marker_from_storage
# ---------------------------------------------------------------------------


class TestFetchMarkerFromStorage:
    """Tests for fetch_marker_from_storage (multi-source fallback)."""

    def _make_workflow_args(self, marker=None, qualified_name="t/c/123"):
        args = {
            "connection": {"connection_qualified_name": qualified_name},
            "metadata": {},
        }
        if marker:
            args["metadata"]["marker_timestamp"] = marker
        return args

    @pytest.mark.asyncio
    async def test_marker_from_workflow_args(self):
        """Marker provided in workflow_args is used directly (no S3 call)."""
        args = self._make_workflow_args(marker="2025-01-15T10:00:00Z")

        with patch(
            "application_sdk.common.incremental.marker.download_marker_from_s3"
        ) as mock_s3:
            marker, next_marker = await fetch_marker_from_storage(args)

        assert marker == "2025-01-15T10:00:00Z"
        assert next_marker  # Should be a valid timestamp string
        mock_s3.assert_not_called()

    @pytest.mark.asyncio
    async def test_marker_from_s3_fallback(self):
        """When not in workflow_args, marker is downloaded from S3."""
        args = self._make_workflow_args()

        with patch(
            "application_sdk.common.incremental.marker.download_marker_from_s3",
            new_callable=AsyncMock,
            return_value="2025-01-10T08:00:00Z",
        ):
            marker, next_marker = await fetch_marker_from_storage(args)

        assert marker == "2025-01-10T08:00:00Z"
        assert next_marker

    @pytest.mark.asyncio
    async def test_no_marker_returns_none(self):
        """First run: no marker in args or S3 returns (None, next_marker)."""
        args = self._make_workflow_args()

        with patch(
            "application_sdk.common.incremental.marker.download_marker_from_s3",
            new_callable=AsyncMock,
            return_value=None,
        ):
            marker, next_marker = await fetch_marker_from_storage(args)

        assert marker is None
        assert next_marker  # next_marker is always set

    @pytest.mark.asyncio
    async def test_marker_with_prepone(self):
        """Fetched marker is preponed when enabled."""
        args = self._make_workflow_args(marker="2025-01-15T10:00:00Z")

        marker, _ = await fetch_marker_from_storage(
            args, prepone_enabled=True, prepone_hours=2
        )

        assert marker == "2025-01-15T08:00:00Z"

    @pytest.mark.asyncio
    async def test_next_marker_always_generated(self):
        """next_marker is always a fresh timestamp regardless of existing marker."""
        args = self._make_workflow_args()

        with patch(
            "application_sdk.common.incremental.marker.download_marker_from_s3",
            new_callable=AsyncMock,
            return_value=None,
        ):
            _, next_marker = await fetch_marker_from_storage(args)

        # next_marker should be parseable as a timestamp
        assert "T" in next_marker
        assert next_marker.endswith("Z")


# ---------------------------------------------------------------------------
# persist_marker_to_storage
# ---------------------------------------------------------------------------


class TestPersistMarkerToStorage:
    """Tests for persist_marker_to_storage (local write + S3 upload)."""

    def _make_workflow_args(self, qualified_name="t/c/123", app_name="oracle"):
        return {
            "connection": {"connection_qualified_name": qualified_name},
            "application_name": app_name,
        }

    @pytest.mark.asyncio
    async def test_writes_and_uploads_marker(self):
        """Writes marker to local file and uploads to S3."""
        args = self._make_workflow_args()

        with patch(
            "application_sdk.common.incremental.marker.ObjectStore"
        ) as mock_store:
            mock_store.upload_file = AsyncMock()

            result = await persist_marker_to_storage(args, "2025-01-15T10:00:00Z")

        assert result["marker_written"] is True
        assert result["marker_timestamp"] == "2025-01-15T10:00:00Z"
        assert "s3_key" in result
        assert "marker.txt" in result["s3_key"]
        mock_store.upload_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_s3_upload_failure_raises(self):
        """S3 upload failure propagates the exception."""
        args = self._make_workflow_args()

        with patch(
            "application_sdk.common.incremental.marker.ObjectStore"
        ) as mock_store:
            mock_store.upload_file = AsyncMock(side_effect=Exception("S3 unavailable"))

            with pytest.raises(Exception, match="S3 unavailable"):
                await persist_marker_to_storage(args, "2025-01-15T10:00:00Z")
