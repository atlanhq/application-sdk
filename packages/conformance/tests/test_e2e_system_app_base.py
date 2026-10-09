"""Tests for T026 E2EHarnessTenantPoolMismatch.

The rule starts with zero fleet findings, so the silent cases matter as much as
the firing ones: ``utility`` with either base, every type the rule does not
grade, and every class that is not an SDK-harness class.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite.checks.e2e_system_app_base import (
    RULE_T026,
    discover,
    main,
    scan_all,
)

_SYSTEM_SUITE = """\
from application_sdk.testing.e2e import SystemAppE2ETest

class TestMyAppE2E(SystemAppE2ETest):
    connector_short_name = "myapp"
"""

_PLAIN_SUITE = """\
from application_sdk.testing.e2e import BaseE2ETest, RunMode

class TestMyAppE2E(BaseE2ETest):
    mode = RunMode.AGENT
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _atlan_yaml(root: Path, app_type: str) -> None:
    _write(
        root / "atlan.yaml",
        "# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.\n"
        "name: myapp\n"
        f"type: {app_type}\n"
        "entrypoints:\n"
        "- name: myapp\n"
        "  type: connector\n",
    )


def _suite(root: Path, text: str, name: str = "test_myapp_e2e.py") -> None:
    _write(root / "tests" / "e2e" / name, text)


def _run(root: Path) -> list[str]:
    """Messages of the unsuppressed T026 findings for the repo at *root*."""
    findings = scan_all(discover(root), root)
    assert all(f.rule_id == RULE_T026 for f in findings)
    return [f.message for f in findings if not f.suppressed]


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


def test_system_with_plain_harness_is_flagged(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _suite(tmp_path, _PLAIN_SUITE)
    [message] = _run(tmp_path)
    assert "type 'system'" in message
    assert "BaseE2ETest" in message
    assert "E2E_SYSTEM_TENANT_MATRIX_JSON" in message
    assert "connector-ci-e2e.md#system-apps" in message


def test_system_with_system_base_passes(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _suite(tmp_path, _SYSTEM_SUITE)
    assert _run(tmp_path) == []


@pytest.mark.parametrize("spelling", ["System", "SYSTEM", '"system"', "system  # x"])
def test_system_type_is_case_and_quote_insensitive(
    tmp_path: Path, spelling: str
) -> None:
    _atlan_yaml(tmp_path, spelling)
    _suite(tmp_path, _PLAIN_SUITE)
    assert len(_run(tmp_path)) == 1


def test_system_finding_names_the_declared_spelling(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "System")
    _suite(tmp_path, _PLAIN_SUITE)
    [message] = _run(tmp_path)
    assert "type 'System'" in message


def test_system_with_sql_harness_is_flagged(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _suite(
        tmp_path,
        "from application_sdk.testing.e2e import SQLAppE2ETest\n"
        "class TestMyAppE2E(SQLAppE2ETest):\n    pass\n",
    )
    [message] = _run(tmp_path)
    assert "SQLAppE2ETest" in message


def test_system_base_through_generated_base_is_recognised(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _write(
        tmp_path / "app" / "generated" / "_e2e_base.py",
        "from application_sdk.testing.e2e import SystemAppE2ETest\n"
        "class MyappGeneratedE2EBase(SystemAppE2ETest):\n"
        '    connector_short_name = "myapp"\n',
    )
    _suite(
        tmp_path,
        "from app.generated._e2e_base import MyappGeneratedE2EBase\n"
        "class TestMyAppE2E(MyappGeneratedE2EBase):\n    pass\n",
    )
    assert _run(tmp_path) == []


def test_stale_generated_base_on_system_app_is_flagged(tmp_path: Path) -> None:
    """A generated base from before the contract was typed ``system``."""
    _atlan_yaml(tmp_path, "system")
    _write(
        tmp_path / "app" / "generated" / "_e2e_base.py",
        "from application_sdk.testing.e2e import BaseE2ETest\n"
        "class MyappGeneratedE2EBase(BaseE2ETest):\n    pass\n",
    )
    _suite(
        tmp_path,
        "from app.generated._e2e_base import MyappGeneratedE2EBase\n"
        "class TestMyAppE2E(MyappGeneratedE2EBase):\n    pass\n",
    )
    [message] = _run(tmp_path)
    assert "MyappGeneratedE2EBase" in message


def test_system_base_through_repo_local_base_is_recognised(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _write(
        tmp_path / "tests" / "e2e" / "helpers.py",
        "from application_sdk.testing.e2e import SystemAppE2ETest as _Sys\n"
        "class SharedBase(_Sys):\n    pass\n",
    )
    _suite(
        tmp_path,
        "from tests.e2e.helpers import SharedBase\n"
        "class TestMyAppE2E(SharedBase):\n    pass\n",
    )
    assert _run(tmp_path) == []


def test_system_base_via_relative_import_is_recognised(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _write(
        tmp_path / "tests" / "e2e" / "_base.py",
        "from application_sdk.testing.e2e.system_app import SystemAppE2ETest\n"
        "class SharedBase(SystemAppE2ETest):\n    pass\n",
    )
    _suite(
        tmp_path,
        "from ._base import SharedBase\nclass TestMyAppE2E(SharedBase):\n    pass\n",
    )
    assert _run(tmp_path) == []


def test_system_base_via_dotted_module_import(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _suite(
        tmp_path,
        "import application_sdk.testing.e2e\n"
        "class TestMyAppE2E(application_sdk.testing.e2e.SystemAppE2ETest):\n"
        "    pass\n",
    )
    assert _run(tmp_path) == []


def test_non_sdk_system_base_does_not_count(tmp_path: Path) -> None:
    """A same-named class from another package is not the SDK's."""
    _atlan_yaml(tmp_path, "system")
    _suite(
        tmp_path,
        "from application_sdk.testing.e2e import BaseE2ETest\n"
        "from somewhere_else import SystemAppE2ETest\n"
        "class TestMyAppE2E(SystemAppE2ETest, BaseE2ETest):\n    pass\n",
    )
    [message] = _run(tmp_path)
    assert "resolves to BaseE2ETest" in message


def test_nested_import_does_not_mask_module_level_sdk_base(tmp_path: Path) -> None:
    """Only module-scope bindings name a module-level class's bases."""
    _atlan_yaml(tmp_path, "system")
    _suite(
        tmp_path,
        "from application_sdk.testing.e2e import BaseE2ETest\n"
        "def _helper():\n"
        "    from somewhere_else import BaseE2ETest\n"
        "    return BaseE2ETest\n"
        "class TestMyAppE2E(BaseE2ETest):\n    pass\n",
    )
    assert len(_run(tmp_path)) == 1


def test_module_level_conditional_import_counts(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "connector")
    _suite(
        tmp_path,
        "try:\n"
        "    from application_sdk.testing.e2e import SystemAppE2ETest\n"
        "except ImportError:\n"
        "    raise\n"
        "class TestMyAppE2E(SystemAppE2ETest):\n    pass\n",
    )
    assert len(_run(tmp_path)) == 1


@pytest.mark.parametrize(
    ("app_type", "base", "expected"),
    [
        ("connector", "SystemAppE2ETest", 1),
        ("system", "SystemAppE2ETest", 0),
        ("system", "BaseE2ETest", 1),
    ],
)
def test_sdk_star_import_is_recognised(
    tmp_path: Path, app_type: str, base: str, expected: int
) -> None:
    _atlan_yaml(tmp_path, app_type)
    _suite(
        tmp_path,
        "from application_sdk.testing.e2e import *\n"
        f"class TestMyAppE2E({base}):\n    pass\n",
    )
    assert len(_run(tmp_path)) == expected


def test_non_sdk_star_import_is_not_the_sdk(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "connector")
    _suite(
        tmp_path,
        "from somewhere_else import *\nclass TestMyAppE2E(SystemAppE2ETest):\n    pass\n",
    )
    assert _run(tmp_path) == []


# ---------------------------------------------------------------------------
# utility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suite", [_PLAIN_SUITE, _SYSTEM_SUITE])
@pytest.mark.parametrize("spelling", ["utility", "Utility"])
def test_utility_accepts_either_base(tmp_path: Path, suite: str, spelling: str) -> None:
    _atlan_yaml(tmp_path, spelling)
    _suite(tmp_path, suite)
    assert _run(tmp_path) == []


# ---------------------------------------------------------------------------
# connector
# ---------------------------------------------------------------------------


def test_connector_with_system_base_is_flagged(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "connector")
    _suite(tmp_path, _SYSTEM_SUITE)
    [message] = _run(tmp_path)
    assert "type 'connector'" in message
    assert "SystemAppE2ETest" in message
    assert "system apps only" in message


def test_connector_with_plain_harness_passes(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "connector")
    _suite(tmp_path, _PLAIN_SUITE)
    assert _run(tmp_path) == []


def test_connector_system_base_through_repo_local_base_is_flagged(
    tmp_path: Path,
) -> None:
    _atlan_yaml(tmp_path, "connector")
    _write(
        tmp_path / "tests" / "shared.py",
        "from application_sdk.testing.e2e import SystemAppE2ETest\n"
        "class SharedBase(SystemAppE2ETest):\n    pass\n",
    )
    _suite(
        tmp_path,
        "from tests.shared import SharedBase\n"
        "class TestMyAppE2E(SharedBase):\n    pass\n",
    )
    assert len(_run(tmp_path)) == 1


# ---------------------------------------------------------------------------
# Not applicable
# ---------------------------------------------------------------------------


def test_no_atlan_yaml_is_not_applicable(tmp_path: Path) -> None:
    _suite(tmp_path, _SYSTEM_SUITE)
    assert _run(tmp_path) == []


def test_atlan_yaml_without_top_level_type_is_not_applicable(tmp_path: Path) -> None:
    """The indented entrypoint ``type:`` is not the app's type."""
    _write(
        tmp_path / "atlan.yaml",
        "name: myapp\nentrypoints:\n- name: myapp\n  type: connector\n",
    )
    _suite(tmp_path, _SYSTEM_SUITE)
    assert _run(tmp_path) == []


@pytest.mark.parametrize("app_type", ["miner", "custom", ""])
def test_other_types_are_not_applicable(tmp_path: Path, app_type: str) -> None:
    _atlan_yaml(tmp_path, app_type)
    _suite(tmp_path, _SYSTEM_SUITE)
    assert _run(tmp_path) == []


def test_no_e2e_suite_is_not_applicable(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _write(tmp_path / "tests" / "unit" / "test_x.py", "def test_x():\n    pass\n")
    assert _run(tmp_path) == []


def test_non_harness_class_is_not_graded(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _suite(tmp_path, "class TestHelpers:\n    def test_x(self):\n        pass\n")
    assert _run(tmp_path) == []


def test_harness_class_outside_e2e_is_not_graded(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _write(tmp_path / "tests" / "integration" / "test_myapp.py", _PLAIN_SUITE)
    assert _run(tmp_path) == []


def test_non_collectable_base_module_is_not_graded(tmp_path: Path) -> None:
    """A shared base matters only through a collected leaf."""
    _atlan_yaml(tmp_path, "system")
    _suite(tmp_path, _PLAIN_SUITE, name="helpers.py")
    assert _run(tmp_path) == []


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def test_inline_suppression(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _suite(
        tmp_path,
        "from application_sdk.testing.e2e import BaseE2ETest\n"
        "# conformance: ignore[T026] onboarding tracked separately\n"
        "class TestMyAppE2E(BaseE2ETest):\n    pass\n",
    )
    findings = scan_all(discover(tmp_path), tmp_path)
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "onboarding tracked separately"


def test_finding_is_anchored_to_the_class_line(tmp_path: Path) -> None:
    _atlan_yaml(tmp_path, "system")
    _suite(tmp_path, _PLAIN_SUITE)
    [finding] = scan_all(discover(tmp_path), tmp_path)
    assert finding.file == "tests/e2e/test_myapp_e2e.py"
    assert finding.line == 3


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_default_scan_finds_e2e_suites(tmp_path: Path) -> None:
    """With no path argument the CLI scans the repo root's tests/ tree."""
    _atlan_yaml(tmp_path, "system")
    _suite(tmp_path, _PLAIN_SUITE)
    sarif_file = tmp_path / "out.sarif"
    main(["--root", str(tmp_path), "--sarif-output", str(sarif_file)])
    results = json.loads(sarif_file.read_text())["runs"][0]["results"]
    assert [r["ruleId"] for r in results] == [RULE_T026]
