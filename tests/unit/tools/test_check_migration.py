"""Unit tests for tools/migrate_v3/check_migration.py.

Each test constructs a minimal Python snippet (or a temporary directory tree),
runs the checker, and asserts on the resulting CheckResult list / advisories.
"""

from __future__ import annotations

from pathlib import Path

from tools.migrate_v3.check_migration import FAIL, WARN, check_directory, check_file

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, filename: str, source: str) -> Path:
    """Write *source* to *tmp_path/filename* and return the Path."""
    p = tmp_path / filename
    p.write_text(source, encoding="utf-8")
    return p


def _rules(results: list) -> set[str]:
    return {r.rule for r in results}


def _levels(results: list) -> set[str]:
    return {r.level for r in results}


# ---------------------------------------------------------------------------
# Check A: handler-typed-signatures
# ---------------------------------------------------------------------------


class TestHandlerTypedSignatures:
    """FAIL fires when a Handler subclass still uses *args / **kwargs."""

    _HANDLER_BASE = "from application_sdk.handler import Handler\n\n"

    def test_handler_kwargs_detected_test_auth(self, tmp_path: Path) -> None:
        source = (
            self._HANDLER_BASE + "class MyHandler(Handler):\n"
            "    async def test_auth(self, *args, **kwargs) -> bool:\n"
            "        return True\n"
        )
        path = _write(tmp_path, "handler.py", source)
        results = check_file(path)
        assert any(
            r.rule == "handler-typed-signatures" and r.level == FAIL for r in results
        )

    def test_handler_kwargs_detected_preflight(self, tmp_path: Path) -> None:
        source = (
            self._HANDLER_BASE + "class MyHandler(Handler):\n"
            "    async def preflight_check(self, **kwargs):\n"
            "        pass\n"
        )
        path = _write(tmp_path, "handler.py", source)
        results = check_file(path)
        assert any(
            r.rule == "handler-typed-signatures" and r.level == FAIL for r in results
        )

    def test_handler_kwargs_detected_fetch_metadata(self, tmp_path: Path) -> None:
        source = (
            self._HANDLER_BASE + "class MyHandler(Handler):\n"
            "    async def fetch_metadata(self, *args, **kwargs):\n"
            "        pass\n"
        )
        path = _write(tmp_path, "handler.py", source)
        results = check_file(path)
        assert any(
            r.rule == "handler-typed-signatures" and r.level == FAIL for r in results
        )

    def test_handler_typed_signature_passes(self, tmp_path: Path) -> None:
        source = (
            self._HANDLER_BASE
            + "from application_sdk.handler.contracts import AuthInput, AuthOutput\n\n"
            "class MyHandler(Handler):\n"
            "    async def test_auth(self, input: AuthInput) -> AuthOutput:\n"
            "        return AuthOutput(status='success')\n"
        )
        path = _write(tmp_path, "handler.py", source)
        results = check_file(path)
        assert not any(r.rule == "handler-typed-signatures" for r in results)

    def test_handler_kwargs_only_in_handler_files(self, tmp_path: Path) -> None:
        """Non-handler files with test_auth(**kwargs) must NOT trigger the rule."""
        source = (
            "# utility module — not a Handler subclass\n"
            "async def test_auth(self, **kwargs) -> bool:\n"
            "    return True\n"
        )
        path = _write(tmp_path, "utils.py", source)
        results = check_file(path)
        assert not any(r.rule == "handler-typed-signatures" for r in results)


# ---------------------------------------------------------------------------
# Check B: no-unbounded-escape-hatch
# ---------------------------------------------------------------------------


class TestNoUnboundedEscapeHatch:
    """FAIL fires whenever allow_unbounded_fields=True appears in connector code."""

    def test_unbounded_escape_hatch_detected(self, tmp_path: Path) -> None:
        source = (
            "from application_sdk.contracts.base import Input\n\n"
            "class MyInput(Input, allow_unbounded_fields=True):\n"
            "    items: list[str]\n"
        )
        path = _write(tmp_path, "contracts.py", source)
        results = check_file(path)
        assert any(
            r.rule == "no-unbounded-escape-hatch" and r.level == FAIL for r in results
        )

    def test_unbounded_escape_hatch_with_spaces(self, tmp_path: Path) -> None:
        source = (
            "from application_sdk.contracts.base import Input\n\n"
            "class MyInput(Input, allow_unbounded_fields = True):\n"
            "    pass\n"
        )
        path = _write(tmp_path, "contracts.py", source)
        results = check_file(path)
        assert any(
            r.rule == "no-unbounded-escape-hatch" and r.level == FAIL for r in results
        )

    def test_no_escape_hatch_passes(self, tmp_path: Path) -> None:
        source = (
            "from application_sdk.contracts.base import Input\n"
            "from typing import Annotated\n"
            "from application_sdk.contracts.types import MaxItems\n\n"
            "class MyInput(Input):\n"
            "    items: Annotated[list[str], MaxItems(1000)]\n"
        )
        path = _write(tmp_path, "contracts.py", source)
        results = check_file(path)
        assert not any(r.rule == "no-unbounded-escape-hatch" for r in results)

    def test_escape_hatch_false_does_not_trigger(self, tmp_path: Path) -> None:
        source = (
            "from application_sdk.contracts.base import Input\n\n"
            "class MyInput(Input, allow_unbounded_fields=False):\n"
            "    pass\n"
        )
        path = _write(tmp_path, "contracts.py", source)
        results = check_file(path)
        assert not any(r.rule == "no-unbounded-escape-hatch" for r in results)


# ---------------------------------------------------------------------------
# Check C: no-v2-directory-structure
# ---------------------------------------------------------------------------


class TestNoV2DirectoryStructure:
    """WARN fires when activities/ or workflows/ directories are still present."""

    def test_activities_dir_detected(self, tmp_path: Path) -> None:
        activities_dir = tmp_path / "app" / "activities"
        activities_dir.mkdir(parents=True)
        (activities_dir / "my_connector.py").write_text(
            "class MyConnector: pass\n", encoding="utf-8"
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=False, entry_point_required=False
        )
        assert any(
            "no-v2-directory-structure" in a and "activities" in a for a in advisories
        )

    def test_workflows_dir_detected(self, tmp_path: Path) -> None:
        workflows_dir = tmp_path / "app" / "workflows"
        workflows_dir.mkdir(parents=True)
        (workflows_dir / "my_workflow.py").write_text("# re-export\n", encoding="utf-8")
        _, advisories = check_directory(
            tmp_path, app_subclass_required=False, entry_point_required=False
        )
        assert any(
            "no-v2-directory-structure" in a and "workflows" in a for a in advisories
        )

    def test_no_v2_dirs_clean(self, tmp_path: Path) -> None:
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "my_connector.py").write_text(
            "from application_sdk.templates import SqlMetadataExtractor\n\n"
            "class MyConnector(SqlMetadataExtractor): pass\n",
            encoding="utf-8",
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=False, entry_point_required=False
        )
        assert not any("no-v2-directory-structure" in a for a in advisories)


# ---------------------------------------------------------------------------
# Check D: app-subclass-missing excludes test files
# ---------------------------------------------------------------------------


class TestAppSubclassMissingExcludesTests:
    """WARN for app-subclass-missing should only look at production code."""

    def test_app_subclass_in_prod_not_warned(self, tmp_path: Path) -> None:
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "my_connector.py").write_text(
            "from application_sdk.templates import SqlMetadataExtractor\n\n"
            "class MyConnector(SqlMetadataExtractor): pass\n",
            encoding="utf-8",
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=True, entry_point_required=False
        )
        assert not any("app-subclass-missing" in a for a in advisories)

    def test_app_subclass_only_in_tests_is_warned(self, tmp_path: Path) -> None:
        # App class only in test file — should still WARN because prod has no App subclass.
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_connector.py").write_text(
            "from application_sdk.templates import SqlMetadataExtractor\n\n"
            "class MockConnector(SqlMetadataExtractor): pass\n",
            encoding="utf-8",
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=True, entry_point_required=False
        )
        assert any("app-subclass-missing" in a for a in advisories)

    def test_multiline_class_signature_detected(self, tmp_path: Path) -> None:
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "my_connector.py").write_text(
            "from application_sdk.templates import SqlMetadataExtractor\n\n"
            "class MyConnector(\n    SqlMetadataExtractor\n): pass\n",
            encoding="utf-8",
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=True, entry_point_required=False
        )
        assert not any("app-subclass-missing" in a for a in advisories)

    def test_app_subclass_with_mixin_detected(self, tmp_path: Path) -> None:
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "my_connector.py").write_text(
            "from application_sdk.app import App\n\n"
            "class MyConnector(App, SomeMixin): pass\n",
            encoding="utf-8",
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=True, entry_point_required=False
        )
        assert not any("app-subclass-missing" in a for a in advisories)


# ---------------------------------------------------------------------------
# Check E: entry-point detection expansion
# ---------------------------------------------------------------------------


class TestEntryPointExpansion:
    """Entry-point check should detect CLI references in Dockerfile and pyproject.toml."""

    def test_entry_point_in_py_file(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(
            "from application_sdk.main import run_dev_combined\n"
            "import asyncio\nasyncio.run(run_dev_combined(MyApp))\n",
            encoding="utf-8",
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=False, entry_point_required=True
        )
        assert not any("entry-point" in a for a in advisories)

    def test_entry_point_in_dockerfile(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text(
            'FROM python:3.11\nCMD ["application-sdk", "--mode", "combined"]\n',
            encoding="utf-8",
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=False, entry_point_required=True
        )
        assert not any("entry-point" in a for a in advisories)

    def test_entry_point_in_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project.scripts]\nmy-connector = "application_sdk --mode combined"\n',
            encoding="utf-8",
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=False, entry_point_required=True
        )
        assert not any("entry-point" in a for a in advisories)

    def test_entry_point_missing_warns(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("class MyApp: pass\n", encoding="utf-8")
        _, advisories = check_directory(
            tmp_path, app_subclass_required=False, entry_point_required=True
        )
        assert any("entry-point" in a for a in advisories)

    def test_entry_point_only_in_tests_is_warned(self, tmp_path: Path) -> None:
        # Entry point in a test file should not suppress the WARN.
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_main.py").write_text(
            "from application_sdk.main import run_dev_combined\n",
            encoding="utf-8",
        )
        _, advisories = check_directory(
            tmp_path, app_subclass_required=False, entry_point_required=True
        )
        assert any("entry-point" in a for a in advisories)


# ---------------------------------------------------------------------------
# Check F: response-format-change WARN
# ---------------------------------------------------------------------------


class TestResponseFormatChange:
    """WARN fires when a Handler subclass defines fetch_metadata or preflight_check."""

    _HANDLER_BASE = "from application_sdk.handler import Handler\n\n"

    def test_fetch_metadata_warns(self, tmp_path: Path) -> None:
        source = (
            self._HANDLER_BASE + "class MyHandler(Handler):\n"
            "    async def fetch_metadata(self, input):\n"
            "        return []\n"
        )
        path = _write(tmp_path, "handler.py", source)
        results = check_file(path)
        assert any(
            r.rule == "response-format-change" and r.level == WARN for r in results
        )

    def test_preflight_check_warns(self, tmp_path: Path) -> None:
        source = (
            self._HANDLER_BASE + "class MyHandler(Handler):\n"
            "    async def preflight_check(self, input):\n"
            "        return {}\n"
        )
        path = _write(tmp_path, "handler.py", source)
        results = check_file(path)
        assert any(
            r.rule == "response-format-change" and r.level == WARN for r in results
        )

    def test_both_methods_emit_two_warns(self, tmp_path: Path) -> None:
        source = (
            self._HANDLER_BASE + "class MyHandler(Handler):\n"
            "    async def fetch_metadata(self, input):\n"
            "        return []\n"
            "    async def preflight_check(self, input):\n"
            "        return {}\n"
        )
        path = _write(tmp_path, "handler.py", source)
        results = check_file(path)
        rfc_results = [r for r in results if r.rule == "response-format-change"]
        assert len(rfc_results) == 2

    def test_non_handler_file_no_warn(self, tmp_path: Path) -> None:
        source = (
            "class MyClass:\n"
            "    async def fetch_metadata(self, input):\n"
            "        return []\n"
        )
        path = _write(tmp_path, "other.py", source)
        results = check_file(path)
        assert not any(r.rule == "response-format-change" for r in results)

    def test_fetch_metadata_message_mentions_flat_list(self, tmp_path: Path) -> None:
        source = (
            self._HANDLER_BASE + "class MyHandler(Handler):\n"
            "    async def fetch_metadata(self, input):\n"
            "        return []\n"
        )
        path = _write(tmp_path, "handler.py", source)
        results = check_file(path)
        rfc = next(r for r in results if r.rule == "response-format-change")
        assert "flat list" in rfc.message or "MetadataOutput" in rfc.message

    def test_preflight_message_mentions_output_type(self, tmp_path: Path) -> None:
        source = (
            self._HANDLER_BASE + "class MyHandler(Handler):\n"
            "    async def preflight_check(self, input):\n"
            "        return {}\n"
        )
        path = _write(tmp_path, "handler.py", source)
        results = check_file(path)
        rfc = next(r for r in results if r.rule == "response-format-change")
        assert "PreflightOutput" in rfc.message or "authenticationCheck" in rfc.message


# ---------------------------------------------------------------------------
# Check G: no-base-application (E5)
# ---------------------------------------------------------------------------


class TestNoBaseApplication:
    """FAIL fires when BaseApplication( is found."""

    def test_base_application_detected(self, tmp_path: Path) -> None:
        source = (
            "from application_sdk.application import BaseApplication\n\n"
            "app = BaseApplication(MyWorkflow, MyActivities)\n"
            "app.run()\n"
        )
        path = _write(tmp_path, "main.py", source)
        results = check_file(path)
        assert any(r.rule == "no-base-application" and r.level == FAIL for r in results)

    def test_base_application_with_args_detected(self, tmp_path: Path) -> None:
        source = "result = BaseApplication(workflow=Wf, activities=[Act])\n"
        path = _write(tmp_path, "main.py", source)
        results = check_file(path)
        assert any(r.rule == "no-base-application" for r in results)

    def test_clean_run_dev_combined_passes(self, tmp_path: Path) -> None:
        source = (
            "from application_sdk.main import run_dev_combined\n"
            "import asyncio\nasyncio.run(run_dev_combined(MyApp))\n"
        )
        path = _write(tmp_path, "main.py", source)
        results = check_file(path)
        assert not any(r.rule == "no-base-application" for r in results)

    def test_base_application_class_name_without_call_not_triggered(
        self, tmp_path: Path
    ) -> None:
        # "BaseApplication" as an import name (without call parens) should not trigger.
        source = "from application_sdk.application import BaseApplication\n"
        path = _write(tmp_path, "main.py", source)
        results = check_file(path)
        assert not any(r.rule == "no-base-application" for r in results)


# ---------------------------------------------------------------------------
# Check H: no-dapr-client (E5)
# ---------------------------------------------------------------------------


class TestNoDaprClient:
    """FAIL fires for DaprClient() in production files; skipped in test files."""

    def test_dapr_client_detected_in_prod(self, tmp_path: Path) -> None:
        source = "from dapr.clients import DaprClient\n\n" "client = DaprClient()\n"
        path = _write(tmp_path, "service.py", source)
        results = check_file(path, is_test=False)
        assert any(r.rule == "no-dapr-client" and r.level == FAIL for r in results)

    def test_dapr_client_skipped_in_test_files(self, tmp_path: Path) -> None:
        source = "client = DaprClient()\n"
        path = _write(tmp_path, "test_service.py", source)
        # Pass is_test=True explicitly
        results = check_file(path, is_test=True)
        assert not any(r.rule == "no-dapr-client" for r in results)

    def test_dapr_client_skipped_via_check_directory(self, tmp_path: Path) -> None:
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_dapr.py").write_text(
            "from dapr.clients import DaprClient\nclient = DaprClient()\n",
            encoding="utf-8",
        )
        results, _ = check_directory(
            tmp_path, app_subclass_required=False, entry_point_required=False
        )
        assert not any(r.rule == "no-dapr-client" for r in results)

    def test_no_dapr_client_passes(self, tmp_path: Path) -> None:
        source = "class MyService:\n    pass\n"
        path = _write(tmp_path, "service.py", source)
        results = check_file(path)
        assert not any(r.rule == "no-dapr-client" for r in results)


# ---------------------------------------------------------------------------
# Check I: no-temporalio-direct-import (E5)
# ---------------------------------------------------------------------------


class TestNoTemporalioDirectImport:
    """FAIL fires for 'from temporalio import workflow' or 'from temporalio import activity'."""

    def test_workflow_import_detected(self, tmp_path: Path) -> None:
        source = "from temporalio import workflow\n"
        path = _write(tmp_path, "connector.py", source)
        results = check_file(path)
        assert any(
            r.rule == "no-temporalio-direct-import" and r.level == FAIL for r in results
        )

    def test_activity_import_detected(self, tmp_path: Path) -> None:
        source = "from temporalio import activity\n"
        path = _write(tmp_path, "connector.py", source)
        results = check_file(path)
        assert any(r.rule == "no-temporalio-direct-import" for r in results)

    def test_sub_module_import_not_triggered(self, tmp_path: Path) -> None:
        # 'from temporalio.common import ...' is fine
        source = "from temporalio.common import RetryPolicy\n"
        path = _write(tmp_path, "connector.py", source)
        results = check_file(path)
        assert not any(r.rule == "no-temporalio-direct-import" for r in results)

    def test_clean_code_passes(self, tmp_path: Path) -> None:
        source = "from application_sdk.app import App, task\n"
        path = _write(tmp_path, "connector.py", source)
        results = check_file(path)
        assert not any(r.rule == "no-temporalio-direct-import" for r in results)


# ---------------------------------------------------------------------------
# Check J: use-app-state (E5)
# ---------------------------------------------------------------------------


class TestUseAppState:
    """WARN fires when self._state is accessed directly."""

    def test_self_state_detected(self, tmp_path: Path) -> None:
        source = (
            "class MyApp:\n"
            "    async def run(self, input):\n"
            "        value = self._state.get('key')\n"
        )
        path = _write(tmp_path, "app.py", source)
        results = check_file(path)
        assert any(r.rule == "use-app-state" and r.level == WARN for r in results)

    def test_self_state_multiple_occurrences(self, tmp_path: Path) -> None:
        source = (
            "class MyApp:\n"
            "    async def run(self, input):\n"
            "        a = self._state.get('a')\n"
            "        b = self._state.get('b')\n"
        )
        path = _write(tmp_path, "app.py", source)
        results = check_file(path)
        assert sum(1 for r in results if r.rule == "use-app-state") == 2

    def test_context_state_store_passes(self, tmp_path: Path) -> None:
        source = (
            "class MyApp:\n"
            "    async def run(self, input):\n"
            "        value = await self.context.state_store.get('key')\n"
        )
        path = _write(tmp_path, "app.py", source)
        results = check_file(path)
        assert not any(r.rule == "use-app-state" for r in results)

    def test_state_in_string_fires_as_false_positive(self, tmp_path: Path) -> None:
        # The regex fires on string contents too — this is a known false positive.
        # Document it here so it's not a surprise.
        source = 'msg = "use self._state here"\n'
        path = _write(tmp_path, "app.py", source)
        check_file(path)  # just confirm it doesn't raise; no assertion on result
