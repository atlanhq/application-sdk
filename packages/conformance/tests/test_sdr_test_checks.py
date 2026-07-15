"""Meta-tests for the T-series SDR test-quality checks (T002–T003, DISTR-752).

T002 catches SDR apps that drive the SDR (agent-mode) path from no test at all,
so manifest defects and upload-path regressions are invisible to CI.  Coverage
is satisfied by either harness: an agent-mode e2e test (a BaseE2ETest subclass
with ``mode = RunMode.AGENT``) or a legacy BaseSDRIntegrationTest subclass.  A
direct-mode e2e test (``mode = RunMode.DIRECT``) is not SDR coverage.

T003 catches BaseSDRIntegrationTest subclasses that use the legacy
agent_spec_template instead of manifest_path.  The hand-crafted template
bypasses manifest validation: the test can pass even when manifest.json is
missing agent_json, which was exactly the MSSQL regression pattern (DISTR-752).

T002 gates on self_deployed_runtime: true in atlan.yaml.  T003 fires on any
BaseSDRIntegrationTest subclass regardless of the atlan.yaml flag.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.sdr_test_checks import discover, scan_all, scan_path
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ── helpers ─────────────────────────────────────────────────────────────────


_SDR_ATLAN_YAML = "self_deployed_runtime: true\nname: my-connector\n"
_NON_SDR_ATLAN_YAML = "self_deployed_runtime: false\nname: my-connector\n"


def _write(tmp_path: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _run(tmp_path: Path) -> list:
    paths = discover(tmp_path)
    return scan_all(paths, tmp_path)


def _ids(findings: list) -> list[str]:
    return [f.rule_id for f in findings]


# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t002_rule_metadata() -> None:
    rule = get_rule("T002")
    assert rule.name == "MissingSdrTestClass"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.9.0"
    assert rule.category == "sdr-test-coverage"


def test_t003_rule_metadata() -> None:
    rule = get_rule("T003")
    assert rule.name == "SdrTestLegacyAgentSpec"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.autofixable is False
    assert rule.rationale.strip()
    assert rule.since == "0.9.0"
    assert rule.category == "sdr-test-coverage"


# ── T002: missing SDR test class ─────────────────────────────────────────────


def test_t002_fires_when_sdr_app_has_no_sdr_test(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "tests/test_connector.py": "import pytest\n\n\ndef test_smoke():\n    pass\n",
        },
    )
    findings = _run(tmp_path)
    t002 = [f for f in findings if f.rule_id == "T002"]
    assert len(t002) == 1
    # Message guides toward both accepted harnesses.
    assert "RunMode.AGENT" in t002[0].message
    assert "BaseSDRIntegrationTest" in t002[0].message
    assert t002[0].file == "atlan.yaml"
    assert t002[0].line == 1


def test_t002_silent_when_sdr_subclass_present(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "tests/test_sdr.py": (
                "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
                "class TestMySDR(BaseSDRIntegrationTest):\n"
                "    manifest_path = 'app/generated/manifest.json'\n"
            ),
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "T002" for f in findings)


def test_t002_silent_on_non_sdr_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _NON_SDR_ATLAN_YAML,
            "tests/test_connector.py": "def test_smoke():\n    pass\n",
        },
    )
    assert not _run(tmp_path)


def test_t002_silent_when_no_atlan_yaml(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "tests/test_connector.py": "def test_smoke():\n    pass\n",
        },
    )
    assert not _run(tmp_path)


def test_t002_fires_when_no_tests_dir(tmp_path: Path) -> None:
    _write(tmp_path, {"atlan.yaml": _SDR_ATLAN_YAML})
    findings = _run(tmp_path)
    assert any(f.rule_id == "T002" for f in findings)


def test_t002_one_finding_only(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "tests/test_a.py": "def test_a():\n    pass\n",
            "tests/test_b.py": "def test_b():\n    pass\n",
        },
    )
    findings = [f for f in _run(tmp_path) if f.rule_id == "T002"]
    assert len(findings) == 1


def test_t002_silent_when_subclass_in_nested_dir(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "tests/integration/test_sdr.py": (
                "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
                "class TestMySDR(BaseSDRIntegrationTest):\n"
                "    manifest_path = 'app/generated/manifest.json'\n"
            ),
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "T002" for f in findings)


# ── T002: agent-mode e2e harness satisfies coverage (BLDX) ───────────────────

# Mirrors the migrated fleet (openapi/mysql/metabase): a BaseE2ETest subclass
# via a generated *GeneratedE2EBase that sets mode = RunMode.AGENT.
_AGENT_E2E_SRC = (
    "from application_sdk.testing.e2e import RunMode\n"
    "from app.generated._e2e_base import MyAppGeneratedE2EBase\n\n\n"
    "class TestMyAppE2E(MyAppGeneratedE2EBase):\n"
    "    mode = RunMode.AGENT\n"
)
_DIRECT_E2E_SRC = (
    "from application_sdk.testing.e2e import RunMode\n"
    "from app.generated._e2e_base import MyAppGeneratedE2EBase\n\n\n"
    "class TestMyAppE2E(MyAppGeneratedE2EBase):\n"
    "    mode = RunMode.DIRECT\n"
)


def test_t002_silent_when_agent_mode_e2e_present(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "tests/e2e/test_e2e.py": _AGENT_E2E_SRC,
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "T002" for f in findings)


def test_t002_silent_when_agent_mode_annotated_assign(tmp_path: Path) -> None:
    # `mode: RunMode = RunMode.AGENT` (AnnAssign form) also counts.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "tests/e2e/test_e2e.py": (
                "from application_sdk.testing.e2e import RunMode\n"
                "from app.generated._e2e_base import MyAppGeneratedE2EBase\n\n\n"
                "class TestMyAppE2E(MyAppGeneratedE2EBase):\n"
                "    mode: RunMode = RunMode.AGENT\n"
            ),
        },
    )
    findings = _run(tmp_path)
    assert not any(f.rule_id == "T002" for f in findings)


def test_t002_fires_when_only_direct_mode_e2e(tmp_path: Path) -> None:
    # A direct-mode e2e test does NOT exercise the SDR path — T002 must still fire.
    _write(
        tmp_path,
        {
            "atlan.yaml": _SDR_ATLAN_YAML,
            "tests/e2e/test_e2e.py": _DIRECT_E2E_SRC,
        },
    )
    findings = _run(tmp_path)
    assert any(f.rule_id == "T002" for f in findings)


# ── T003: legacy agent_spec_template ────────────────────────────────────────


_T003_LEGACY_SRC = (
    "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
    "class TestMySDR(BaseSDRIntegrationTest):\n"
    '    agent_spec_template = \'{"agent": "spec"}\'\n'
)

_T003_MANIFEST_PATH_SRC = (
    "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
    "class TestMySDR(BaseSDRIntegrationTest):\n"
    "    manifest_path = 'app/generated/manifest.json'\n"
)

_T003_BOTH_SRC = (
    "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
    "class TestMySDR(BaseSDRIntegrationTest):\n"
    '    agent_spec_template = \'{"agent": "spec"}\'\n'
    "    manifest_path = 'app/generated/manifest.json'\n"
)

_T003_NEITHER_SRC = (
    "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
    "class TestMySDR(BaseSDRIntegrationTest):\n"
    "    workflow_type = 'extraction'\n"
)


def test_t003_fires_on_legacy_agent_spec_only(tmp_path: Path) -> None:
    _write(tmp_path, {"tests/test_sdr.py": _T003_LEGACY_SRC})
    findings = _run(tmp_path)
    t003 = [f for f in findings if f.rule_id == "T003"]
    assert len(t003) == 1
    assert "agent_spec_template" in t003[0].message
    assert "manifest_path" in t003[0].message
    assert t003[0].line >= 4  # class def line


def test_t003_silent_when_manifest_path_set(tmp_path: Path) -> None:
    _write(tmp_path, {"tests/test_sdr.py": _T003_MANIFEST_PATH_SRC})
    findings = _run(tmp_path)
    assert not any(f.rule_id == "T003" for f in findings)


def test_t003_silent_when_both_vars_set(tmp_path: Path) -> None:
    # If manifest_path is also set, the test uses the manifest — not legacy.
    _write(tmp_path, {"tests/test_sdr.py": _T003_BOTH_SRC})
    findings = _run(tmp_path)
    assert not any(f.rule_id == "T003" for f in findings)


def test_t003_silent_when_neither_var_set(tmp_path: Path) -> None:
    # A new subclass that sets neither is not using the legacy pattern.
    _write(tmp_path, {"tests/test_sdr.py": _T003_NEITHER_SRC})
    findings = _run(tmp_path)
    assert not any(f.rule_id == "T003" for f in findings)


def test_t003_fires_regardless_of_atlan_yaml(tmp_path: Path) -> None:
    # T003 does not gate on self_deployed_runtime — legacy spec is wrong either way.
    _write(
        tmp_path,
        {
            # no atlan.yaml
            "tests/test_sdr.py": _T003_LEGACY_SRC,
        },
    )
    findings = _run(tmp_path)
    assert any(f.rule_id == "T003" for f in findings)


def test_t003_fires_on_non_sdr_atlan_yaml(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "atlan.yaml": _NON_SDR_ATLAN_YAML,
            "tests/test_sdr.py": _T003_LEGACY_SRC,
        },
    )
    findings = _run(tmp_path)
    assert any(f.rule_id == "T003" for f in findings)


def test_t003_finding_anchored_to_class_def_line(tmp_path: Path) -> None:
    src = (
        "import pytest\n"
        "\n"
        "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n"
        "\n"
        "\n"
        "class TestMySDR(BaseSDRIntegrationTest):\n"
        "    agent_spec_template = '{\"a\": 1}'\n"
    )
    _write(tmp_path, {"tests/test_sdr.py": src})
    findings = [f for f in _run(tmp_path) if f.rule_id == "T003"]
    assert len(findings) == 1
    class_def_line = (
        src.splitlines().index("class TestMySDR(BaseSDRIntegrationTest):") + 1
    )
    assert findings[0].line == class_def_line


def test_t003_fires_per_legacy_class(tmp_path: Path) -> None:
    src = (
        "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
        "class TestAlpha(BaseSDRIntegrationTest):\n"
        "    agent_spec_template = '{\"a\": 1}'\n\n\n"
        "class TestBeta(BaseSDRIntegrationTest):\n"
        "    agent_spec_template = '{\"b\": 2}'\n"
    )
    _write(tmp_path, {"tests/test_sdr.py": src})
    findings = [f for f in _run(tmp_path) if f.rule_id == "T003"]
    assert len(findings) == 2


def test_t003_inline_suppression(tmp_path: Path) -> None:
    src = (
        "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
        "# conformance: ignore[T003] negative-path test uses hand-crafted spec intentionally\n"
        "class TestMySDR(BaseSDRIntegrationTest):\n"
        "    agent_spec_template = '{\"a\": 1}'\n"
    )
    _write(tmp_path, {"tests/test_sdr.py": src})
    findings = [f for f in _run(tmp_path) if f.rule_id == "T003"]
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_t003_fires_on_dict_literal_agent_spec(tmp_path: Path) -> None:
    # Dict-literal is the canonical legacy form (base class is ClassVar[dict[str, Any]])
    src = (
        "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
        "class TestMySDR(BaseSDRIntegrationTest):\n"
        '    agent_spec_template = {"connection_qualified_name": "default/mssql/1"}\n'
    )
    _write(tmp_path, {"tests/test_sdr.py": src})
    findings = [f for f in _run(tmp_path) if f.rule_id == "T003"]
    assert len(findings) == 1


def test_t003_fires_on_annotated_dict_agent_spec(tmp_path: Path) -> None:
    # Annotated assignment mirrors the base-class ClassVar annotation
    src = (
        "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n"
        "from typing import Any\n\n\n"
        "class TestMySDR(BaseSDRIntegrationTest):\n"
        '    agent_spec_template: dict[str, Any] = {"connection_qualified_name": "default/mssql/1"}\n'
    )
    _write(tmp_path, {"tests/test_sdr.py": src})
    findings = [f for f in _run(tmp_path) if f.rule_id == "T003"]
    assert len(findings) == 1


def test_t003_silent_on_empty_agent_spec_string(tmp_path: Path) -> None:
    # An empty string assignment is treated as "not set" — no legacy signal.
    src = (
        "from application_sdk.testing.sdr.base import BaseSDRIntegrationTest\n\n\n"
        "class TestMySDR(BaseSDRIntegrationTest):\n"
        "    agent_spec_template = ''\n"
    )
    _write(tmp_path, {"tests/test_sdr.py": src})
    findings = _run(tmp_path)
    assert not any(f.rule_id == "T003" for f in findings)


def test_t003_silent_on_syntax_error(tmp_path: Path) -> None:
    _write(tmp_path, {"tests/test_sdr.py": "def bad(:\n"})
    # No crash, no findings
    assert not _run(tmp_path)


# ── discover() targets tests/ broadly ────────────────────────────────────────


def test_discover_finds_test_files(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "tests/test_one.py": "pass\n",
            "tests/integration/test_two.py": "pass\n",
            "tests/helpers/utils.py": "pass\n",
            "tests/__pycache__/cached.py": "pass\n",
        },
    )
    found = {p.name for p in discover(tmp_path)}
    assert "test_one.py" in found
    assert "test_two.py" in found
    assert "utils.py" in found
    assert "cached.py" not in found


def test_discover_empty_when_tests_absent(tmp_path: Path) -> None:
    assert discover(tmp_path) == []


# ── scan_path no-op ──────────────────────────────────────────────────────────


def test_scan_path_is_noop(tmp_path: Path) -> None:
    f = tmp_path / "tests" / "test_x.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("pass\n")
    assert scan_path(f, tmp_path) == []
