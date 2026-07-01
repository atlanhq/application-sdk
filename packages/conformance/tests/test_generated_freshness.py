"""Tests for K003/K004/K005 generated-artifact freshness (BLDX-1414).

Each test builds a minimal temporary app tree in ``tmp_path`` (a ``contract/``
directory plus generated artifacts), runs :func:`scan_all` over the discovered
paths, and asserts on the returned findings by ``rule_id``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from conformance.suite.checks.generated_freshness import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PKL_PROJECT = dedent("""\
    amends "pkl:Project"
    dependencies {
      ["app-contract-toolkit"] {
        uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0.16.0"
      }
    }
""")

_DEPS_JSON = dedent("""\
    {
      "schemaVersion": 1,
      "resolvedDependencies": {
        "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0": {
          "type": "remote",
          "uri": "projectpackage://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0.16.0",
          "checksums": {"sha256": "deadbeef"}
        }
      }
    }
""")

_APP_PKL = dedent("""\
    amends "@app-contract-toolkit/App.pkl"

    name = "demo"
""")

_BANNER = "# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.\n"
_BANNER_VARIANT = (
    "# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.\n"
)


def _clean_files() -> dict[str, str]:
    """A fully conformant generated app tree."""
    return {
        "contract/PklProject": _PKL_PROJECT,
        "contract/PklProject.deps.json": _DEPS_JSON,
        "contract/app.pkl": _APP_PKL,
        "atlan.yaml": _BANNER + "name: demo\n",
        "app/generated/manifest.json": "{}\n",
        "app/generated/_input.py": _BANNER + "x = 1\n",
        "app/generated/__init__.py": "",
    }


def _scan(tmp_path: Path, files: dict[str, str]) -> list:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return scan_all(discover(tmp_path), tmp_path)


def _ids(findings: list) -> list[str]:
    return sorted(f.rule_id for f in findings)


# ---------------------------------------------------------------------------
# Clean tree / no-op cases
# ---------------------------------------------------------------------------


def test_clean_app_no_findings(tmp_path: Path) -> None:
    assert _scan(tmp_path, _clean_files()) == []


def test_no_contract_dir_no_findings(tmp_path: Path) -> None:
    """A repo with no contract/ (SDK-like) produces nothing even with stray files."""
    findings = _scan(
        tmp_path, {"atlan.yaml": "name: x\n", "app/generated/foo.py": "x = 1\n"}
    )
    assert findings == []


# ---------------------------------------------------------------------------
# K003 — contract lock drift
# ---------------------------------------------------------------------------


def test_k003_stale_lock(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject.deps.json"] = _DEPS_JSON.replace("@0.16.0", "@0.15.0")
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K003"]
    assert len(findings) == 1
    assert findings[0].file == "contract/PklProject"
    assert not findings[0].suppressed


def test_k003_missing_lock(tmp_path: Path) -> None:
    files = _clean_files()
    del files["contract/PklProject.deps.json"]
    assert _ids([f for f in _scan(tmp_path, files) if f.rule_id == "K003"]) == ["K003"]


def test_k003_dependency_absent_from_lock(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject.deps.json"] = '{"resolvedDependencies": {}}'
    assert _ids([f for f in _scan(tmp_path, files) if f.rule_id == "K003"]) == ["K003"]


def test_k003_broad_pin_satisfied_no_finding(tmp_path: Path) -> None:
    """A broad pin (@0) is satisfied by any resolved 0.y.z — not drift."""
    files = _clean_files()
    files["contract/PklProject"] = _PKL_PROJECT.replace("@0.16.0", "@0")
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K003"] == []


def test_k003_suppressed_via_pkl_directive(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject.deps.json"] = _DEPS_JSON.replace("@0.16.0", "@0.15.0")
    files["contract/PklProject"] = _PKL_PROJECT.replace(
        "    uri =", "    // conformance: ignore[K003] phased bump\n    uri ="
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K003"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "phased bump"


# ---------------------------------------------------------------------------
# K004 — missing generated artifact
# ---------------------------------------------------------------------------


def test_k004_missing_manifest(tmp_path: Path) -> None:
    files = _clean_files()
    del files["app/generated/manifest.json"]
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    assert len(findings) == 1
    assert findings[0].file == "contract/app.pkl"


def test_k004_all_outputs_missing(tmp_path: Path) -> None:
    files = {
        "contract/PklProject": _PKL_PROJECT,
        "contract/PklProject.deps.json": _DEPS_JSON,
        "contract/app.pkl": _APP_PKL,
    }
    # atlan.yaml, manifest.json, _input.py all absent -> three K004 findings.
    assert _ids([f for f in _scan(tmp_path, files) if f.rule_id == "K004"]) == [
        "K004",
        "K004",
        "K004",
    ]


def test_k004_no_contract_app_pkl_no_finding(tmp_path: Path) -> None:
    """No contract/app.pkl -> K004 does not fire even if outputs are absent."""
    files = {
        "contract/PklProject": _PKL_PROJECT,
        "contract/PklProject.deps.json": _DEPS_JSON,
    }
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K004"] == []


def test_k004_suppressed_via_pkl_directive(tmp_path: Path) -> None:
    files = _clean_files()
    del files["app/generated/manifest.json"]
    files["contract/app.pkl"] = (
        "// conformance: ignore[K004] utility app emits no manifest\n" + _APP_PKL
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K004"]
    assert len(findings) == 1
    assert findings[0].suppressed


# ---------------------------------------------------------------------------
# K005 — stripped provenance banner
# ---------------------------------------------------------------------------


def test_k005_stripped_banner_on_yaml(tmp_path: Path) -> None:
    files = _clean_files()
    files["atlan.yaml"] = "name: demo\n"  # no banner
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K005"]
    assert len(findings) == 1
    assert findings[0].file == "atlan.yaml"


def test_k005_banner_variant_accepted(tmp_path: Path) -> None:
    """The '… via contract-toolkit. DO NOT EDIT.' variant is a valid banner."""
    files = _clean_files()
    files["app/generated/_e2e_base.py"] = _BANNER_VARIANT + "y = 2\n"
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K005"] == []


def test_k005_empty_init_and_json_exempt(tmp_path: Path) -> None:
    """Empty __init__.py and .json outputs never carry a banner and are exempt."""
    files = _clean_files()
    # __init__.py is empty and manifest.json has no banner — neither is flagged.
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K005"] == []


def test_k005_generated_py_missing_banner(tmp_path: Path) -> None:
    files = _clean_files()
    files["app/generated/_input.py"] = "x = 1\n"  # banner stripped
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K005"]
    assert len(findings) == 1
    assert findings[0].file == "app/generated/_input.py"


def test_k005_markers_on_separate_lines_not_a_banner(tmp_path: Path) -> None:
    """A hand-written preamble that mentions "generated" and "do not edit" on
    separate lines is not a valid banner — both markers must appear together
    on the same header line."""
    files = _clean_files()
    files["atlan.yaml"] = (
        "# This file is auto-generated for reference only.\n"
        "# Please do not edit unless explicitly asked to.\n"
        "name: demo\n"
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K005"]
    assert len(findings) == 1
    assert findings[0].file == "atlan.yaml"


def test_k005_suppressed(tmp_path: Path) -> None:
    files = _clean_files()
    files["atlan.yaml"] = "# conformance: ignore[K005] hand-maintained\nname: demo\n"
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K005"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "hand-maintained"


def test_k005_not_fired_without_contract(tmp_path: Path) -> None:
    """No contract/app.pkl -> no banner expectation even on app/generated files."""
    files = {"app/generated/_input.py": "x = 1\n"}
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K005"] == []


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_rule_metadata_app_scoped_warn() -> None:
    for rid in ("K003", "K004", "K005"):
        rule = get_rule(rid)
        assert rule.scope == RuleScope.APP
        assert rule.tier == EnforcementTier.WARN
        assert rule.rationale, f"{rid} must have a non-empty rationale"
