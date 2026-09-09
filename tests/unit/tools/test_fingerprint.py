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
# Comments must not feed the regexes
# ---------------------------------------------------------------------------


class TestCommentsIgnored:
    def test_rewrite_imports_todo_comment_does_not_trigger_already_migrated(
        self, tmp_path: Path
    ) -> None:
        """Regression test.

        rewrite_imports.py (Phase 1) inserts TODO comments that name v3
        classes as suggestions, e.g.:
            # TODO(upgrade-v3): Extend BaseMetadataExtractor or
            # SqlMetadataExtractor directly.
        Immediately fingerprinting a non-SQL connector right after Phase 1
        must not read that suggestion as evidence the connector already
        extends SqlMetadataExtractor — the connector still has its v2
        @activity.defn / @workflow.defn decorators and no v3 base class in
        actual code.
        """
        _write(
            tmp_path,
            "app/activities.py",
            "# TODO(upgrade-v3): Extend BaseMetadataExtractor or SqlMetadataExtractor directly.\n"
            "from application_sdk.templates import BaseMetadataExtractor\n"
            "from temporalio import activity\n\n"
            "class MyActivities(BaseMetadataExtractionActivities):\n"
            "    @activity.defn(name='my_activity')\n"
            "    async def my_activity(self): ...\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "custom"
        assert result.already_migrated is False

    def test_comment_mentioning_v2_class_does_not_trigger_v2_detection(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "app/connector.py",
            "# see BaseSQLMetadataExtractionWorkflow for the old shape\n"
            "class MyConnector: pass\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "custom"

    def test_hash_inside_string_literal_is_not_treated_as_comment(
        self, tmp_path: Path
    ) -> None:
        """A '#' inside a string must survive stripping — only real comment
        tokens should be blanked, not everything after a bare '#' char.

        The '#' and the real import deliberately share one physical line: a
        naive per-line ``line.split('#', 1)[0]`` stripper would mangle this
        line and lose the import, while the tokenizer-based implementation
        correctly recognizes the '#' as being inside a string and leaves the
        rest of the line untouched. Splitting them across lines (as an
        earlier version of this test did) doesn't actually distinguish a
        correct implementation from a naive one.
        """
        _write(
            tmp_path,
            "app/connector.py",
            "URL = 'https://example.com/#fragment'; "
            "from somewhere import SQLQueryExtractionWorkflow\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "sql_query"

    def test_untokenizable_file_falls_back_without_crashing(
        self, tmp_path: Path
    ) -> None:
        """A file with a genuine syntax error (plausible mid-migration, e.g.
        after a partial codemod) must not crash the whole scan — it should
        fall back to matching on the raw (unstripped) text instead, for the
        safe-by-default priority 1-4 checks."""
        _write(
            tmp_path,
            "app/broken.py",
            "def f(:\n    from somewhere import SQLQueryExtractionWorkflow\n",
        )
        result = fingerprint_connector(tmp_path)
        # Falls back to raw-text matching rather than raising — still finds
        # the real (non-comment) import in the same broken file.
        assert result.connector_type == "sql_query"

    def test_todo_comment_plus_unrelated_syntax_error_does_not_trigger_already_migrated(
        self, tmp_path: Path
    ) -> None:
        """Regression test for the fallback reopening the original bug.

        A file that has BOTH a rewrite_imports.py-style TODO comment naming
        a v3 class AND an unrelated tokenizer-breaking issue elsewhere (an
        unterminated string, in this case — plausible on a file mid-
        migration) must not fall back to matching the "already migrated"
        patterns against raw, unstripped text: that would silently
        reproduce the exact false positive this whole module exists to
        prevent. The file should contribute no already-migrated evidence
        at all in this case (priority 5 is skipped, not fallen back).
        """
        _write(
            tmp_path,
            "app/activities.py",
            "# TODO(upgrade-v3): Extend BaseMetadataExtractor or "
            "SqlMetadataExtractor directly.\n"
            "from temporalio import activity\n"
            'x = "unterminated\n',
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "custom"
        assert result.already_migrated is False

    def test_docstring_code_example_does_not_trigger_already_migrated(
        self, tmp_path: Path
    ) -> None:
        """Regression test: a docstring quoting an actual code example (the
        shape this toolchain's own upgrade-guide snippets take) must not be
        mistaken for a real subclass declaration. Comment-stripping alone
        doesn't catch this — a docstring tokenizes as STRING, not COMMENT —
        so this needs the stricter comments-and-strings strip used for the
        already-migrated check specifically."""
        _write(
            tmp_path,
            "app/activities.py",
            '"""After upgrading, subclass like this:\n'
            "class MyExtractor(SqlMetadataExtractor): pass\n"
            '"""\n'
            "from temporalio import activity\n"
            "class MyActivities:\n"
            "    @activity.defn(name='x')\n"
            "    async def x(self): ...\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "custom"
        assert result.already_migrated is False

    def test_help_text_string_mentioning_v3_class_does_not_trigger_already_migrated(
        self, tmp_path: Path
    ) -> None:
        """A plain (non-docstring) string constant mentioning a v3 class name
        as prose must not trigger already-migrated detection either."""
        _write(
            tmp_path,
            "app/activities.py",
            '_HELP_TEXT = "After upgrading, subclass '
            'IncrementalSqlMetadataExtractor instead."\n'
            "from temporalio import activity\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "custom"
        assert result.already_migrated is False

    def test_real_subclass_inside_docstring_marker_but_actually_code_still_detected(
        self, tmp_path: Path
    ) -> None:
        """Sanity check the strings-blanking strip doesn't overreach: a real,
        executable subclass declaration outside any string must still be
        detected as already-migrated, even in a file that ALSO happens to
        have an unrelated docstring."""
        _write(
            tmp_path,
            "app/connector.py",
            '"""Just a module docstring, no code examples here."""\n'
            "from application_sdk.templates import SqlQueryExtractor\n"
            "class MyConnector(SqlQueryExtractor): pass\n",
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

    def test_non_utf8_file_is_skipped_not_crashed_on(self, tmp_path: Path) -> None:
        """A stray non-UTF-8 .py file under the scan root (e.g. a fixture
        with raw bytes, or a file with an unusual encoding) must be skipped
        like any other unreadable file, not crash the whole scan.
        UnicodeDecodeError is NOT a subclass of OSError, so the read needs
        its own explicit handling alongside the OSError catch."""
        bad = tmp_path / "app" / "bad_encoding.py"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"\xff\xfe# not valid utf-8\n")
        _write(
            tmp_path,
            "app/connector.py",
            "from somewhere import SQLQueryExtractionWorkflow\n",
        )
        result = fingerprint_connector(tmp_path)
        assert result.connector_type == "sql_query"


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
