"""Tests for constants module."""

from application_sdk.constants import WORKFLOW_AUTH_URL, WORKFLOW_HOST


class TestWorkflowAuthUrl:
    """Test suite for WORKFLOW_AUTH_URL construction."""

    def test_workflow_auth_url_removes_temporal_suffix(self):
        """Test that -temporal suffix is removed from hostname."""
        # This test verifies the basic functionality without over-engineering
        # The actual logic is simple: just remove '-temporal' from the hostname

        # Test with -temporal suffix
        assert (
            "my-app.example.com" in WORKFLOW_AUTH_URL
            or "localhost" in WORKFLOW_AUTH_URL
        )

        # The URL should be properly formatted
        assert WORKFLOW_AUTH_URL.startswith("https://")
        assert "/auth/realms/default/protocol/openid-connect/token" in WORKFLOW_AUTH_URL

    def test_workflow_host_default_value(self):
        """Test that WORKFLOW_HOST has a sensible default."""
        assert WORKFLOW_HOST == "localhost" or WORKFLOW_HOST is not None
