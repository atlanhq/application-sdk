"""Meta-tests for the T016 e2e-deployment-name check.

T016 catches an e2e CI docker-compose overlay that hard-codes
``ATLAN_DEPLOYMENT_NAME`` instead of inheriting the per-leg value the SDK's
``sdr-e2e`` action exports to ``$GITHUB_ENV``. A hard-coded value overrides the
inherited env, so the worker container polls a different Temporal queue than the
harness dispatches to — the failure mode that hung atlan-mysql-app's e2e for
~20 min ("No Workers Running").
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.e2e_deployment_name import discover, scan_path, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t016_rule_metadata() -> None:
    rule = get_rule("T016")
    assert rule.name == "E2EDeploymentNameNotInherited"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.category == "e2e-ci"


# ── Fixtures ─────────────────────────────────────────────────────────────────

_HARDCODED_LIST = (
    "services:\n"
    "  atlan-app:\n"
    "    environment:\n"
    "      - ATLAN_DEPLOYMENT_NAME=e2e-full-ci-${GITHUB_RUN_ID}\n"
)
_INHERITED_LIST = (
    "services:\n"
    "  atlan-app:\n"
    "    environment:\n"
    "      - ATLAN_DEPLOYMENT_NAME=${ATLAN_DEPLOYMENT_NAME:-e2e-full-ci-${GITHUB_RUN_ID}}\n"
)
_PASSTHROUGH_LIST = (
    "services:\n  atlan-app:\n    environment:\n      - ATLAN_DEPLOYMENT_NAME\n"
)
_HARDCODED_MAP = (
    "services:\n"
    "  atlan-app:\n"
    "    environment:\n"
    '      ATLAN_DEPLOYMENT_NAME: "e2e-full-ci-${GITHUB_RUN_ID}"\n'
)
_INHERITED_MAP = (
    "services:\n"
    "  atlan-app:\n"
    "    environment:\n"
    '      ATLAN_DEPLOYMENT_NAME: "${ATLAN_DEPLOYMENT_NAME:-e2e-full-ci-${GITHUB_RUN_ID}}"\n'
)


# ── Positive detections ──────────────────────────────────────────────────────


def test_flags_hardcoded_list_form() -> None:
    findings = scan_text(_HARDCODED_LIST, ".github/e2e/e2e-full-docker-compose.yaml")
    assert [f.rule_id for f in findings] == ["T016"]
    assert findings[0].line == 4


def test_flags_hardcoded_map_form() -> None:
    findings = scan_text(_HARDCODED_MAP, ".github/e2e/e2e-full-docker-compose.yaml")
    assert [f.rule_id for f in findings] == ["T016"]
    assert findings[0].line == 4


def test_flags_bare_hardcoded_value_without_interpolation() -> None:
    text = (
        "services:\n"
        "  atlan-app:\n"
        "    environment:\n"
        "      - ATLAN_DEPLOYMENT_NAME=production\n"
    )
    findings = scan_text(text, ".github/e2e/overlay.yaml")
    assert [f.rule_id for f in findings] == ["T016"]


# ── Compliant forms (no finding) ─────────────────────────────────────────────


def test_passes_inherited_list_form() -> None:
    assert scan_text(_INHERITED_LIST, ".github/e2e/overlay.yaml") == []


def test_passes_inherited_map_form() -> None:
    assert scan_text(_INHERITED_MAP, ".github/e2e/overlay.yaml") == []


def test_passes_bare_passthrough_list_entry() -> None:
    assert scan_text(_PASSTHROUGH_LIST, ".github/e2e/overlay.yaml") == []


def test_passes_plain_interpolation_without_default() -> None:
    text = (
        "services:\n"
        "  atlan-app:\n"
        "    environment:\n"
        "      - ATLAN_DEPLOYMENT_NAME=${ATLAN_DEPLOYMENT_NAME}\n"
    )
    assert scan_text(text, ".github/e2e/overlay.yaml") == []


# ── No-ops / scoping ─────────────────────────────────────────────────────────


def test_noop_on_non_compose_yaml() -> None:
    # A GitHub workflow file mentions the var but has no top-level services: key.
    text = (
        "jobs:\n"
        "  e2e:\n"
        "    steps:\n"
        "      - run: echo ATLAN_DEPLOYMENT_NAME=e2e-full-ci-${GITHUB_RUN_ID}\n"
    )
    assert scan_text(text, ".github/workflows/tests.yaml") == []


def test_noop_when_var_absent() -> None:
    text = "services:\n  atlan-app:\n    environment:\n      - FOO=bar\n"
    assert scan_text(text, ".github/e2e/overlay.yaml") == []


# ── Inline suppression ───────────────────────────────────────────────────────


def test_suppression_directive_on_same_line() -> None:
    text = (
        "services:\n"
        "  atlan-app:\n"
        "    environment:\n"
        "      - ATLAN_DEPLOYMENT_NAME=e2e-full-ci-${GITHUB_RUN_ID}  "
        "# conformance: ignore[T016] single-queue by design\n"
    )
    findings = scan_text(text, ".github/e2e/overlay.yaml")
    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert findings[0].suppression_justification == "single-queue by design"


# ── Discovery + scan_path integration ────────────────────────────────────────


def test_discover_finds_e2e_overlay_and_skips_workflows(tmp_path: Path) -> None:
    e2e_dir = tmp_path / ".github" / "e2e"
    e2e_dir.mkdir(parents=True)
    overlay = e2e_dir / "e2e-full-docker-compose.yaml"
    overlay.write_text(_HARDCODED_MAP, encoding="utf-8")

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "tests.yaml").write_text(
        "jobs:\n  e2e:\n    steps:\n      - run: echo ATLAN_DEPLOYMENT_NAME=x\n",
        encoding="utf-8",
    )

    discovered = discover(tmp_path)
    assert discovered == [overlay]

    findings = scan_path(overlay, tmp_path)
    assert [f.rule_id for f in findings] == ["T016"]
    assert findings[0].file == ".github/e2e/e2e-full-docker-compose.yaml"


def test_discover_empty_without_github_dir(tmp_path: Path) -> None:
    assert discover(tmp_path) == []
