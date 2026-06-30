"""Tests for rule scope: runtime detection + runner filtering.

Covers:
- ``detect_scope`` classifying a repo as SDK / app / unknown from pyproject.
- The runner skipping an all-APP series (D) when scope is SDK.
- The runner honouring an explicit ``--scope`` over auto-detection.
- ``--scope app`` surfacing app-scoped findings (D001) on a consumer app.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite.checks._ast_common import detect_scope
from conformance.suite.runner import _rule_in_scope, _series_in_scope, main
from conformance.suite.schema.disposition import RuleScope

# ---------------------------------------------------------------------------
# detect_scope
# ---------------------------------------------------------------------------


def _write_pyproject(root: Path, name: str | None) -> None:
    if name is None:
        (root / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    else:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\n', encoding="utf-8"
        )


def test_detect_scope_sdk(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "atlan-application-sdk")
    assert detect_scope(tmp_path) == RuleScope.SDK


def test_detect_scope_sdk_sibling(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "atlan-application-sdk-conformance")
    assert detect_scope(tmp_path) == RuleScope.SDK


def test_detect_scope_app(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "my-connector")
    assert detect_scope(tmp_path) == RuleScope.APP


def test_detect_scope_no_pyproject(tmp_path: Path) -> None:
    assert detect_scope(tmp_path) is None


def test_detect_scope_no_project_name(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, None)
    assert detect_scope(tmp_path) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # SDK: exact name and hyphen-extended sibling packages.
        ("atlan-application-sdk", RuleScope.SDK),
        ("atlan-application-sdk-conformance", RuleScope.SDK),
        ("atlan_application_sdk", RuleScope.SDK),  # PEP 503 underscore form
        # APP: names that merely share the prefix as a substring must NOT match.
        ("atlan-application-sdk2", RuleScope.APP),
        ("atlan-application-sdkk", RuleScope.APP),
        ("my-connector", RuleScope.APP),
    ],
)
def test_detect_scope_prefix_boundary(
    tmp_path: Path, name: str, expected: RuleScope
) -> None:
    """The SDK prefix is hyphen-anchored, not a loose substring match."""
    _write_pyproject(tmp_path, name)
    assert detect_scope(tmp_path) == expected


def test_sdk_prefix_matches_dependency_checker() -> None:
    """Guard the comment-synced duplication of the SDK package name.

    ``_scope.SDK_PACKAGE_PREFIX`` and ``dependency_conformance.SDK_PACKAGE`` encode
    the same Atlan distribution name in two places (kept in sync by comment rather
    than a shared module).  This catches drift without taking on that layering cost.
    """
    from conformance.suite.checks import dependency_conformance
    from conformance.suite.checks._ast_common import SDK_PACKAGE_PREFIX

    assert SDK_PACKAGE_PREFIX == dependency_conformance.SDK_PACKAGE


@pytest.mark.parametrize(
    ("name", "is_sdk"),
    [
        ("atlan-application-sdk", True),
        ("atlan-application-sdk-conformance", True),
        ("atlan_application_sdk", True),  # PEP 503 underscore form
        ("atlan-application-sdk2", False),  # substring lookalike
        ("atlan-application-sdkk", False),
        ("my-connector", False),
    ],
)
def test_is_self_check_matches_shared_helper(name: str, is_sdk: bool) -> None:
    """`_is_self_check` and the shared `is_sdk_package_name` must agree.

    Both now route through `is_sdk_package_name`, so the matching *semantics*
    (hyphen-anchored, not just the constant) cannot drift between the D-series
    self-exemption and the runner-side scope detection.
    """
    from conformance.suite.checks import dependency_conformance
    from conformance.suite.checks._ast_common import is_sdk_package_name

    assert is_sdk_package_name(name) is is_sdk
    assert dependency_conformance._is_self_check(name) is is_sdk


def test_is_self_check_handles_none() -> None:
    """`_is_self_check(None)` stays False (the original None-guard is preserved)."""
    from conformance.suite.checks import dependency_conformance

    assert dependency_conformance._is_self_check(None) is False


# ---------------------------------------------------------------------------
# Scope predicates
# ---------------------------------------------------------------------------


def test_rule_in_scope_predicate() -> None:
    assert _rule_in_scope(RuleScope.BOTH, RuleScope.SDK)
    assert _rule_in_scope(RuleScope.APP, RuleScope.APP)
    assert not _rule_in_scope(RuleScope.APP, RuleScope.SDK)
    # Unknown active scope -> everything is in scope.
    assert _rule_in_scope(RuleScope.APP, None)


def test_series_in_scope_predicate() -> None:
    # D-series is mixed (D001/D002 app, D003 both) -> active on both surfaces;
    # the SDK runs only its in-scope D003 (D001/D002 are post-filtered out).
    assert _series_in_scope("D", RuleScope.SDK)
    assert _series_in_scope("D", RuleScope.APP)
    # C-series is mixed (C001/C003 both, C002 app) -> stays active on the SDK.
    assert _series_in_scope("C", RuleScope.SDK)


# ---------------------------------------------------------------------------
# Runner end-to-end (D-series: app-scoped)
# ---------------------------------------------------------------------------


def _make_app_repo(root: Path) -> None:
    """An app pyproject with no atlan-application-sdk dependency -> triggers D001."""
    (root / "pyproject.toml").write_text(
        '[project]\nname = "my-connector"\nversion = "0.1.0"\n'
        'dependencies = ["requests>=2,<3"]\n',
        encoding="utf-8",
    )


def _rule_ids(sarif_path: Path) -> set[str]:
    data = json.loads(sarif_path.read_text(encoding="utf-8"))
    return {r["ruleId"] for r in data["runs"][0]["results"]}


def test_runner_app_scope_surfaces_d001(tmp_path: Path) -> None:
    _make_app_repo(tmp_path)
    out = tmp_path / "report.sarif"
    exit_code = main(
        [
            "--repo",
            str(tmp_path),
            "--series",
            "D",
            "--scope",
            "app",
            "--output",
            str(out),
        ]
    )
    assert "D001" in _rule_ids(out)
    assert exit_code == 1  # D001 is block-tier


def _make_sdk_repo_with_unused_dep(root: Path) -> None:
    """An SDK-named repo declaring an installed-but-unimported dependency.

    ``pydantic`` is a direct conformance dependency, so it is always importable
    in the test environment; with no source files importing it, D003 fires
    deterministically.
    """
    (root / "pyproject.toml").write_text(
        '[project]\nname = "atlan-application-sdk"\nversion = "0.1.0"\n'
        'dependencies = ["pydantic>=2,<3"]\n',
        encoding="utf-8",
    )


def test_runner_sdk_scope_runs_d003_filters_app_rules(tmp_path: Path) -> None:
    """Under SDK scope the D-series runs (D003 is scope=both) but D001/D002,
    which are app-only, are filtered out."""
    _make_sdk_repo_with_unused_dep(tmp_path)
    out = tmp_path / "report.sarif"
    exit_code = main(
        [
            "--repo",
            str(tmp_path),
            "--series",
            "D",
            "--scope",
            "sdk",
            "--output",
            str(out),
        ]
    )
    ids = _rule_ids(out)
    assert ids == {"D003"}  # D003 runs on the SDK; D001/D002 are app-only, filtered
    assert exit_code == 0  # D003 is warn-tier


def test_runner_autodetects_app_scope(tmp_path: Path) -> None:
    """No --scope: the app's pyproject name auto-detects to app, so D001 fires."""
    _make_app_repo(tmp_path)
    out = tmp_path / "report.sarif"
    main(["--repo", str(tmp_path), "--series", "D", "--output", str(out)])
    assert "D001" in _rule_ids(out)


def test_runner_autodetects_sdk_scope_runs_d003(tmp_path: Path) -> None:
    """An SDK-named repo auto-detects to sdk: the app-scoped D001/D002 are
    filtered, but the both-scoped D003 still runs."""
    _make_sdk_repo_with_unused_dep(tmp_path)
    out = tmp_path / "report.sarif"
    exit_code = main(["--repo", str(tmp_path), "--series", "D", "--output", str(out)])
    ids = _rule_ids(out)
    assert ids == {"D003"}
    assert "D001" not in ids and "D002" not in ids
    assert exit_code == 0  # D003 is warn-tier


# ---------------------------------------------------------------------------
# --exit-zero: process exits 0 even on blocking violations; SARIF unchanged
# ---------------------------------------------------------------------------


def test_runner_exit_zero_returns_zero_on_blocking_violation(tmp_path: Path) -> None:
    """--exit-zero makes main() return 0 even when a BLOCK-tier rule fires."""
    _make_app_repo(tmp_path)
    out = tmp_path / "report.sarif"
    exit_code = main(
        [
            "--repo",
            str(tmp_path),
            "--series",
            "D",
            "--scope",
            "app",
            "--exit-zero",
            "--output",
            str(out),
        ]
    )
    assert exit_code == 0  # process exits 0 despite D001 (block-tier)


def test_runner_exit_zero_sarif_still_records_real_exit_code(tmp_path: Path) -> None:
    """SARIF invocation.exitCode remains 1 when --exit-zero suppresses the process exit."""
    _make_app_repo(tmp_path)
    out = tmp_path / "report.sarif"
    main(
        [
            "--repo",
            str(tmp_path),
            "--series",
            "D",
            "--scope",
            "app",
            "--exit-zero",
            "--output",
            str(out),
        ]
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    sarif_exit_code = data["runs"][0]["invocations"][0]["exitCode"]
    assert sarif_exit_code == 1  # SARIF honestly records that violations were found


def test_runner_exit_zero_false_preserves_normal_gating(tmp_path: Path) -> None:
    """Without --exit-zero (default), a BLOCK-tier violation still exits 1."""
    _make_app_repo(tmp_path)
    out = tmp_path / "report.sarif"
    exit_code = main(
        [
            "--repo",
            str(tmp_path),
            "--series",
            "D",
            "--scope",
            "app",
            "--output",
            str(out),
        ]
    )
    assert exit_code == 1  # hard gate unchanged when --exit-zero is absent
