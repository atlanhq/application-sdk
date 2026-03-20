"""Unit tests for tools/migrate_v3/check_migration.py.

Each test constructs a minimal Python snippet (or a temporary directory tree),
runs the checker, and asserts on the resulting CheckResult list / advisories.
"""

from __future__ import annotations

from pathlib import Path

from tools.migrate_v3.check_migration import FAIL, check_directory, check_file

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
            "from dataclasses import dataclass\n"
            "from application_sdk.contracts.base import Input\n"
            "from typing import Annotated\n"
            "from application_sdk.contracts.types import MaxItems\n\n"
            "@dataclass\n"
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
