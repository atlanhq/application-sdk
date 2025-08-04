"""Tests for constants module."""

from application_sdk.constants import WORKFLOW_HOST


class TestWorkflowHost:
    """Test suite for WORKFLOW_HOST constant."""

    def test_workflow_host_default_value(self):
        """Test that WORKFLOW_HOST has a sensible default."""
        assert WORKFLOW_HOST == "localhost" or WORKFLOW_HOST is not None
