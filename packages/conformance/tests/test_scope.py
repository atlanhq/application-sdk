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
    # D-series is all-APP: out of scope on the SDK, in scope on an app.
    assert not _series_in_scope("D", RuleScope.SDK)
    assert _series_in_scope("D", RuleScope.APP)
    # C-series is mixed (C001 both, C002/C003 app) -> stays active on the SDK.
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


def test_runner_sdk_scope_skips_d_series(tmp_path: Path) -> None:
    _make_app_repo(tmp_path)  # same repo, but force SDK scope
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
    assert _rule_ids(out) == set()
    assert exit_code == 0


def test_runner_autodetects_app_scope(tmp_path: Path) -> None:
    """No --scope: the app's pyproject name auto-detects to app, so D001 fires."""
    _make_app_repo(tmp_path)
    out = tmp_path / "report.sarif"
    main(["--repo", str(tmp_path), "--series", "D", "--output", str(out)])
    assert "D001" in _rule_ids(out)


def test_runner_autodetects_sdk_scope_skips_d(tmp_path: Path) -> None:
    """An SDK-named repo auto-detects to sdk, so the app-scoped D-series is skipped."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "atlan-application-sdk"\nversion = "0.1.0"\n'
        'dependencies = ["requests>=2,<3"]\n',
        encoding="utf-8",
    )
    out = tmp_path / "report.sarif"
    exit_code = main(["--repo", str(tmp_path), "--series", "D", "--output", str(out)])
    assert _rule_ids(out) == set()
    assert exit_code == 0
