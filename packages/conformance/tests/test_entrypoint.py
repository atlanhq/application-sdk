"""Tests for P-series entrypoint-conformance checks (P017–P018, BLDX-1411).

Both rules are per-file (no cross-file resolution), so scan_text is sufficient
for all tests.  A separate discovery test confirms test files are included in
the scan walk.

Test structure per rule:
- Fires on each covered violation class
- Stays silent on conformant patterns (false-positive guards)
- Inline suppression is honoured
- Tier and disposition assertions (WARN → exit 0 → Disposition.WARNING)
- SARIF result count
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.entrypoint import SERIES, discover, main, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema import SarifReport, derive_disposition
from conformance.suite.schema.disposition import Disposition, EnforcementTier

# ── helpers ───────────────────────────────────────────────────────────────────


def _rule(src: str, rule_id: str, file: str = "app/connector.py") -> list:
    """All findings of *rule_id* from a per-file scan of *src*."""
    return [f for f in scan_text(src, file) if f.rule_id == rule_id]


def _ids(src: str, file: str = "app/connector.py") -> list[str]:
    """All rule IDs emitted from a per-file scan of *src*."""
    return [f.rule_id for f in scan_text(src, file)]


# ── series metadata ───────────────────────────────────────────────────────────


def test_series_letter() -> None:
    assert SERIES == "P"


# ── P017 ManualWorkerBootstrap ────────────────────────────────────────────────


class TestP017WorkerConstructionCalls:
    """Violation class (b): calls to construction symbols from application_sdk.execution."""

    def test_fires_on_create_worker_call(self) -> None:
        src = (
            "from application_sdk.execution import create_worker\n"
            "worker = create_worker(client=client, task_queue='my-app')\n"
        )
        fs = _rule(src, "P017")
        assert len(fs) == 1
        assert "create_worker" in fs[0].message

    def test_fires_on_create_temporal_client_call(self) -> None:
        src = (
            "from application_sdk.execution import create_temporal_client\n"
            "client = create_temporal_client(host='localhost')\n"
        )
        assert len(_rule(src, "P017")) == 1

    def test_fires_on_appworker_call(self) -> None:
        src = (
            "from application_sdk.execution import AppWorker\n"
            "w = AppWorker(client=client, task_queue='my-app')\n"
        )
        assert len(_rule(src, "P017")) == 1

    def test_fires_on_aliased_create_worker(self) -> None:
        """Alias doesn't hide the violation."""
        src = (
            "from application_sdk.execution import create_worker as _cw\n"
            "_cw(client=client, task_queue='my-app')\n"
        )
        assert len(_rule(src, "P017")) == 1

    def test_silent_on_create_data_converter(self) -> None:
        """create_data_converter is not a boot call — should not fire."""
        src = (
            "from application_sdk.execution import create_data_converter\n"
            "converter = create_data_converter()\n"
        )
        assert _rule(src, "P017") == []

    def test_silent_on_unrelated_create_worker(self) -> None:
        """A create_worker imported from somewhere else is not flagged."""
        src = "from mylib.workers import create_worker\ncreate_worker()\n"
        assert _rule(src, "P017") == []

    def test_silent_on_run_dev_combined(self) -> None:
        """run_dev_combined is the sanctioned dev launcher — must not fire."""
        src = (
            "import asyncio\n"
            "from application_sdk.main import run_dev_combined\n"
            "from app.connector import MyApp\n"
            "asyncio.run(run_dev_combined(MyApp, credentials={}))\n"
        )
        assert _rule(src, "P017") == []


class TestP017LegacyV2Imports:
    """Violation class (a): imports from removed v2 boot modules."""

    def test_fires_on_application_sdk_worker_import(self) -> None:
        src = "from application_sdk.worker import Worker\n"
        fs = _rule(src, "P017")
        assert len(fs) == 1
        assert "v2 boot surface" in fs[0].message

    def test_fires_on_base_application_import(self) -> None:
        src = "from application_sdk.application import BaseApplication\n"
        assert len(_rule(src, "P017")) == 1

    def test_fires_on_deep_application_submodule_import(self) -> None:
        src = (
            "from application_sdk.application.metadata_extraction.sql import "
            "BaseSQLMetadataExtractionApplication\n"
        )
        assert len(_rule(src, "P017")) == 1

    def test_fires_on_temporal_workflow_client_import(self) -> None:
        src = "from application_sdk.clients.temporal import TemporalWorkflowClient\n"
        assert len(_rule(src, "P017")) == 1

    def test_fires_on_dotted_worker_import(self) -> None:
        src = "import application_sdk.worker\n"
        assert len(_rule(src, "P017")) == 1

    def test_fires_on_dotted_application_import(self) -> None:
        src = "import application_sdk.application\n"
        assert len(_rule(src, "P017")) == 1

    def test_silent_on_application_sdk_app(self) -> None:
        """application_sdk.app is the conformant seam — must not fire."""
        src = "from application_sdk.app import App, entrypoint, task\n"
        assert _rule(src, "P017") == []

    def test_silent_on_application_sdk_execution(self) -> None:
        """application_sdk.execution is the sanctioned public seam — import is fine."""
        src = "from application_sdk.execution import create_worker, AppWorker\n"
        assert _rule(src, "P017") == []

    def test_silent_on_application_sdk_main(self) -> None:
        """application_sdk.main is the launcher — importing it is conformant."""
        src = "from application_sdk.main import run_dev_combined\n"
        assert _rule(src, "P017") == []


class TestP017LifecycleMethods:
    """Violation class (c): v2 worker-lifecycle method calls."""

    def test_fires_on_setup_workflow(self) -> None:
        src = "app.setup_workflow(workflow_and_activities_classes=[(W, A())])\n"
        fs = _rule(src, "P017")
        assert len(fs) == 1
        assert "setup_workflow" in fs[0].message

    def test_fires_on_start_workflow(self) -> None:
        src = "await self.start_workflow(input)\n"
        assert len(_rule(src, "P017")) == 1

    def test_fires_on_start_worker(self) -> None:
        src = "await app.start_worker()\n"
        assert len(_rule(src, "P017")) == 1


class TestP017Suppression:
    def test_suppressed_inline(self) -> None:
        src = (
            "from application_sdk.execution import create_worker\n"
            "worker = create_worker(client=c, task_queue='q')  "
            "# conformance: ignore[P017] integration test scaffolding\n"
        )
        fs = _rule(src, "P017")
        assert len(fs) == 1 and fs[0].suppressed
        assert fs[0].suppression_justification == "integration test scaffolding"

    def test_suppressed_comment_line_above(self) -> None:
        # The directive must appear on the comment-only line directly *above*
        # the offending statement for make_finding to honour it.
        src = (
            "# conformance: ignore[P017] legacy boot during phased migration\n"
            "from application_sdk.worker import Worker\n"
            "w = Worker(client=client)\n"
        )
        fs = [f for f in scan_text(src, "app/x.py") if f.rule_id == "P017"]
        assert any(f.suppressed for f in fs)


class TestP017TierAndDisposition:
    def test_is_warn_tier(self) -> None:
        assert get_rule("P017").tier is EnforcementTier.WARN

    def test_warn_violation_passes_gate(self, tmp_path: Path) -> None:
        """An unsuppressed P017 (WARN) does not fail the gate (exit 0)."""
        (tmp_path / "app.py").write_text(
            "from application_sdk.execution import create_worker\n"
            "w = create_worker(client=c, task_queue='q')\n"
        )
        code = main(["--root", str(tmp_path), str(tmp_path / "app.py")])
        assert code == 0

    def test_result_is_warning_disposition(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text(
            "from application_sdk.execution import create_worker\n"
            "w = create_worker(client=c, task_queue='q')\n"
        )
        sarif_file = tmp_path / "out.sarif"
        main(
            [
                "--root",
                str(tmp_path),
                str(tmp_path / "app.py"),
                "--sarif-output",
                str(sarif_file),
            ]
        )
        report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
        p017_results = [r for r in report.runs[0].results if r.rule_id == "P017"]
        assert len(p017_results) >= 1
        assert all(derive_disposition(r) == Disposition.WARNING for r in p017_results)


# ── P018 ManualServerBootstrap ────────────────────────────────────────────────


class TestP018FastAPIConstruction:
    """Violation class (a): FastAPI(...) constructed directly."""

    def test_fires_on_fastapi_from_fastapi(self) -> None:
        src = "from fastapi import FastAPI\napp = FastAPI()\n"
        fs = _rule(src, "P018")
        assert len(fs) == 1
        assert "FastAPI" in fs[0].message

    def test_fires_on_fastapi_with_kwargs(self) -> None:
        src = (
            "from fastapi import FastAPI\n"
            "app = FastAPI(title='My API', version='1.0')\n"
        )
        assert len(_rule(src, "P018")) == 1

    def test_silent_on_fastapi_imported_elsewhere(self) -> None:
        """FastAPI imported from a non-fastapi package must not fire."""
        src = "from mylib import FastAPI\napp = FastAPI()\n"
        assert _rule(src, "P018") == []

    def test_silent_on_entrypoint_usage(self) -> None:
        """Conformant @entrypoint pattern — no FastAPI, no uvicorn."""
        src = (
            "from application_sdk.app import App, entrypoint\n"
            "from contracts import FetchInput, FetchOutput\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def run(self, input: FetchInput) -> FetchOutput:\n"
            "        return FetchOutput()\n"
        )
        assert _rule(src, "P018") == []


class TestP018UvicornRun:
    """Violation class (b): uvicorn.run() called directly."""

    def test_fires_on_uvicorn_dotted_run(self) -> None:
        src = "import uvicorn\nuvicorn.run(app, host='0.0.0.0', port=8000)\n"
        fs = _rule(src, "P018")
        assert len(fs) == 1
        assert "uvicorn" in fs[0].message

    def test_fires_on_uvicorn_run_from_import(self) -> None:
        src = "from uvicorn import run\nrun(app, host='0.0.0.0', port=8000)\n"
        assert len(_rule(src, "P018")) == 1

    def test_silent_on_other_run_call(self) -> None:
        """A .run() from an unrelated import should not fire."""
        src = "from mylib import run\nrun(something)\n"
        assert _rule(src, "P018") == []

    def test_silent_on_asyncio_run(self) -> None:
        """asyncio.run(...) is the sanctioned dev launcher wrapper — not P018."""
        src = "import asyncio\nasyncio.run(main())\n"
        assert _rule(src, "P018") == []


class TestP018ServerLifecycleMethods:
    """Violation class (c): v2 server-lifecycle method calls."""

    def test_fires_on_setup_server(self) -> None:
        src = "await app.setup_server()\n"
        fs = _rule(src, "P018")
        assert len(fs) == 1
        assert "setup_server" in fs[0].message

    def test_fires_on_start_server(self) -> None:
        src = "await self.start_server()\n"
        assert len(_rule(src, "P018")) == 1

    def test_fires_on_include_router(self) -> None:
        src = "app.include_router(router, prefix='/api')\n"
        assert len(_rule(src, "P018")) == 1


class TestP018Suppression:
    def test_suppressed_inline(self) -> None:
        src = (
            "import uvicorn\n"
            "uvicorn.run(app, host='0.0.0.0')  "
            "# conformance: ignore[P018] standalone dev script outside SDK lifecycle\n"
        )
        fs = _rule(src, "P018")
        assert len(fs) == 1 and fs[0].suppressed

    def test_suppressed_fastapi_inline(self) -> None:
        src = (
            "from fastapi import FastAPI\n"
            "# conformance: ignore[P018] admin-only sidecar server, not the main handler\n"
            "app = FastAPI()\n"
        )
        fs = _rule(src, "P018")
        assert len(fs) == 1 and fs[0].suppressed


class TestP018TierAndDisposition:
    def test_is_warn_tier(self) -> None:
        assert get_rule("P018").tier is EnforcementTier.WARN

    def test_warn_violation_passes_gate(self, tmp_path: Path) -> None:
        """An unsuppressed P018 (WARN) does not fail the gate (exit 0)."""
        (tmp_path / "app.py").write_text("import uvicorn\nuvicorn.run(app)\n")
        code = main(["--root", str(tmp_path), str(tmp_path / "app.py")])
        assert code == 0

    def test_result_is_warning_disposition(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n"
        )
        sarif_file = tmp_path / "out.sarif"
        main(
            [
                "--root",
                str(tmp_path),
                str(tmp_path / "app.py"),
                "--sarif-output",
                str(sarif_file),
            ]
        )
        report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
        p018_results = [r for r in report.runs[0].results if r.rule_id == "P018"]
        assert len(p018_results) >= 1
        assert all(derive_disposition(r) == Disposition.WARNING for r in p018_results)


# ── Discovery: test files are included ───────────────────────────────────────


def test_discover_includes_test_files(tmp_path: Path) -> None:
    """Unlike the source-only prescriptions checker, this series scans test files."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "connector.py").write_text(
        "from application_sdk.app import App\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_integration.py").write_text(
        "from application_sdk.execution import create_worker\n"
        "worker = create_worker(client=c, task_queue='q')\n"
    )
    paths = discover(tmp_path)
    names = {p.name for p in paths}
    assert "test_integration.py" in names, (
        "Entrypoint-conformance discovery must include test files"
    )


def test_discover_excludes_venv(tmp_path: Path) -> None:
    """Virtualenv directories are excluded regardless of test-file inclusion."""
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "worker_shim.py").write_text(
        "from application_sdk.worker import Worker\n"
    )
    (tmp_path / "app.py").write_text("from application_sdk.app import App\n")
    paths = discover(tmp_path)
    assert all(".venv" not in str(p) for p in paths)


def test_discover_excludes_dot_dirs(tmp_path: Path) -> None:
    """Dot-prefixed directories (.github, .claude, …) are always excluded."""
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    (tmp_path / ".github" / "scripts" / "deploy.py").write_text(
        "import uvicorn\nuvicorn.run(app)\n"
    )
    (tmp_path / "app.py").write_text("from application_sdk.app import App\n")
    paths = discover(tmp_path)
    assert all(".github" not in str(p) for p in paths)
