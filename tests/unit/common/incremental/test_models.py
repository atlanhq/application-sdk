"""Tests for incremental extraction Pydantic models.

Tests cover public methods with real business logic:
- WorkflowMetadata.is_incremental_ready: Multi-flag validation
- IncrementalWorkflowArgs: Model validation with aliases and extras
"""

from application_sdk.common.incremental.models import (
    IncrementalWorkflowArgs,
    WorkflowMetadata,
)

# ---------------------------------------------------------------------------
# WorkflowMetadata.is_incremental_ready
# ---------------------------------------------------------------------------


class TestWorkflowMetadataIsIncrementalReady:
    """Tests for WorkflowMetadata.is_incremental_ready (multi-flag validation).

    This method is the core business rule that determines whether all three
    prerequisites are met for incremental extraction:
    1. incremental_extraction is enabled
    2. marker_timestamp is present
    3. current_state_available is True
    """

    def test_all_prerequisites_met(self):
        """Returns True when all three flags are set."""
        meta = WorkflowMetadata(
            **{
                "incremental-extraction": True,
                "marker_timestamp": "2025-01-15T10:00:00Z",
                "current_state_available": True,
            }
        )
        assert meta.is_incremental_ready() is True

    def test_extraction_disabled(self):
        """Returns False when incremental_extraction is False."""
        meta = WorkflowMetadata(
            **{
                "incremental-extraction": False,
                "marker_timestamp": "2025-01-15T10:00:00Z",
                "current_state_available": True,
            }
        )
        assert meta.is_incremental_ready() is False

    def test_no_marker(self):
        """Returns False when marker_timestamp is absent."""
        meta = WorkflowMetadata(
            **{
                "incremental-extraction": True,
                "current_state_available": True,
            }
        )
        assert meta.is_incremental_ready() is False

    def test_state_not_available(self):
        """Returns False when current_state_available is False."""
        meta = WorkflowMetadata(
            **{
                "incremental-extraction": True,
                "marker_timestamp": "2025-01-15T10:00:00Z",
                "current_state_available": False,
            }
        )
        assert meta.is_incremental_ready() is False

    def test_empty_marker_string(self):
        """Empty marker string is treated as not present (falsy)."""
        meta = WorkflowMetadata(
            **{
                "incremental-extraction": True,
                "marker_timestamp": "",
                "current_state_available": True,
            }
        )
        assert meta.is_incremental_ready() is False

    def test_defaults_not_ready(self):
        """Default WorkflowMetadata is not incremental-ready."""
        meta = WorkflowMetadata()
        assert meta.is_incremental_ready() is False


# ---------------------------------------------------------------------------
# IncrementalWorkflowArgs
# ---------------------------------------------------------------------------


class TestIncrementalWorkflowArgs:
    """Tests for IncrementalWorkflowArgs model validation."""

    def test_parses_argo_style_hyphenated_keys(self):
        """Accepts Argo-style hyphenated keys via field aliases."""
        args = IncrementalWorkflowArgs.model_validate(
            {
                "metadata": {
                    "incremental-extraction": True,
                    "column-batch-size": 50000,
                    "column-chunk-size": 200000,
                    "prepone-marker-timestamp": False,
                    "prepone-marker-hours": 5,
                }
            }
        )
        assert args.metadata.incremental_extraction is True
        assert args.metadata.column_batch_size == 50000
        assert args.metadata.column_chunk_size == 200000
        assert args.metadata.prepone_marker_timestamp is False
        assert args.metadata.prepone_marker_hours == 5

    def test_parses_underscore_keys(self):
        """Accepts Python-style underscore keys via populate_by_name."""
        args = IncrementalWorkflowArgs.model_validate(
            {
                "metadata": {
                    "incremental_extraction": True,
                    "column_batch_size": 30000,
                }
            }
        )
        assert args.metadata.incremental_extraction is True
        assert args.metadata.column_batch_size == 30000

    def test_extra_fields_allowed(self):
        """Extra fields are allowed for forward compatibility."""
        # Should not raise - extras are accepted
        IncrementalWorkflowArgs.model_validate(
            {
                "metadata": {"some_future_field": "value"},
                "unknown_top_level": 42,
            }
        )

    def test_defaults(self):
        """Default values are sensible."""
        args = IncrementalWorkflowArgs.model_validate({})

        assert args.metadata.incremental_extraction is False
        assert args.metadata.column_batch_size == 25000
        assert args.metadata.column_chunk_size == 100000
        assert args.metadata.prepone_marker_timestamp is True
        assert args.metadata.prepone_marker_hours == 3
        assert args.metadata.copy_workers == 3
        assert args.metadata.marker_timestamp is None
        assert args.metadata.current_state_available is False

    def test_connection_info(self):
        """Connection info is correctly parsed."""
        args = IncrementalWorkflowArgs.model_validate(
            {
                "connection": {
                    "connection_name": "my-oracle",
                    "connection_qualified_name": "default/oracle/12345",
                },
            }
        )
        assert args.connection.connection_name == "my-oracle"
        assert args.connection.connection_qualified_name == "default/oracle/12345"

    def test_is_incremental_ready_delegates(self):
        """is_incremental_ready delegates to metadata.is_incremental_ready."""
        args = IncrementalWorkflowArgs.model_validate(
            {
                "metadata": {
                    "incremental-extraction": True,
                    "marker_timestamp": "2025-01-15T10:00:00Z",
                    "current_state_available": True,
                }
            }
        )
        assert args.is_incremental_ready() is True

        args2 = IncrementalWorkflowArgs.model_validate({"metadata": {}})
        assert args2.is_incremental_ready() is False
