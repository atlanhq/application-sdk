"""Meta-tests for the P-series orchestration-seam checks (P004–P007, BLDX-1417).

These checks fan out across the fleet, so each rule is tested to fire *exactly*
when it should and stay silent otherwise — both false positives and false
negatives are guarded.  A dedicated test also proves the series' defining trait:
its discovery walk includes test/harness files, so an integration conftest that
wires Temporal directly is caught.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.orchestration import SERIES, discover, scan_all, scan_text


def _rule(src: str, rule_id: str, file: str = "app/x.py") -> list:
    """Findings of a single rule from a per-file scan of *src* at path *file*."""
    return [f for f in scan_text(src, file) if f.rule_id == rule_id]


def _scan_files(tmp_path: Path, files: dict[str, str]) -> list:
    paths: list[Path] = []
    for name, src in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
        paths.append(path)
    return scan_all(paths, tmp_path)


def test_series_letter() -> None:
    assert SERIES == "P"


# ── P004 DirectTemporalImport (app) ─────────────────────────────────────────


def test_p004_fires_on_from_temporalio_import() -> None:
    fs = _rule("from temporalio import workflow, activity\n", "P004")
    assert len(fs) == 1 and fs[0].line == 1


def test_p004_fires_on_import_temporalio_submodule() -> None:
    assert len(_rule("import temporalio.client\n", "P004")) == 1


def test_p004_fires_on_client_import() -> None:
    assert len(_rule("from temporalio.client import Client\n", "P004")) == 1


def test_p004_fires_on_lazy_in_function_import() -> None:
    src = "def f():\n    import temporalio\n    return temporalio\n"
    assert len(_rule(src, "P004")) == 1


def test_p004_silent_on_sdk_seam() -> None:
    src = "from application_sdk.app import task, signal, now, sleep\n"
    assert _rule(src, "P004") == []


def test_p004_silent_on_relative_import() -> None:
    assert _rule("from . import temporalio\n", "P004") == []


def test_p004_suppressed_inline() -> None:
    src = "from temporalio.client import Client  # conformance: ignore[P004] embedded test harness\n"
    fs = _rule(src, "P004")
    assert len(fs) == 1 and fs[0].suppressed


# ── P005 PrivateOrchestrationInternalImport (app) ───────────────────────────


def test_p005_fires_on_private_temporal_module() -> None:
    src = "from application_sdk.execution._temporal.worker import create_worker\n"
    assert len(_rule(src, "P005")) == 1


def test_p005_fires_on_dotted_import_of_private_module() -> None:
    assert (
        len(_rule("import application_sdk.execution._temporal.backend\n", "P005")) == 1
    )


def test_p005_fires_on_private_name_from_public_module() -> None:
    assert len(_rule("from application_sdk.execution import _backend\n", "P005")) == 1


def test_p005_silent_on_public_seam() -> None:
    src = "from application_sdk.execution import create_worker, AppWorker\n"
    assert _rule(src, "P005") == []


def test_p005_silent_on_non_sdk_private() -> None:
    assert _rule("from other_pkg._internal import thing\n", "P005") == []


def test_p005_suppressed_inline() -> None:
    src = (
        "from application_sdk.execution._temporal.worker import create_worker  "
        "# conformance: ignore[P005] no public twin yet\n"
    )
    fs = _rule(src, "P005")
    assert len(fs) == 1 and fs[0].suppressed


# ── P006 TemporalImportOutsideAdapter (sdk) ─────────────────────────────────


def test_p006_fires_outside_adapter() -> None:
    src = "from temporalio.common import RetryPolicy\n"
    assert len(_rule(src, "P006", file="application_sdk/execution/retry.py")) == 1


def test_p006_silent_inside_adapter() -> None:
    src = "from temporalio.client import Client\n"
    assert (
        _rule(src, "P006", file="application_sdk/execution/_temporal/backend.py") == []
    )


def test_p006_silent_in_primitive_reexport_init() -> None:
    src = "from temporalio.workflow import now, sleep\n"
    assert _rule(src, "P006", file="application_sdk/app/__init__.py") == []


def test_p006_silent_in_test_file() -> None:
    # The SDK's own tests may drive the adapter directly.
    src = "from temporalio.client import Client\n"
    assert _rule(src, "P006", file="tests/integration/conftest.py") == []


def test_p006_silent_without_temporal_import() -> None:
    src = "from application_sdk.app import App\n"
    assert _rule(src, "P006", file="application_sdk/execution/retry.py") == []


def test_p006_suppressed_inline() -> None:
    src = "import temporalio  # conformance: ignore[P006] adapter relocation pending\n"
    fs = _rule(src, "P006", file="application_sdk/execution/retry.py")
    assert len(fs) == 1 and fs[0].suppressed


def _p007(tmp_path: Path, files: dict[str, str]) -> list:
    return [f for f in _scan_files(tmp_path, files) if f.rule_id == "P007"]


# ── P007 RawTemporalInPublicSurface (sdk) ───────────────────────────────────


def test_p007_fires_on_reexported_temporal_symbol(tmp_path: Path) -> None:
    files = {
        "application_sdk/execution/__init__.py": (
            'from temporalio.client import Client\n__all__ = ["Client"]\n'
        ),
    }
    assert len(_p007(tmp_path, files)) == 1


def test_p007_fires_on_signature_leak_across_files(tmp_path: Path) -> None:
    files = {
        "application_sdk/execution/__init__.py": (
            "from application_sdk.execution._temporal.backend import create_temporal_client\n"
            '__all__ = ["create_temporal_client"]\n'
        ),
        "application_sdk/execution/_temporal/backend.py": (
            "from temporalio.client import Client\n"
            "async def create_temporal_client(host: str) -> Client:\n"
            "    ...\n"
        ),
    }
    fs = _p007(tmp_path, files)
    assert len(fs) == 1
    assert fs[0].file.endswith("backend.py")


def test_p007_silent_on_blessed_primitive_reexport(tmp_path: Path) -> None:
    files = {
        "application_sdk/app/__init__.py": (
            'from temporalio.workflow import now, sleep\n__all__ = ["now", "sleep"]\n'
        ),
    }
    assert _p007(tmp_path, files) == []


def test_p007_silent_when_not_reexported(tmp_path: Path) -> None:
    files = {
        "application_sdk/execution/_temporal/backend.py": (
            "from temporalio.client import Client\n"
            "async def create_temporal_client(host: str) -> Client:\n"
            "    ...\n"
        ),
    }
    assert _p007(tmp_path, files) == []


def test_p007_silent_on_sdk_typed_public_api(tmp_path: Path) -> None:
    files = {
        "application_sdk/execution/__init__.py": (
            'from application_sdk.execution._impl import make_app\n__all__ = ["make_app"]\n'
        ),
        "application_sdk/execution/_impl.py": (
            "from application_sdk.app import App\ndef make_app(name: str) -> App:\n    ...\n"
        ),
    }
    assert _p007(tmp_path, files) == []


def test_p007_silent_in_test_file(tmp_path: Path) -> None:
    # A test file with a public-named temporal-typed function is not a public leak.
    files = {
        "tests/conftest.py": (
            "from temporalio.client import Client\ndef make_client() -> Client:\n    ...\n"
        ),
    }
    assert _p007(tmp_path, files) == []


# ── Discovery includes test files — the motivating conftest case ────────────


def test_discover_includes_test_files(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "connector.py").write_text("x = 1\n")
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    conftest = tmp_path / "tests" / "integration" / "conftest.py"
    conftest.write_text("x = 1\n")
    found = {p.relative_to(tmp_path).as_posix() for p in discover(tmp_path)}
    assert "tests/integration/conftest.py" in found
    assert "app/connector.py" in found


def test_conftest_violations_are_caught(tmp_path: Path) -> None:
    """The motivating case: a harness wiring Temporal directly is flagged."""
    conftest = (
        "from temporalio.client import Client\n"
        "from application_sdk.execution._temporal.worker import create_worker\n"
        "\n"
        "async def make() -> None:\n"
        "    client = await Client.connect('localhost:7233')\n"
        "    create_worker(client, task_queue='q')\n"
    )
    findings = _scan_files(tmp_path, {"tests/integration/conftest.py": conftest})
    ids = sorted(f.rule_id for f in findings)
    assert ids == ["P004", "P005"], ids
