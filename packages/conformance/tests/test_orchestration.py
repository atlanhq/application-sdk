"""Meta-tests for the P-series orchestration-seam checks (P004–P007, BLDX-1417).

These checks fan out across the fleet, so each rule is tested to fire *exactly*
when it should and stay silent otherwise — both false positives and false
negatives are guarded.  A dedicated test also proves the series' defining trait:
its discovery walk includes test/harness files, so an integration conftest that
wires Temporal directly is caught.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import re
from pathlib import Path

from conformance.suite.checks._ast_common._safe_swap import (
    PUBLIC_ALIAS_MODULE,
    PUBLIC_ALIASES,
)
from conformance.suite.checks._version import parse_version, version_reached
from conformance.suite.checks.orchestration import SERIES, discover, scan_all, scan_text
from conformance.suite.schema.extensions import SafeSwap

import conformance


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


# ── Safe-swap markers (atlan/safeSwap) ──────────────────────────────────────


def _swap(finding) -> SafeSwap | None:
    return SafeSwap.from_properties(finding.properties)


def _apply(src: str, finding) -> str:
    swap = _swap(finding)
    assert swap is not None
    lines = src.splitlines(keepends=True)
    assert lines[finding.line - 1].count(swap.find) == 1
    lines[finding.line - 1] = lines[finding.line - 1].replace(swap.find, swap.replace)
    return "".join(lines)


def test_p005_marks_a_module_level_public_reexport_import_as_a_safe_swap() -> None:
    src = (
        "from application_sdk.execution._temporal.activity_utils import (\n"
        "    build_output_path,\n"
        "    get_object_store_prefix as prefix,\n"
        ")\n"
    )
    (finding,) = _rule(src, "P005", "tests/unit/test_x.py")
    swap = _swap(finding)
    assert swap is not None and swap.id == "public-reexport"
    assert (swap.requires.package, swap.requires.min) == (
        "atlan-application-sdk",
        "3.0.0",
    )
    fixed = _apply(src, finding)
    assert fixed.startswith("from application_sdk.execution import (\n")
    assert _rule(fixed, "P005") == []


def test_p005_leaves_a_function_local_reexport_import_unmarked() -> None:
    src = (
        "def f():\n"
        "    from application_sdk.execution._temporal.activity_utils import (\n"
        "        get_object_store_prefix,\n"
        "    )\n"
        "    return get_object_store_prefix\n"
    )
    (finding,) = _rule(src, "P005")
    assert _swap(finding) is None


def test_p005_leaves_a_name_without_a_public_home_unmarked() -> None:
    src = (
        "from application_sdk.execution._temporal.activity_utils import (\n"
        "    get_object_store_prefix,\n"
        "    get_workflow_id,\n"
        ")\n"
    )
    (finding,) = _rule(src, "P005")
    assert _swap(finding) is None


def test_p005_leaves_a_dotted_module_import_unmarked() -> None:
    src = "import application_sdk.execution._temporal.activity_utils as au\n"
    (finding,) = _rule(src, "P005")
    assert _swap(finding) is None


def test_p005_leaves_other_private_modules_unmarked() -> None:
    src = "from application_sdk.execution._temporal.worker import create_worker\n"
    (finding,) = _rule(src, "P005", "tests/integration/test_x.py")
    assert _swap(finding) is None


def test_p004_marks_a_failure_class_import_keeping_the_bound_name() -> None:
    src = "from temporalio.exceptions import ApplicationError\n"
    (finding,) = _rule(src, "P004")
    swap = _swap(finding)
    assert swap is not None and swap.id == "public-alias"
    assert swap.requires.min == "3.50.0"
    fixed = _apply(src, finding)
    assert fixed == (
        "from application_sdk.execution import TemporalApplicationError as ApplicationError\n"
    )
    assert _rule(fixed, "P004") == []


def test_p004_marks_an_aliased_failure_class_import_keeping_its_alias() -> None:
    src = "from temporalio.exceptions import TimeoutError as TT  # noqa: A004\n"
    (finding,) = _rule(src, "P004", "tests/unit/test_x.py")
    swap = _swap(finding)
    assert swap is not None and swap.requires.min == "3.21.0"
    assert _apply(src, finding).startswith(
        "from application_sdk.execution import TemporalTimeoutError as TT"
    )


def test_p004_marks_the_client_failure_class() -> None:
    (finding,) = _rule("from temporalio.client import WorkflowFailureError\n", "P004")
    assert _apply("from temporalio.client import WorkflowFailureError\n", finding) == (
        "from application_sdk.execution import TemporalWorkflowFailureError as WorkflowFailureError\n"
    )


def test_p004_leaves_other_temporal_imports_unmarked() -> None:
    for src in (
        "from temporalio.client import Client\n",
        "from temporalio.exceptions import TimeoutType\n",
        "from temporalio.exceptions import ApplicationError, TimeoutError\n",
        "from temporalio.exceptions import (\n    ApplicationError,\n)\n",
        "from temporalio.exceptions import (ApplicationError)\n",
        "from temporalio.exceptions import  ApplicationError\n",
        "from  temporalio.exceptions import ApplicationError\n",
        "import temporalio.exceptions\n",
        "def f():\n    from temporalio.exceptions import ApplicationError\n    return ApplicationError\n",
    ):
        (finding,) = _rule(src, "P004")
        assert _swap(finding) is None, src


def test_floor_compare_treats_a_pre_release_as_its_release() -> None:
    assert version_reached((3, 50, 0), parse_version("3.50.0.dev0") or ())
    assert not version_reached((3, 50, 0), parse_version("3.49.2") or ())


def test_p004_public_aliases_are_the_same_objects_from_their_floor() -> None:
    installed = parse_version(importlib.metadata.version("atlan-application-sdk"))
    assert installed is not None
    seam = importlib.import_module(PUBLIC_ALIAS_MODULE)
    for (module, name), (public, since) in PUBLIC_ALIASES.items():
        floor = parse_version(since)
        assert floor is not None
        if not version_reached(floor, installed):
            assert not hasattr(seam, public), (public, since, installed)
            continue
        assert getattr(seam, public) is getattr(importlib.import_module(module), name)


def test_p004_hints_never_suggest_a_call_that_fires_p017() -> None:
    for src in (
        "from temporalio.client import Client\n",
        "from temporalio.worker import Worker\n",
        "from temporalio.testing import WorkflowEnvironment\n",
    ):
        (finding,) = _rule(src, "P004", "tests/unit/test_x.py")
        assert "import create_" not in finding.message
        assert "fixture kit" in finding.message


def test_p004_hint_names_the_failure_alias() -> None:
    (finding,) = _rule("from temporalio.exceptions import ApplicationError\n", "P004")
    assert "TemporalApplicationError" in finding.message


def test_p004_hint_names_no_alias_for_a_name_without_one() -> None:
    (finding,) = _rule("from temporalio.exceptions import TimeoutType\n", "P004")
    assert "alias (" not in finding.message
    assert "no public SDK equivalent" in finding.message


def test_p004_hint_keeps_every_kind_of_name_in_a_mixed_import() -> None:
    src = "from temporalio.client import Client, WorkflowFailureError\n"
    (finding,) = _rule(src, "P004")
    assert "TemporalWorkflowFailureError" in finding.message
    assert "TemporalClient" in finding.message


def test_p004_client_and_worker_hints_name_the_production_launcher() -> None:
    for src in (
        "from temporalio.client import Client\n",
        "from temporalio.worker import Worker\n",
    ):
        (finding,) = _rule(src, "P004")
        assert "application-sdk --mode worker|handler|combined" in finding.message


def test_p004_hint_sends_every_unaliased_exception_or_client_name_to_the_list() -> None:
    for src in (
        "from temporalio.exceptions import RetryState\n",
        "from temporalio.exceptions import WorkflowAlreadyStartedError\n",
        "from temporalio.client import WorkflowHandle\n",
        "from temporalio.client import (\n    Client,\n    ScheduleSpec,\n)\n",
    ):
        (finding,) = _rule(src, "P004")
        assert "no public SDK equivalent" in finding.message, src


def test_p004_status_enum_gets_the_literal_hint_not_the_no_public_one() -> None:
    (finding,) = _rule(
        "from temporalio.client import WorkflowExecutionStatus\n", "P004"
    )
    assert '"COMPLETED"' in finding.message
    assert "no public SDK equivalent" not in finding.message


def test_p004_sandbox_settings_get_the_passthrough_hint() -> None:
    for src in (
        "from temporalio.worker.workflow_sandbox import SandboxRestrictions\n",
        "from temporalio.worker import SandboxedWorkflowRunner\n",
    ):
        (finding,) = _rule(src, "P004")
        assert "passthrough_modules" in finding.message
        assert "application-sdk --mode" not in finding.message


def test_p004_unsandboxed_runner_has_no_public_equivalent() -> None:
    (finding,) = _rule(
        "from temporalio.worker import UnsandboxedWorkflowRunner\n", "P004"
    )
    assert "passthrough_modules" not in finding.message
    assert "no public SDK equivalent" in finding.message


def test_p004_other_worker_names_keep_the_launcher_hint() -> None:
    for src in (
        "from temporalio.worker import Interceptor\n",
        "from temporalio.worker import WorkerConfig\n",
        "from temporalio.worker import Worker, UnsandboxedWorkflowRunner\n",
        "import temporalio.worker\n",
    ):
        (finding,) = _rule(src, "P004")
        assert "application-sdk --mode worker|handler|combined" in finding.message, src


def test_p004_bare_module_import_points_at_per_name_treatment() -> None:
    for src in ("import temporalio.exceptions\n", "import temporalio.common\n"):
        (finding,) = _rule(src, "P004")
        assert "Each name used through this module" in finding.message


def _p004_preplanning_blocks() -> list[re.Pattern[str]]:
    skill = (
        Path(conformance.__file__).parent
        / "skills"
        / "migrate-orchestration"
        / "SKILL.md"
    )
    block = re.search(r"```decided-defaults\n(.*?)```", skill.read_text(), re.S)
    assert block is not None
    rows = [json.loads(line) for line in block.group(1).splitlines() if line.strip()]
    return [
        re.compile(r["match"])
        for r in rows
        if r["rule"] == "P004" and r.get("block") and isinstance(r.get("match"), str)
    ]


def test_skill_preplanning_block_holds_every_no_public_hint() -> None:
    blocks = _p004_preplanning_blocks()
    for src in (
        "from temporalio.exceptions import RetryState\n",
        "from temporalio.client import WorkflowHandle\n",
        "from temporalio.common import (\n    SearchAttributeKey,\n)\n",
        "from temporalio.worker import UnsandboxedWorkflowRunner\n",
    ):
        (finding,) = _rule(src, "P004")
        assert any(b.search(finding.message) for b in blocks), src


def test_skill_preplanning_block_leaves_fixable_imports_alone() -> None:
    blocks = _p004_preplanning_blocks()
    for src in (
        "from temporalio.exceptions import ApplicationError\n",
        "from temporalio.client import WorkflowExecutionStatus\n",
        "from temporalio.client import Client\n",
        "from temporalio.common import RetryPolicy\n",
    ):
        (finding,) = _rule(src, "P004")
        assert not any(b.search(finding.message) for b in blocks), src
        assert not any(b.search(src.splitlines()[0]) for b in blocks), src


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


# ── P007 robustness: origin matching + __all__ += ───────────────────────────


def test_p007_no_collision_on_unrelated_name(tmp_path: Path) -> None:
    # 'build' is re-exported from a non-temporal module; an unrelated module
    # defines a different 'build' with a temporal signature. Only the re-export's
    # actual origin is inspected, so the unrelated function is not flagged.
    files = {
        "application_sdk/execution/__init__.py": (
            'from application_sdk.execution._real import build\n__all__ = ["build"]\n'
        ),
        "application_sdk/execution/_real.py": (
            "from application_sdk.app import App\ndef build() -> App:\n    ...\n"
        ),
        "application_sdk/unrelated.py": (
            "from temporalio.client import Client\ndef build(c: Client) -> None:\n    ...\n"
        ),
    }
    assert _p007(tmp_path, files) == []


def test_p007_picks_up_augmented_all(tmp_path: Path) -> None:
    # __all__ += [...] must be honoured, else a re-export added this way is missed.
    files = {
        "application_sdk/execution/__init__.py": (
            "from application_sdk.execution._temporal.backend import create_x\n"
            "__all__ = []\n"
            '__all__ += ["create_x"]\n'
        ),
        "application_sdk/execution/_temporal/backend.py": (
            "from temporalio.client import Client\n"
            "async def create_x() -> Client:\n"
            "    ...\n"
        ),
    }
    fs = _p007(tmp_path, files)
    assert len(fs) == 1 and fs[0].file.endswith("backend.py")
