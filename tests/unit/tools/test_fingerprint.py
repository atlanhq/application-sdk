"""Tests for F3: fingerprint_connector."""

from __future__ import annotations

from pathlib import Path

from tools.migrate_v3.fingerprint import fingerprint_connector


def _write(tmp_path: Path, filename: str, source: str) -> Path:
    p = tmp_path / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# v2 class detection
# ---------------------------------------------------------------------------


class TestV2Detection:
    def test_incremental_sql_detected(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "from somewhere import IncrementalSQLMetadataExtractionWorkflow\n"
            "class MyConnector(IncrementalSQLMetadataExtractionWorkflow): pass\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "incremental_sql"
        assert result.confidence == 1.0
        assert not result.already_migrated

    def test_sql_metadata_via_workflow(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "from somewhere import BaseSQLMetadataExtractionWorkflow\n"
            "class MyConnector(BaseSQLMetadataExtractionWorkflow): pass\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "sql_metadata"
        assert result.confidence == 1.0

    def test_sql_metadata_via_activities(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "from somewhere import BaseSQLMetadataExtractionActivities\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "sql_metadata"

    def test_sql_query_detected(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "from somewhere import SQLQueryExtractionWorkflow\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "sql_query"
        assert result.confidence == 1.0

    def test_sql_metadata_app_confidence_0_9(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "app/main.py",
            "from somewhere import BaseSQLMetadataExtractionApplication\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "sql_metadata"
        assert result.confidence == 0.9


class TestV2Priority:
    def test_incremental_wins_over_sql_metadata(self, tmp_path: Path) -> None:
        """Incremental connectors also extend SQL metadata classes; incremental wins."""
        _write(
            tmp_path,
            "app/workflow.py",
            "from somewhere import IncrementalSQLMetadataExtractionWorkflow\n",
        )
        _write(
            tmp_path,
            "app/activities.py",
            "from somewhere import BaseSQLMetadataExtractionActivities\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "incremental_sql"


# ---------------------------------------------------------------------------
# Already-migrated v3 detection
# ---------------------------------------------------------------------------


class TestV3Detection:
    def test_sql_metadata_extractor_already_migrated(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "from application_sdk.templates import SqlMetadataExtractor\n"
            "class MyConnector(SqlMetadataExtractor): pass\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "sql_metadata"
        assert result.already_migrated is True

    def test_incremental_sql_metadata_extractor_already_migrated(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "from application_sdk.templates import IncrementalSqlMetadataExtractor\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "incremental_sql"
        assert result.already_migrated is True

    def test_sql_query_extractor_already_migrated(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "from application_sdk.templates import SqlQueryExtractor\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "sql_query"
        assert result.already_migrated is True


# ---------------------------------------------------------------------------
# Test-file exclusion
# ---------------------------------------------------------------------------


class TestTestFileExclusion:
    def test_test_files_excluded(self, tmp_path: Path) -> None:
        # Only a test file contains the v2 class — should NOT be detected.
        _write(
            tmp_path,
            "tests/test_connector.py",
            "from somewhere import BaseSQLMetadataExtractionWorkflow\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "custom"

    def test_prod_file_wins_over_test(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "from somewhere import SQLQueryExtractionWorkflow\n",
        )
        _write(
            tmp_path,
            "tests/test_connector.py",
            "from somewhere import BaseSQLMetadataExtractionWorkflow\n",
        )
        result = fingerprint_connector(tmp_path)
        # Only the prod file counts — sql_query, not sql_metadata.
        assert result.connector_type == "sql_query"


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


class TestFallback:
    def test_unknown_connector_returns_custom(self, tmp_path: Path) -> None:
        _write(tmp_path, "app/connector.py", "class MyConnector: pass\n")
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "custom"
        assert result.confidence == 0.5

    def test_empty_directory_returns_custom(self, tmp_path: Path) -> None:
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "custom"

    def test_evidence_populated_on_match(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "from somewhere import BaseSQLMetadataExtractionWorkflow\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.evidence
        assert any("BaseSQLMetadata" in e for e in result.evidence)


# ---------------------------------------------------------------------------
# Single-file input
# ---------------------------------------------------------------------------


class TestSingleFile:
    def test_single_file_detected(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            "connector.py",
            "from somewhere import SQLQueryExtractionActivities\n",
        )
        result = fingerprint_connector(p)
        assert result.connector_type == "sql_query"
