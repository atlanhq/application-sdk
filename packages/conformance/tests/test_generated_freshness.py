"""Tests for K003/K004/K005 generated-artifact freshness (BLDX-1414).

Each test builds a minimal temporary app tree in ``tmp_path`` (a ``contract/``
directory plus generated artifacts), runs :func:`scan_all` over the discovered
paths, and asserts on the returned findings by ``rule_id``.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from conformance.suite.checks._toolkit_baseline import load_baseline
from conformance.suite.checks.generated_freshness import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# The toolkit version floor (K007) and source check (K008) compare against the
# committed baseline, so the clean fixtures track it rather than a literal — that
# keeps this suite green across toolkit bumps (a bump regenerates the baseline).
_BASELINE = load_baseline()
assert _BASELINE is not None, "data/toolkit_baseline.json must be committed"
_LATEST = _BASELINE.latest_version
_CANONICAL = _BASELINE.canonical_base
# Comfortably older than any real toolkit release, for outdated/drift cases.
_OLDER = "0.0.1"


def _pkl_project(version: str = _LATEST, base: str = _CANONICAL) -> str:
    return (
        'amends "pkl:Project"\n'
        "dependencies {\n"
        '  ["app-contract-toolkit"] {\n'
        f'    uri = "package://{base}@{version}"\n'
        "  }\n"
        "}\n"
    )


def _deps_json(
    resolved: str = _LATEST, base: str = _CANONICAL, present: bool = True
) -> str:
    deps: dict[str, object] = {}
    if present:
        deps[f"package://{base}@0"] = {
            "type": "remote",
            "uri": f"projectpackage://{base}@{resolved}",
            "checksums": {"sha256": "deadbeef"},
        }
    return (
        json.dumps({"schemaVersion": 1, "resolvedDependencies": deps}, indent=2) + "\n"
    )


_PKL_PROJECT = _pkl_project()
_DEPS_JSON = _deps_json()

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
        "app/generated/_e2e_base.py": _BANNER + "class BaseE2E:\n    pass\n",
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
    # Pin at latest, lock resolves something else -> pin-vs-lock drift.
    files["contract/PklProject.deps.json"] = _deps_json(resolved=_OLDER)
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
    files["contract/PklProject.deps.json"] = _deps_json(present=False)
    assert _ids([f for f in _scan(tmp_path, files) if f.rule_id == "K003"]) == ["K003"]


def test_k003_broad_pin_satisfied_no_finding(tmp_path: Path) -> None:
    """A broad pin (@0) is satisfied by any resolved 0.y.z — not drift."""
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(version="0")
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K003"] == []


def test_k003_suppressed_via_pkl_directive(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject.deps.json"] = _deps_json(resolved=_OLDER)
    files["contract/PklProject"] = _pkl_project().replace(
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
# K007 — toolkit version outdated
# ---------------------------------------------------------------------------


def test_k007_outdated_toolkit(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(version=_OLDER)
    files["contract/PklProject.deps.json"] = _deps_json(resolved=_OLDER)
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K007"]
    assert len(findings) == 1
    assert findings[0].file == "contract/PklProject"
    assert not findings[0].suppressed


def test_k007_latest_no_finding(tmp_path: Path) -> None:
    assert [f for f in _scan(tmp_path, _clean_files()) if f.rule_id == "K007"] == []


def test_k007_ahead_no_finding(tmp_path: Path) -> None:
    """An app on a newer toolkit than the recorded baseline is not flagged."""
    files = _clean_files()
    files["contract/PklProject.deps.json"] = _deps_json(resolved="999.0.0")
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K007"] == []


def test_k007_no_lock_defers_to_k003(tmp_path: Path) -> None:
    """Without a resolved lock K007 stays quiet — the missing lock is K003's job."""
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(version=_OLDER)
    del files["contract/PklProject.deps.json"]
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K007"] == []


def test_k007_suppressed(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject.deps.json"] = _deps_json(resolved=_OLDER)
    files["contract/PklProject"] = _pkl_project(version=_OLDER).replace(
        "    uri =", "    // conformance: ignore[K007] pinned intentionally\n    uri ="
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K007"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "pinned intentionally"


# ---------------------------------------------------------------------------
# K008 — non-canonical toolkit source
# ---------------------------------------------------------------------------


def test_k008_noncanonical_source(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(
        base="github.com/someone/fork/app-contract-toolkit"
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K008"]
    assert len(findings) == 1
    assert findings[0].file == "contract/PklProject"


def test_k008_noncanonical_suppresses_version_check(tmp_path: Path) -> None:
    """A non-canonical source reports K008 only — the version floor is moot."""
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(
        version=_OLDER, base="example.com/fork/app-contract-toolkit"
    )
    ids = _ids([f for f in _scan(tmp_path, files) if f.rule_id in ("K007", "K008")])
    assert ids == ["K008"]


def test_k008_canonical_no_finding(tmp_path: Path) -> None:
    assert [f for f in _scan(tmp_path, _clean_files()) if f.rule_id == "K008"] == []


def test_k008_suppressed(tmp_path: Path) -> None:
    files = _clean_files()
    files["contract/PklProject"] = _pkl_project(
        base="example.com/fork/app-contract-toolkit"
    ).replace("    uri =", "    // conformance: ignore[K008] vendored fork\n    uri =")
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K008"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "vendored fork"


# ---------------------------------------------------------------------------
# K009 — unresolved scaffold placeholder
# ---------------------------------------------------------------------------


def test_k009_placeholder_in_yaml(tmp_path: Path) -> None:
    files = _clean_files()
    files["atlan.yaml"] = _BANNER + "app_id: {app_name}\n"
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].file == "atlan.yaml"


def test_k009_placeholder_in_json(tmp_path: Path) -> None:
    files = _clean_files()
    files["app/generated/manifest.json"] = '{"conn": "{connection_name}"}\n'
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].file == "app/generated/manifest.json"


def test_k009_double_brace_runtime_token_excluded(tmp_path: Path) -> None:
    """{{...}} E2E runtime-substitution tokens are intentional — never flagged."""
    files = _clean_files()
    files["app/generated/manifest.json"] = (
        '{"cred": "{{credential}}", "name": "{{name}}"}\n'
    )
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K009"] == []


def test_k009_deployment_name_token_is_legitimate(tmp_path: Path) -> None:
    """{deployment_name} is a deploy-time token the current toolkit still emits
    verbatim into every manifest task_queue — never a K009 leftover. Modeled on a
    real stale-toolkit manifest: {app_name} IS flagged, {deployment_name} is not."""
    files = _clean_files()
    files["app/generated/manifest.json"] = (
        "{\n"
        '  "app_name": "{app_name}",\n'
        '  "task_queue": "atlan-glue-{deployment_name}",\n'
        '  "args": {"connection": "{{connection}}"}\n'
        "}\n"
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].line == 2  # only the {app_name} line


def test_k009_suppressed_in_yaml(tmp_path: Path) -> None:
    files = _clean_files()
    files["atlan.yaml"] = (
        _BANNER
        + "# conformance: ignore[K009] legacy placeholder, migration tracked\n"
        + "app_id: {app_name}\n"
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert (
        findings[0].suppression_justification == "legacy placeholder, migration tracked"
    )


def test_k009_clean_no_finding(tmp_path: Path) -> None:
    assert [f for f in _scan(tmp_path, _clean_files()) if f.rule_id == "K009"] == []


def test_k009_non_utf8_artifact_does_not_crash(tmp_path: Path) -> None:
    """A non-UTF-8 blob under app/generated/ is skipped, never crashes the run."""
    for rel, content in _clean_files().items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (tmp_path / "app/generated/logo.bin").write_bytes(b"\xff\xfe\x00{app_name}")
    findings = scan_all(discover(tmp_path), tmp_path)
    assert [f for f in findings if f.rule_id == "K009"] == []


def test_k009_suppressed_after_earlier_hash(tmp_path: Path) -> None:
    """A directive after an earlier # (e.g. a URL fragment) still suppresses."""
    files = _clean_files()
    files["atlan.yaml"] = (
        _BANNER
        + 'app_id: "{app_name}#frag"  # conformance: ignore[K009] migration tracked\n'
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K009"]
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "migration tracked"


# ---------------------------------------------------------------------------
# K010 — missing E2E scaffolding
# ---------------------------------------------------------------------------


def test_k010_missing_e2e_base(tmp_path: Path) -> None:
    files = _clean_files()
    del files["app/generated/_e2e_base.py"]
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K010"]
    assert len(findings) == 1
    assert findings[0].file == "contract/app.pkl"


def test_k010_present_no_finding(tmp_path: Path) -> None:
    assert [f for f in _scan(tmp_path, _clean_files()) if f.rule_id == "K010"] == []


def test_k010_multi_entrypoint_bundle_skipped(tmp_path: Path) -> None:
    """A bundle (entrypoints block) emits E2E scaffolding per-entrypoint — no K010."""
    files = _clean_files()
    del files["app/generated/_e2e_base.py"]
    files["contract/app.pkl"] = (
        'amends "@app-contract-toolkit/App.pkl"\n\n'
        'name = "demo"\n\n'
        "entrypoints {\n"
        '  ["crawler"] { name = "crawler" }\n'
        "}\n"
    )
    assert [f for f in _scan(tmp_path, files) if f.rule_id == "K010"] == []


def test_k010_suppressed(tmp_path: Path) -> None:
    files = _clean_files()
    del files["app/generated/_e2e_base.py"]
    files["contract/app.pkl"] = (
        "// conformance: ignore[K010] utility app ships no e2e\n" + _APP_PKL
    )
    findings = [f for f in _scan(tmp_path, files) if f.rule_id == "K010"]
    assert len(findings) == 1
    assert findings[0].suppressed


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_rule_metadata_app_scoped_warn() -> None:
    for rid in ("K003", "K004", "K005", "K007", "K008", "K010"):
        rule = get_rule(rid)
        assert rule.scope == RuleScope.APP
        assert rule.tier == EnforcementTier.WARN
        assert rule.rationale, f"{rid} must have a non-empty rationale"


def test_k009_is_block_tier() -> None:
    """K009 is the one BLOCK-tier K-rule: an unresolved {app_name} ships a wrong
    artifact and is never a false positive, so it hard-fails the gate rather than
    landing as WARN."""
    rule = get_rule("K009")
    assert rule.scope == RuleScope.APP
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.rationale
