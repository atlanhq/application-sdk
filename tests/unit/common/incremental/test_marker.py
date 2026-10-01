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

    async def test_marker_from_existing_marker_param(self):
        """Marker provided via existing_marker is used directly (no S3 call)."""
        with patch(
            "application_sdk.common.incremental.marker.download_marker_from_s3"
        ) as mock_s3:
            marker, next_marker = await fetch_marker_from_storage(
                connection_qualified_name="t/c/123",
                existing_marker="2025-01-15T10:00:00Z",
            )

        assert marker == "2025-01-15T10:00:00Z"
        assert next_marker  # Should be a valid timestamp string
        mock_s3.assert_not_called()

    async def test_marker_from_s3_fallback(self):
        """When existing_marker is not provided, marker is downloaded from S3."""
        with patch(
            "application_sdk.common.incremental.marker.download_marker_from_s3",
            new_callable=AsyncMock,
            return_value="2025-01-10T08:00:00Z",
        ):
            marker, next_marker = await fetch_marker_from_storage(
                connection_qualified_name="t/c/123"
            )

        assert marker == "2025-01-10T08:00:00Z"
        assert next_marker

    async def test_no_marker_returns_none(self):
        """First run: no existing_marker and S3 returns None → (None, next_marker)."""
        with patch(
            "application_sdk.common.incremental.marker.download_marker_from_s3",
            new_callable=AsyncMock,
            return_value=None,
        ):
            marker, next_marker = await fetch_marker_from_storage(
                connection_qualified_name="t/c/123"
            )

        assert marker is None
        assert next_marker  # next_marker is always set

    async def test_marker_with_prepone(self):
        """Fetched marker is preponed when enabled."""
        marker, _ = await fetch_marker_from_storage(
            connection_qualified_name="t/c/123",
            existing_marker="2025-01-15T10:00:00Z",
            prepone_enabled=True,
            prepone_hours=2,
        )

        assert marker == "2025-01-15T08:00:00Z"

    async def test_next_marker_always_generated(self):
        """next_marker is always a fresh timestamp regardless of existing marker."""
        with patch(
            "application_sdk.common.incremental.marker.download_marker_from_s3",
            new_callable=AsyncMock,
            return_value=None,
        ):
            _, next_marker = await fetch_marker_from_storage(
                connection_qualified_name="t/c/123"
            )

        # next_marker should be parseable as a timestamp
        assert "T" in next_marker
        assert next_marker.endswith("Z")


# ---------------------------------------------------------------------------
# persist_marker_to_storage
# ---------------------------------------------------------------------------


class TestPersistMarkerToStorage:
    """Tests for persist_marker_to_storage (in-memory upload)."""

    async def test_round_trips_through_the_store(
        self, memory_store, tmp_path, monkeypatch
    ):
        """What persist writes, the next run's read returns — with no local copy."""
        from application_sdk.common.incremental import helpers

        monkeypatch.setattr(helpers, "TEMPORARY_PATH", str(tmp_path))
        result = await persist_marker_to_storage(
            connection_qualified_name="t/c/123",
            marker_value="2025-01-15T10:00:00Z",
            application_name="oracle",
        )

        assert result["marker_written"] is True
        assert result["marker_timestamp"] == "2025-01-15T10:00:00Z"
        assert result["s3_key"].endswith("/marker.txt")
        assert await helpers.download_marker_from_s3("t/c/123", "oracle") == (
            "2025-01-15T10:00:00Z"
        )
        assert not any(tmp_path.rglob("marker.txt"))

    async def test_a_marker_that_does_not_match_its_sidecar_is_refused(
        self, memory_store, monkeypatch
    ):
        """The in-memory read keeps the download's integrity check: a damaged
        marker with a later timestamp would make the next run skip changes."""
        import hashlib

        from application_sdk import constants
        from application_sdk.common.incremental import helpers
        from application_sdk.storage.errors import StorageIntegrityError
        from application_sdk.storage.ops import _put

        monkeypatch.setattr(constants, "STORAGE_VERIFY_TRANSFERS", True)
        key = f"{helpers.get_persistent_s3_prefix('t/c/123', 'oracle')}/marker.txt"
        good = b"2025-01-15T10:00:00Z"
        await _put(key, b"2099-01-01T00:00:00Z")
        await _put(f"{key}.sha256", hashlib.sha256(good).hexdigest().encode())

        with pytest.raises(StorageIntegrityError):
            await helpers.download_marker_from_s3("t/c/123", "oracle")

        await _put(key, good)
        assert await helpers.download_marker_from_s3("t/c/123", "oracle") == (
            "2025-01-15T10:00:00Z"
        )

    async def test_s3_upload_failure_raises(self):
        """S3 upload failure propagates the exception."""
        with patch(
            "application_sdk.common.incremental.marker.upload_file_from_bytes",
            new_callable=AsyncMock,
            side_effect=Exception("S3 unavailable"),
        ):
            with pytest.raises(
                Exception, match="Failed to upload marker to S3"
            ) as exc_info:
                await persist_marker_to_storage(
                    connection_qualified_name="t/c/123",
                    marker_value="2025-01-15T10:00:00Z",
                    application_name="oracle",
                )
            assert "S3 unavailable" in str(exc_info.value.__cause__)
