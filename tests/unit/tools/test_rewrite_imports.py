"""Unit tests for tools/migrate_v3/rewrite_imports.py.

Each test feeds a snippet of v2 source through ``rewrite_file_source`` and
asserts that the output contains the expected v3 import (and optionally a
TODO comment).  The original source is never written to disk.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import libcst as cst

from tools.migrate_v3.rewrite_imports import V3ImportRewriter, rewrite_internal_imports

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def rewrite(source: str) -> tuple[str, list[str]]:
    """
    Apply V3ImportRewriter to *source* and return (new_source, changes).

    Strips leading/trailing whitespace from *source* before parsing so that
    test strings written with ``textwrap.dedent`` work cleanly.
    """
    source = textwrap.dedent(source).strip() + "\n"
    tree = cst.parse_module(source)
    rewriter = V3ImportRewriter()
    new_tree = tree.visit(rewriter)
    return new_tree.code, rewriter.changes


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSimpleSymbolRename:
    """Single-symbol imports whose symbol name changes."""

    def test_activity_statistics_renamed(self):
        src = "from application_sdk.activities.common.models import ActivityStatistics"
        out, changes = rewrite(src)
        assert "from application_sdk.common.models import TaskStatistics" in out
        assert any("ActivityStatistics" in c for c in changes)

    def test_activity_result_renamed(self):
        src = "from application_sdk.activities.common.models import ActivityResult"
        out, changes = rewrite(src)
        assert "from application_sdk.common.models import TaskResult" in out

    def test_handler_interface_renamed(self):
        src = "from application_sdk.handlers import HandlerInterface"
        out, changes = rewrite(src)
        assert "from application_sdk.handler import Handler" in out
        assert "HandlerInterface" not in out

    def test_base_handler_renamed(self):
        src = "from application_sdk.handlers.base import BaseHandler"
        out, changes = rewrite(src)
        assert "from application_sdk.handler.base import DefaultHandler" in out

    def test_mock_credential_store(self):
        src = "from application_sdk.test_utils.credentials import MockCredentialStore"
        out, changes = rewrite(src)
        assert "from application_sdk.testing import MockCredentialStore" in out


class TestModulePathChange:
    """Imports where only the module path changes (symbol name unchanged)."""

    def test_interceptors_models(self):
        src = "from application_sdk.interceptors.models import WorkflowEvent"
        out, changes = rewrite(src)
        assert "from application_sdk.contracts.events import WorkflowEvent" in out

    def test_interceptors_events(self):
        src = "from application_sdk.interceptors.events import EventInterceptor"
        out, changes = rewrite(src)
        # Interceptors are auto-registered by create_worker(); the rewriter keeps
        # the module path unchanged and emits a structural TODO to remove the import.
        assert "from application_sdk.interceptors.events import EventInterceptor" in out
        assert "# TODO(upgrade-v3): Remove this import" in out

    def test_activity_sql_utils(self):
        src = "from application_sdk.activities.common.sql_utils import some_util"
        out, changes = rewrite(src)
        assert "from application_sdk.common.sql_utils import some_util" in out

    def test_clients_temporal(self):
        src = "from application_sdk.clients.temporal import TemporalWorkflowClient"
        out, changes = rewrite(src)
        assert "from application_sdk.execution import TemporalWorkflowClient" in out

    def test_test_utils_scale_data_generator(self):
        src = "from application_sdk.test_utils.scale_data_generator import ScaleDataGenerator"
        out, changes = rewrite(src)
        assert (
            "from application_sdk.testing.scale_data_generator import ScaleDataGenerator"
            in out
        )


class TestMultipleSymbols:
    """from X import Y, Z — multiple symbols in one statement."""

    def test_both_activity_models_renamed(self):
        src = "from application_sdk.activities.common.models import ActivityStatistics, ActivityResult"
        out, changes = rewrite(src)
        assert (
            "from application_sdk.common.models import TaskStatistics, TaskResult"
            in out
        )

    def test_multi_same_new_module(self):
        # Both symbols map to application_sdk.testing (unchanged names)
        src = "from application_sdk.test_utils import MockStateStore, MockSecretStore"
        out, changes = rewrite(src)
        assert (
            "from application_sdk.testing import MockStateStore, MockSecretStore" in out
        )


class TestAliasedImport:
    """from X import Y as Z — alias must be preserved."""

    def test_symbol_rename_preserves_alias(self):
        src = "from application_sdk.activities.common.models import ActivityStatistics as AS"
        out, changes = rewrite(src)
        # Symbol renamed to TaskStatistics but alias kept
        assert "import TaskStatistics as AS" in out

    def test_module_only_change_preserves_alias(self):
        src = "from application_sdk.handlers import HandlerInterface as HI"
        out, changes = rewrite(src)
        assert "from application_sdk.handler import Handler as HI" in out


class TestStructuralImports:
    """Imports that require structural work should emit TODO comments."""

    def test_workflow_interface_gets_todo(self):
        src = "from application_sdk.workflows import WorkflowInterface"
        out, changes = rewrite(src)
        assert "from application_sdk.app import App" in out
        assert "# TODO(upgrade-v3):" in out

    def test_sql_workflow_gets_todo(self):
        src = "from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow"
        out, changes = rewrite(src)
        assert "from application_sdk.templates import SqlMetadataExtractor" in out
        assert "# TODO(upgrade-v3):" in out

    def test_activities_interface_gets_todo(self):
        src = "from application_sdk.activities import ActivitiesInterface"
        out, changes = rewrite(src)
        assert "from application_sdk.app import App" in out
        assert "# TODO(upgrade-v3):" in out

    def test_auto_heartbeater_gets_todo(self):
        src = "from application_sdk.activities.common.utils import auto_heartbeater"
        out, changes = rewrite(src)
        assert "# TODO(upgrade-v3):" in out
        # Structural — removed decorator, replaced by @task built-in
        assert any("auto_heartbeater" in c for c in changes)

    def test_worker_gets_todo(self):
        src = "from application_sdk.worker import Worker"
        out, changes = rewrite(src)
        assert "from application_sdk.execution import create_worker" in out
        assert "# TODO(upgrade-v3):" in out


class TestNonDeprecatedImports:
    """Non-deprecated imports must be left completely unchanged."""

    def test_temporalio_untouched(self):
        src = "from temporalio import activity, workflow"
        out, changes = rewrite(src)
        assert out.strip() == src.strip()
        assert changes == []

    def test_stdlib_untouched(self):
        src = "from typing import Any, Dict"
        out, changes = rewrite(src)
        assert out.strip() == src.strip()
        assert changes == []

    def test_non_sdk_untouched(self):
        src = "from my_connector.utils import helper"
        out, changes = rewrite(src)
        assert out.strip() == src.strip()
        assert changes == []

    def test_v3_sdk_untouched(self):
        src = "from application_sdk.app import App, task"
        out, changes = rewrite(src)
        assert out.strip() == src.strip()
        assert changes == []


class TestFullFileRewrite:
    """Integration-style test: multi-import file preserves non-import code."""

    def test_mixed_file(self):
        src = """\
            import os
            from typing import Dict, Any

            from application_sdk.workflows import WorkflowInterface
            from application_sdk.activities import ActivitiesInterface
            from application_sdk.activities.common.models import ActivityStatistics
            from application_sdk.handlers import HandlerInterface

            APPLICATION_NAME = "my-connector"

            class MyWorkflow(WorkflowInterface):
                pass
        """
        out, changes = rewrite(src)
        # All four deprecated imports rewritten
        assert "from application_sdk.app import App" in out
        assert "from application_sdk.common.models import TaskStatistics" in out
        assert "from application_sdk.handler import Handler" in out
        # Non-import code preserved
        assert 'APPLICATION_NAME = "my-connector"' in out
        assert "class MyWorkflow" in out
        # os and typing untouched
        assert "import os" in out
        assert "from typing import Dict, Any" in out
        # Changes logged
        assert len(changes) >= 4


# ---------------------------------------------------------------------------
# Tests for rewrite_internal_imports()
# ---------------------------------------------------------------------------


class TestInternalImportRewriter:
    """Tests for rewrite_internal_imports() — fixes imports after dir consolidation."""

    def test_rewrites_module_path(self, tmp_path: Path) -> None:
        src = "from app.activities.metadata_extraction import MetadataExtractor\n"
        f = tmp_path / "test_foo.py"
        f.write_text(src, encoding="utf-8")
        mapping = {"app.activities.metadata_extraction": "app.metadata_extraction"}
        rewrite_internal_imports(tmp_path, mapping)
        result = f.read_text(encoding="utf-8")
        assert "from app.metadata_extraction import MetadataExtractor" in result

    def test_symbol_name_preserved(self, tmp_path: Path) -> None:
        src = "from app.activities.foo import FooActivities\n"
        f = tmp_path / "test.py"
        f.write_text(src, encoding="utf-8")
        mapping = {"app.activities.foo": "app.foo"}
        rewrite_internal_imports(tmp_path, mapping)
        result = f.read_text(encoding="utf-8")
        assert "from app.foo import FooActivities" in result

    def test_non_matching_import_unchanged(self, tmp_path: Path) -> None:
        src = "from app.utils import helper\n"
        f = tmp_path / "test.py"
        f.write_text(src, encoding="utf-8")
        mapping = {"app.activities.foo": "app.foo"}
        results = rewrite_internal_imports(tmp_path, mapping)
        assert results == {}
        assert f.read_text(encoding="utf-8") == src

    def test_multiple_files_in_directory(self, tmp_path: Path) -> None:
        mapping = {"app.activities.foo": "app.foo"}
        for i in range(3):
            f = tmp_path / f"test_{i}.py"
            f.write_text(f"from app.activities.foo import Foo{i}\n", encoding="utf-8")
        results = rewrite_internal_imports(tmp_path, mapping)
        assert len(results) == 3

    def test_empty_mapping_changes_nothing(self, tmp_path: Path) -> None:
        src = "from app.activities.foo import Foo\n"
        f = tmp_path / "test.py"
        f.write_text(src, encoding="utf-8")
        results = rewrite_internal_imports(tmp_path, {})
        assert results == {}
        assert f.read_text(encoding="utf-8") == src

    def test_returns_change_descriptions(self, tmp_path: Path) -> None:
        src = "from app.activities.foo import Foo\n"
        f = tmp_path / "test.py"
        f.write_text(src, encoding="utf-8")
        mapping = {"app.activities.foo": "app.foo"}
        results = rewrite_internal_imports(tmp_path, mapping)
        assert f in results
        assert any("app.activities.foo" in c for c in results[f])
        assert any("app.foo" in c for c in results[f])

    def test_multiple_symbols_same_module(self, tmp_path: Path) -> None:
        src = "from app.activities.foo import Bar, Baz\n"
        f = tmp_path / "test.py"
        f.write_text(src, encoding="utf-8")
        mapping = {"app.activities.foo": "app.foo"}
        rewrite_internal_imports(tmp_path, mapping)
        result = f.read_text(encoding="utf-8")
        assert "from app.foo import Bar, Baz" in result

    def test_single_file_target(self, tmp_path: Path) -> None:
        src = "from app.activities.bar import BarClass\n"
        f = tmp_path / "test.py"
        f.write_text(src, encoding="utf-8")
        mapping = {"app.activities.bar": "app.bar"}
        results = rewrite_internal_imports(f, mapping)
        assert f in results
        assert "from app.bar import BarClass" in f.read_text(encoding="utf-8")
