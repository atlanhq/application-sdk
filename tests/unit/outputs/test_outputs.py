"""Unit tests for OutputCollector, Metric, and Artifact."""

from application_sdk.outputs.collector import OutputCollector
from application_sdk.outputs.models import Artifact, Metric


class TestMetricAccumulation:
    """Tests for OutputCollector.add_metric accumulation semantics."""

    def test_add_numeric_metric(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="rows", value=100))
        assert c.to_dict() == {"metrics": {"rows": 100}}

    def test_numeric_metrics_with_same_name_are_summed(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="rows", value=100))
        c.add_metric(Metric(name="rows", value=50))
        assert c.to_dict()["metrics"]["rows"] == 150

    def test_float_metrics_are_summed(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="ratio", value=0.5))
        c.add_metric(Metric(name="ratio", value=0.25))
        assert abs(c.to_dict()["metrics"]["ratio"] - 0.75) < 1e-9

    def test_string_metric_last_write_wins(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="status", value="running"))
        c.add_metric(Metric(name="status", value="completed"))
        assert c.to_dict()["metrics"]["status"] == "completed"

    def test_string_over_numeric_last_write_wins(self) -> None:
        """Non-numeric collision falls through to last-write-wins."""
        c = OutputCollector()
        c.add_metric(Metric(name="mixed", value=42))
        c.add_metric(Metric(name="mixed", value="done"))
        assert c.to_dict()["metrics"]["mixed"] == "done"

    def test_display_name_serialized_when_present(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="qi-queries", value=42, display_name="QI Queries"))
        result = c.to_dict()["metrics"]["qi-queries"]
        assert result == {"value": 42, "display_name": "QI Queries"}

    def test_no_display_name_serialized_as_plain_value(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="rows", value=10))
        assert c.to_dict()["metrics"]["rows"] == 10

    def test_display_name_preserved_on_numeric_accumulation(self) -> None:
        """When accumulating, the incoming display_name takes precedence."""
        c = OutputCollector()
        c.add_metric(Metric(name="rows", value=10, display_name="Rows Processed"))
        c.add_metric(Metric(name="rows", value=5, display_name="Total Rows"))
        result = c.to_dict()["metrics"]["rows"]
        assert result["value"] == 15
        assert result["display_name"] == "Total Rows"

    def test_display_name_falls_back_to_existing_when_new_has_none(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="rows", value=10, display_name="Rows"))
        c.add_metric(Metric(name="rows", value=5))
        result = c.to_dict()["metrics"]["rows"]
        assert result["display_name"] == "Rows"


class TestArtifacts:
    """Tests for OutputCollector.add_artifact."""

    def test_add_artifact(self) -> None:
        c = OutputCollector()
        c.add_artifact(Artifact(name="logs", path="argo/workflow-123/debug.tgz"))
        assert c.to_dict() == {"artifacts": {"logs": "argo/workflow-123/debug.tgz"}}

    def test_artifact_last_write_wins(self) -> None:
        c = OutputCollector()
        c.add_artifact(Artifact(name="logs", path="old/path.tgz"))
        c.add_artifact(Artifact(name="logs", path="new/path.tgz"))
        assert c.to_dict()["artifacts"]["logs"] == "new/path.tgz"

    def test_artifact_with_display_name(self) -> None:
        c = OutputCollector()
        c.add_artifact(
            Artifact(name="logs", path="argo/debug.tgz", display_name="Debug Logs")
        )
        result = c.to_dict()["artifacts"]["logs"]
        assert result == {"path": "argo/debug.tgz", "display_name": "Debug Logs"}

    def test_artifact_without_display_name_is_plain_path(self) -> None:
        c = OutputCollector()
        c.add_artifact(Artifact(name="report", path="argo/report.csv"))
        assert c.to_dict()["artifacts"]["report"] == "argo/report.csv"


class TestToDict:
    """Tests for OutputCollector.to_dict."""

    def test_empty_collector_returns_empty_dict(self) -> None:
        c = OutputCollector()
        assert c.to_dict() == {}

    def test_only_metrics_omits_artifacts_key(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="rows", value=1))
        result = c.to_dict()
        assert "artifacts" not in result
        assert "metrics" in result

    def test_only_artifacts_omits_metrics_key(self) -> None:
        c = OutputCollector()
        c.add_artifact(Artifact(name="f", path="p"))
        result = c.to_dict()
        assert "metrics" not in result
        assert "artifacts" in result

    def test_both_present(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="n", value=1))
        c.add_artifact(Artifact(name="f", path="p"))
        result = c.to_dict()
        assert "metrics" in result
        assert "artifacts" in result


class TestMerge:
    """Tests for OutputCollector.merge."""

    def test_merge_accumulates_numeric_metrics(self) -> None:
        a = OutputCollector()
        a.add_metric(Metric(name="rows", value=100))

        b = OutputCollector()
        b.add_metric(Metric(name="rows", value=50))

        a.merge(b)
        assert a.to_dict()["metrics"]["rows"] == 150

    def test_merge_transfers_artifacts(self) -> None:
        a = OutputCollector()
        b = OutputCollector()
        b.add_artifact(Artifact(name="f", path="p"))

        a.merge(b)
        assert "f" in a.to_dict()["artifacts"]

    def test_merge_does_not_modify_other(self) -> None:
        a = OutputCollector()
        a.add_metric(Metric(name="rows", value=10))

        b = OutputCollector()
        b.add_metric(Metric(name="rows", value=5))

        a.merge(b)
        # b should be unchanged
        assert b.to_dict()["metrics"]["rows"] == 5


class TestMergeWith:
    """Tests for OutputCollector.merge_with."""

    def test_merge_with_combines_base_and_outputs(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="rows", value=100))

        base = {"status": "ok", "count": 42}
        result = c.merge_with(base)

        assert result["status"] == "ok"
        assert result["count"] == 42
        assert result["metrics"]["rows"] == 100

    def test_merge_with_does_not_mutate_base(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="n", value=1))
        base = {"k": "v"}
        c.merge_with(base)
        assert "metrics" not in base

    def test_merge_with_empty_collector_returns_base_unchanged(self) -> None:
        c = OutputCollector()
        base = {"x": 1}
        result = c.merge_with(base)
        assert result == {"x": 1}


class TestHasData:
    """Tests for OutputCollector.has_data."""

    def test_empty_returns_false(self) -> None:
        assert OutputCollector().has_data() is False

    def test_with_metric_returns_true(self) -> None:
        c = OutputCollector()
        c.add_metric(Metric(name="n", value=1))
        assert c.has_data() is True

    def test_with_artifact_returns_true(self) -> None:
        c = OutputCollector()
        c.add_artifact(Artifact(name="f", path="p"))
        assert c.has_data() is True
