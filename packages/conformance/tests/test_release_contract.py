"""Tests for K011 AppIdMissingFromContract and K012 GeneratePoeTaskMissing.

Both rules are cross-artifact release-readiness guards driven by ``scan_all``:

* K011 fires when a contract-driven app repo's generated ``atlan.yaml`` has no
  top-level ``app_id`` (the Global Marketplace identity the publish step POSTs).
* K012 fires when ``pyproject.toml`` defines no ``[tool.poe.tasks.generate]``
  task (the SDK Certify step runs ``uv run poe generate``).

Test helpers
------------
``_app_repo``: scaffolds a minimal app repo under ``tmp_path`` — a ``contract/``
dir (the discover guard), plus optional ``atlan.yaml`` / ``pyproject.toml``
bodies — then returns the ``scan_all`` findings for it.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.release_contract import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding

# A well-formed atlan.yaml fragment carrying the top-level app_id key.
_ATLAN_WITH_APP_ID = (
    "name: openapi\n"
    "app_id: 019d1f6b-6fea-7db3-96d8-e61e159d0351\n"
    "display_name: OpenAPI Spec Loader\n"
    "release_model: semver\n"
)
_ATLAN_WITHOUT_APP_ID = (
    "name: openapi\n" "display_name: OpenAPI Spec Loader\n" "release_model: semver\n"
)

_PYPROJECT_WITH_GENERATE = (
    "[tool.poe.tasks]\n"
    'start-deps = "echo none"\n'
    'generate = "pkl eval --project-dir contract -m . contract/app.pkl"\n'
)
_PYPROJECT_WITHOUT_GENERATE = '[tool.poe.tasks]\nstart-deps = "echo none"\n'


def _app_repo(
    tmp_path: Path,
    *,
    contract: bool = True,
    atlan: str | None = None,
    pyproject: str | None = None,
) -> list[Finding]:
    """Scaffold a repo under *tmp_path* and return its ``scan_all`` findings."""
    if contract:
        (tmp_path / "contract").mkdir()
        (tmp_path / "contract" / "app.pkl").write_text('name = "x"\n', encoding="utf-8")
    if atlan is not None:
        (tmp_path / "atlan.yaml").write_text(atlan, encoding="utf-8")
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return scan_all(discover(tmp_path), tmp_path)


def _ids(findings: list[Finding]) -> list[str]:
    """Rule IDs of unsuppressed findings (matching runner gate semantics)."""
    return [f.rule_id for f in findings if not f.suppressed]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k011_rule_metadata() -> None:
    rule = get_rule("K011")
    assert rule.name == "AppIdMissingFromContract"
    assert rule.tier is EnforcementTier.BLOCK
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


def test_k012_rule_metadata() -> None:
    rule = get_rule("K012")
    assert rule.name == "GeneratePoeTaskMissing"
    assert rule.tier is EnforcementTier.BLOCK
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


# ---------------------------------------------------------------------------
# discover — the "is this a contract-driven app repo?" guard
# ---------------------------------------------------------------------------


def test_discover_requires_contract_dir(tmp_path: Path) -> None:
    assert discover(tmp_path) == []  # no contract/ dir → not in scope
    (tmp_path / "contract").mkdir()
    assert discover(tmp_path) == [tmp_path]


def test_scan_all_noops_without_contract(tmp_path: Path) -> None:
    # Even with a broken atlan.yaml/pyproject present, no contract/ → no paths.
    (tmp_path / "atlan.yaml").write_text(_ATLAN_WITHOUT_APP_ID, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        _PYPROJECT_WITHOUT_GENERATE, encoding="utf-8"
    )
    assert scan_all(discover(tmp_path), tmp_path) == []


# ---------------------------------------------------------------------------
# K011 — app_id in atlan.yaml
# ---------------------------------------------------------------------------


def test_k011_fires_when_app_id_missing(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITHOUT_APP_ID)
    assert "K011" in _ids(findings)


def test_k011_silent_when_app_id_present(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITH_APP_ID)
    assert "K011" not in _ids(findings)


def test_k011_anchored_in_atlan_yaml(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, atlan=_ATLAN_WITHOUT_APP_ID)
    k011 = [f for f in findings if f.rule_id == "K011"]
    assert k011
    assert k011[0].file == "atlan.yaml"


def test_k011_silent_when_no_atlan_yaml(tmp_path: Path) -> None:
    # A missing generated output is K004's concern, not K011's — no double-flag.
    findings = _app_repo(tmp_path, atlan=None)
    assert "K011" not in _ids(findings)


def test_k011_nested_app_id_does_not_satisfy(tmp_path: Path) -> None:
    # An indented app_id under some other mapping is not the top-level identity.
    nested = "name: openapi\nmetadata:\n  app_id: nested\nrelease_model: semver\n"
    findings = _app_repo(tmp_path, atlan=nested)
    assert "K011" in _ids(findings)


def test_k011_fires_on_empty_or_null_value(tmp_path: Path) -> None:
    # A present app_id set to empty-quotes or a YAML null literal still POSTs an
    # empty/None identity and hits the same 404 — treat it as missing.
    for i, empty in enumerate(
        ('app_id: ""', "app_id: ''", "app_id: null", "app_id: ~")
    ):
        sub = tmp_path / f"case{i}"
        sub.mkdir()
        body = f"name: openapi\n{empty}\nrelease_model: semver\n"
        findings = _app_repo(sub, atlan=body)
        assert "K011" in _ids(findings), empty


def test_k011_suppressed_by_directive(tmp_path: Path) -> None:
    body = "# conformance: ignore[K011] non-published app\n" + _ATLAN_WITHOUT_APP_ID
    findings = _app_repo(tmp_path, atlan=body)
    k011 = [f for f in findings if f.rule_id == "K011"]
    assert k011
    assert k011[0].suppressed is True
    assert "K011" not in _ids(findings)


# ---------------------------------------------------------------------------
# K012 — generate poe task in pyproject.toml
# ---------------------------------------------------------------------------


def test_k012_fires_when_generate_task_missing(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, pyproject=_PYPROJECT_WITHOUT_GENERATE)
    assert "K012" in _ids(findings)


def test_k012_silent_when_generate_task_present(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, pyproject=_PYPROJECT_WITH_GENERATE)
    assert "K012" not in _ids(findings)


def test_k012_fires_when_no_poe_tasks_table(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, pyproject='[project]\nname = "openapi"\n')
    assert "K012" in _ids(findings)


def test_k012_anchored_on_poe_tasks_header(tmp_path: Path) -> None:
    body = '[project]\nname = "x"\n\n[tool.poe.tasks]\nstart-deps = "echo none"\n'
    findings = _app_repo(tmp_path, pyproject=body)
    k012 = [f for f in findings if f.rule_id == "K012"]
    assert k012
    assert k012[0].file == "pyproject.toml"
    assert k012[0].line == 4  # the [tool.poe.tasks] header line


def test_k012_fires_on_empty_generate_command(tmp_path: Path) -> None:
    # A present but empty generate task is not a runnable target — 'uv run poe
    # generate' still fails, so K012 must fire.
    body = '[tool.poe.tasks]\nstart-deps = "echo none"\ngenerate = ""\n'
    findings = _app_repo(tmp_path, pyproject=body)
    assert "K012" in _ids(findings)


def test_k012_fires_on_malformed_pyproject(tmp_path: Path) -> None:
    # A malformed pyproject.toml is treated as "no tasks declared": the check
    # neither crashes nor false-negatives, so K012 fires.
    findings = _app_repo(tmp_path, pyproject="[tool.poe.tasks\nbroken = \n")
    assert "K012" in _ids(findings)


def test_k012_silent_when_no_pyproject(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, pyproject=None)
    assert "K012" not in _ids(findings)


def test_k012_suppressed_by_directive(tmp_path: Path) -> None:
    body = "# conformance: ignore[K012] never published\n" + _PYPROJECT_WITHOUT_GENERATE
    findings = _app_repo(tmp_path, pyproject=body)
    k012 = [f for f in findings if f.rule_id == "K012"]
    assert k012
    assert k012[0].suppressed is True
    assert "K012" not in _ids(findings)


# ---------------------------------------------------------------------------
# Both together — the exact regression that broke the openapi releases
# ---------------------------------------------------------------------------


def test_both_fire_on_fully_broken_release_setup(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path, atlan=_ATLAN_WITHOUT_APP_ID, pyproject=_PYPROJECT_WITHOUT_GENERATE
    )
    assert set(_ids(findings)) == {"K011", "K012"}


def test_clean_setup_produces_no_findings(tmp_path: Path) -> None:
    findings = _app_repo(
        tmp_path, atlan=_ATLAN_WITH_APP_ID, pyproject=_PYPROJECT_WITH_GENERATE
    )
    assert _ids(findings) == []
