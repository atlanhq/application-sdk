"""Tests for SQL workflow run() return value — output path computation for AE downstream nodes."""


class TestOutputPathComputation:
    """Unit tests for the output path stripping/construction logic.

    The actual run() method orchestrates Temporal activities, so we test the
    path computation logic in isolation by extracting the same algorithm.
    """

    @staticmethod
    def _compute_output(
        output_path: str,
        output_prefix: str,
        connection_qualified_name: str,
    ) -> dict:
        """Mirror the output-path computation from BaseSQLMetadataExtractionWorkflow.run()."""
        if output_prefix and output_path.startswith(output_prefix):
            transformed_prefix = (
                output_path[len(output_prefix) :].strip("/") + "/transformed"
            )
        else:
            transformed_prefix = output_path + "/transformed"

        return {
            "transformed_data_prefix": transformed_prefix,
            "connection_qualified_name": connection_qualified_name,
            "publish_state_prefix": f"persistent-artifacts/apps/atlan-publish-app/state/{connection_qualified_name}/publish-state",
            "current_state_prefix": f"argo-artifacts/{connection_qualified_name}/current-state",
        }

    def test_strips_local_prefix_from_output_path(self):
        """When output_path starts with output_prefix, the prefix is stripped."""
        result = self._compute_output(
            output_path="./local/tmp/2024/01/01/run-1",
            output_prefix="./local/tmp/",
            connection_qualified_name="default/snowflake/123",
        )
        assert result["transformed_data_prefix"] == "2024/01/01/run-1/transformed"

    def test_no_prefix_match_uses_full_path(self):
        """When output_path doesn't start with prefix, full path + /transformed."""
        result = self._compute_output(
            output_path="artifacts/run-1",
            output_prefix="./local/tmp/",
            connection_qualified_name="default/pg/456",
        )
        assert result["transformed_data_prefix"] == "artifacts/run-1/transformed"

    def test_empty_prefix_uses_full_path(self):
        """When output_prefix is empty, full path + /transformed."""
        result = self._compute_output(
            output_path="some/path",
            output_prefix="",
            connection_qualified_name="default/bq/789",
        )
        assert result["transformed_data_prefix"] == "some/path/transformed"

    def test_publish_state_prefix_format(self):
        """Publish state prefix follows expected pattern."""
        result = self._compute_output(
            output_path="p",
            output_prefix="",
            connection_qualified_name="default/snowflake/abc",
        )
        assert result["publish_state_prefix"] == (
            "persistent-artifacts/apps/atlan-publish-app/state/default/snowflake/abc/publish-state"
        )

    def test_current_state_prefix_format(self):
        """Current state prefix follows expected pattern."""
        result = self._compute_output(
            output_path="p",
            output_prefix="",
            connection_qualified_name="default/snowflake/abc",
        )
        assert result["current_state_prefix"] == (
            "argo-artifacts/default/snowflake/abc/current-state"
        )

    def test_empty_connection_qualified_name(self):
        """When connection is empty, paths still get generated (with empty segment)."""
        result = self._compute_output(
            output_path="./local/tmp/run",
            output_prefix="./local/tmp/",
            connection_qualified_name="",
        )
        assert result["connection_qualified_name"] == ""
        assert result["publish_state_prefix"] == (
            "persistent-artifacts/apps/atlan-publish-app/state//publish-state"
        )

    def test_trailing_slash_stripped_from_prefix(self):
        """Trailing slashes on the stripped path are removed before appending /transformed."""
        result = self._compute_output(
            output_path="./local/tmp/run/",
            output_prefix="./local/tmp/",
            connection_qualified_name="c",
        )
        assert result["transformed_data_prefix"] == "run/transformed"
