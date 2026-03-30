"""Unit tests for OutputCollector and output models.

Tests the Metric and Artifact models, OutputCollector operations including
metric accumulation, artifact storage, and merge semantics.
"""

import pytest

from application_sdk.workflows.outputs import Artifact, Metric, OutputCollector


class TestMetricModel:
    """Tests for the Metric Pydantic model."""

    def test_metric_creation_with_int_value(self):
        """Test creating a metric with integer value."""
        metric = Metric(name="tables-extracted", value=150)
        assert metric.name == "tables-extracted"
        assert metric.value == 150
        assert isinstance(metric.value, int)

    def test_metric_creation_with_float_value(self):
        """Test creating a metric with float value."""
        metric = Metric(name="processing-time-ms", value=1234.56)
        assert metric.name == "processing-time-ms"
        assert metric.value == 1234.56
        assert isinstance(metric.value, float)

    def test_metric_creation_with_string_value(self):
        """Test creating a metric with string value."""
        metric = Metric(name="status", value="completed")
        assert metric.name == "status"
        assert metric.value == "completed"
        assert isinstance(metric.value, str)

    def test_metric_model_dump(self):
        """Test metric serialization to dict."""
        metric = Metric(name="count", value=42)
        data = metric.model_dump()
        assert data == {"name": "count", "value": 42, "display_name": None}

    def test_metric_with_display_name(self):
        """Test creating a metric with display_name."""
        metric = Metric(name="qi-queries-parsed", value=100, display_name="QI Queries Parsed")
        assert metric.name == "qi-queries-parsed"
        assert metric.value == 100
        assert metric.display_name == "QI Queries Parsed"


class TestArtifactModel:
    """Tests for the Artifact Pydantic model."""

    def test_artifact_creation(self):
        """Test creating an artifact reference."""
        artifact = Artifact(
            name="debug-logs", path="argo-artifacts/workflow-123/debug.tgz"
        )
        assert artifact.name == "debug-logs"
        assert artifact.path == "argo-artifacts/workflow-123/debug.tgz"

    def test_artifact_model_dump(self):
        """Test artifact serialization to dict."""
        artifact = Artifact(name="report", path="artifacts/report.pdf")
        data = artifact.model_dump()
        assert data == {"name": "report", "path": "artifacts/report.pdf", "display_name": None}

    def test_artifact_with_display_name(self):
        """Test creating an artifact with display_name."""
        artifact = Artifact(
            name="debug-logs",
            path="argo-artifacts/workflow-123/debug.tgz",
            display_name="Debug Logs"
        )
        assert artifact.name == "debug-logs"
        assert artifact.path == "argo-artifacts/workflow-123/debug.tgz"
        assert artifact.display_name == "Debug Logs"


class TestOutputCollector:
    """Tests for the OutputCollector class."""

    @pytest.fixture
    def collector(self):
        """Create a fresh OutputCollector instance."""
        return OutputCollector()

    def test_add_metric_stores_correctly(self, collector):
        """Test that add_metric stores a metric correctly."""
        metric = Metric(name="rows-processed", value=1000)
        collector.add_metric(metric)
        result = collector.to_dict()
        assert result == {"metrics": {"rows-processed": 1000}}

    def test_add_metric_with_display_name_serializes_as_object(self, collector):
        """Test that metrics with display_name serialize as objects."""
        metric = Metric(name="qi-queries-parsed", value=42, display_name="QI Queries Parsed")
        collector.add_metric(metric)
        result = collector.to_dict()
        assert result == {
            "metrics": {
                "qi-queries-parsed": {
                    "value": 42,
                    "display_name": "QI Queries Parsed",
                }
            }
        }

    def test_add_metric_sums_preserves_display_name(self, collector):
        """Test that summing metrics preserves display_name."""
        collector.add_metric(Metric(name="count", value=100, display_name="Total Count"))
        collector.add_metric(Metric(name="count", value=50))
        result = collector.to_dict()
        assert result == {
            "metrics": {
                "count": {
                    "value": 150,
                    "display_name": "Total Count",
                }
            }
        }

    def test_add_metric_sums_same_key_numerics_int(self, collector):
        """Test that numeric metrics with same key are summed (int)."""
        collector.add_metric(Metric(name="count", value=100))
        collector.add_metric(Metric(name="count", value=50))
        collector.add_metric(Metric(name="count", value=25))
        result = collector.to_dict()
        assert result == {"metrics": {"count": 175}}

    def test_add_metric_sums_same_key_numerics_float(self, collector):
        """Test that numeric metrics with same key are summed (float)."""
        collector.add_metric(Metric(name="duration", value=1.5))
        collector.add_metric(Metric(name="duration", value=2.5))
        result = collector.to_dict()
        assert result == {"metrics": {"duration": 4.0}}

    def test_add_metric_sums_mixed_int_float(self, collector):
        """Test that mixed int/float metrics are summed correctly."""
        collector.add_metric(Metric(name="total", value=100))
        collector.add_metric(Metric(name="total", value=0.5))
        result = collector.to_dict()
        assert result == {"metrics": {"total": 100.5}}

    def test_add_metric_last_write_wins_for_strings(self, collector):
        """Test that string metrics use last-write-wins semantics."""
        collector.add_metric(Metric(name="status", value="started"))
        collector.add_metric(Metric(name="status", value="running"))
        collector.add_metric(Metric(name="status", value="completed"))
        result = collector.to_dict()
        assert result == {"metrics": {"status": "completed"}}

    def test_add_metric_overwrites_when_types_differ(self, collector):
        """Test that metrics with different types use last-write-wins."""
        collector.add_metric(Metric(name="mixed", value=100))
        collector.add_metric(Metric(name="mixed", value="hundred"))
        result = collector.to_dict()
        assert result == {"metrics": {"mixed": "hundred"}}

    def test_add_artifact_stores_correctly(self, collector):
        """Test that add_artifact stores an artifact correctly."""
        artifact = Artifact(name="debug-logs", path="artifacts/debug.tgz")
        collector.add_artifact(artifact)
        result = collector.to_dict()
        assert result == {"artifacts": {"debug-logs": "artifacts/debug.tgz"}}

    def test_add_artifact_with_display_name_serializes_as_object(self, collector):
        """Test that artifacts with display_name serialize as objects."""
        artifact = Artifact(
            name="debug-logs",
            path="artifacts/debug.tgz",
            display_name="Debug Logs"
        )
        collector.add_artifact(artifact)
        result = collector.to_dict()
        assert result == {
            "artifacts": {
                "debug-logs": {
                    "path": "artifacts/debug.tgz",
                    "display_name": "Debug Logs",
                }
            }
        }

    def test_add_artifact_last_write_wins(self, collector):
        """Test that artifacts with same name use last-write-wins."""
        collector.add_artifact(Artifact(name="log", path="artifacts/log-v1.txt"))
        collector.add_artifact(Artifact(name="log", path="artifacts/log-v2.txt"))
        result = collector.to_dict()
        assert result == {"artifacts": {"log": "artifacts/log-v2.txt"}}

    def test_merge_combines_two_collectors(self, collector):
        """Test that merge combines metrics and artifacts from two collectors."""
        collector.add_metric(Metric(name="count", value=100))
        collector.add_artifact(Artifact(name="file1", path="path1"))

        other = OutputCollector()
        other.add_metric(Metric(name="count", value=50))
        other.add_metric(Metric(name="other", value=10))
        other.add_artifact(Artifact(name="file2", path="path2"))

        collector.merge(other)

        result = collector.to_dict()
        assert result["metrics"]["count"] == 150
        assert result["metrics"]["other"] == 10
        assert result["artifacts"]["file1"] == "path1"
        assert result["artifacts"]["file2"] == "path2"

    def test_merge_empty_collector(self, collector):
        """Test merging an empty collector has no effect."""
        collector.add_metric(Metric(name="count", value=100))
        other = OutputCollector()
        collector.merge(other)
        result = collector.to_dict()
        assert result == {"metrics": {"count": 100}}

    def test_to_dict_produces_correct_namespaces(self, collector):
        """Test that to_dict produces correctly namespaced output."""
        collector.add_metric(Metric(name="m1", value=1))
        collector.add_metric(Metric(name="m2", value=2))
        collector.add_artifact(Artifact(name="a1", path="p1"))
        result = collector.to_dict()
        assert "metrics" in result
        assert "artifacts" in result
        assert result["metrics"] == {"m1": 1, "m2": 2}
        assert result["artifacts"] == {"a1": "p1"}

    def test_to_dict_omits_empty_metrics(self, collector):
        """Test that to_dict omits empty metrics namespace."""
        collector.add_artifact(Artifact(name="a1", path="p1"))
        result = collector.to_dict()
        assert "metrics" not in result
        assert result == {"artifacts": {"a1": "p1"}}

    def test_to_dict_omits_empty_artifacts(self, collector):
        """Test that to_dict omits empty artifacts namespace."""
        collector.add_metric(Metric(name="m1", value=1))
        result = collector.to_dict()
        assert "artifacts" not in result
        assert result == {"metrics": {"m1": 1}}

    def test_to_dict_returns_empty_when_no_data(self, collector):
        """Test that to_dict returns empty dict when no data collected."""
        result = collector.to_dict()
        assert result == {}

    def test_merge_with_preserves_base_dict_keys(self, collector):
        """Test that merge_with preserves existing keys in base dict."""
        collector.add_metric(Metric(name="new-metric", value=42))

        base = {
            "transformed_data_prefix": "s3://bucket/path",
            "connection_qualified_name": "default/redshift/prod",
        }

        result = collector.merge_with(base)

        assert result["transformed_data_prefix"] == "s3://bucket/path"
        assert result["connection_qualified_name"] == "default/redshift/prod"
        assert result["metrics"] == {"new-metric": 42}

    def test_merge_with_empty_base(self, collector):
        """Test merge_with with empty base dict."""
        collector.add_metric(Metric(name="count", value=100))
        result = collector.merge_with({})
        assert result == {"metrics": {"count": 100}}

    def test_merge_with_empty_collector(self, collector):
        """Test merge_with when collector is empty."""
        base = {"key": "value"}
        result = collector.merge_with(base)
        assert result == {"key": "value"}

    def test_has_data_returns_false_when_empty(self, collector):
        """Test that has_data returns False for empty collector."""
        assert collector.has_data() is False

    def test_has_data_returns_true_with_metric(self, collector):
        """Test that has_data returns True when metric is added."""
        collector.add_metric(Metric(name="m", value=1))
        assert collector.has_data() is True

    def test_has_data_returns_true_with_artifact(self, collector):
        """Test that has_data returns True when artifact is added."""
        collector.add_artifact(Artifact(name="a", path="p"))
        assert collector.has_data() is True

    def test_has_data_returns_true_with_both(self, collector):
        """Test that has_data returns True with both metrics and artifacts."""
        collector.add_metric(Metric(name="m", value=1))
        collector.add_artifact(Artifact(name="a", path="p"))
        assert collector.has_data() is True

    def test_multiple_different_metrics(self, collector):
        """Test storing multiple different metrics."""
        collector.add_metric(Metric(name="tables", value=100))
        collector.add_metric(Metric(name="columns", value=500))
        collector.add_metric(Metric(name="rows", value=10000))

        result = collector.to_dict()
        assert result["metrics"]["tables"] == 100
        assert result["metrics"]["columns"] == 500
        assert result["metrics"]["rows"] == 10000

    def test_multiple_different_artifacts(self, collector):
        """Test storing multiple different artifacts."""
        collector.add_artifact(Artifact(name="logs", path="artifacts/logs.tgz"))
        collector.add_artifact(Artifact(name="report", path="artifacts/report.pdf"))

        result = collector.to_dict()
        assert result["artifacts"]["logs"] == "artifacts/logs.tgz"
        assert result["artifacts"]["report"] == "artifacts/report.pdf"
