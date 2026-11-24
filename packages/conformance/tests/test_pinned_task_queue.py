"""Tests for K031 PinnedWorkerTaskQueue.

An explicit ``ATLAN_TASK_QUEUE`` in the deploy env overrides the SDK's
per-deployment queue, so the on-demand (``-od``) worker polls the base queue and
rerouted retries hang. K031 flags it in ``contract/*.pkl`` or, when the contract
does not say it, in ``atlan.yaml`` (including an app with no contract at all).
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.pinned_task_queue import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding

_RULE = "K031"

_ATLAN_DEPLOY_PIN = (
    "name: atlan-example-app\n"
    "deploy:\n"
    "  env:\n"
    "    ATLAN_TASK_QUEUE: atlan-example-production\n"
    "    OTHER: x\n"
)
_ATLAN_BOTH_PINS = (
    "name: atlan-example-app\n"
    "deploy:\n"
    "  env:\n"
    '    ATLAN_TASK_QUEUE: "atlan-example-production"\n'
    "pools:\n"
    "  default:\n"
    "    env:\n"
    "      ATLAN_TASK_QUEUE: atlan-example-production\n"
)
_CONTRACT_PIN = (
    'amends "@app-contract-toolkit/App.pkl"\n'
    "\n"
    "deploy = new DeployConfig {\n"
    "  env {\n"
    '    ["ATLAN_TASK_QUEUE"] = "atlan-example-production"\n'
    "  }\n"
    "}\n"
)
_CONTRACT_CLEAN = _CONTRACT_PIN.replace(
    '    ["ATLAN_TASK_QUEUE"] = "atlan-example-production"\n', ""
)


def _scan(
    tmp_path: Path,
    *,
    contracts: dict[str, str] | None = None,
    atlan: str | None = None,
) -> list[Finding]:
    for name, text in (contracts or {}).items():
        path = tmp_path / "contract" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if atlan is not None:
        (tmp_path / "atlan.yaml").write_text(atlan, encoding="utf-8")
    return scan_all(discover(tmp_path), tmp_path)


def _active(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.rule_id == _RULE and not f.suppressed]


def test_rule_metadata() -> None:
    rule = get_rule(_RULE)
    assert rule.name == "PinnedWorkerTaskQueue"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


def test_atlan_yaml_only_deploy_env_pin(tmp_path: Path) -> None:
    findings = _active(_scan(tmp_path, atlan=_ATLAN_DEPLOY_PIN))
    assert [(f.file, f.line) for f in findings] == [("atlan.yaml", 4)]
    assert "Delete this line from atlan.yaml" in findings[0].message


def test_atlan_yaml_deploy_and_pool_pins(tmp_path: Path) -> None:
    findings = _active(_scan(tmp_path, atlan=_ATLAN_BOTH_PINS))
    assert [f.line for f in findings] == [4, 8]


def test_contract_pin_wins_over_atlan_yaml(tmp_path: Path) -> None:
    findings = _active(
        _scan(tmp_path, contracts={"app.pkl": _CONTRACT_PIN}, atlan=_ATLAN_DEPLOY_PIN)
    )
    assert [(f.file, f.line) for f in findings] == [("contract/app.pkl", 5)]
    assert "Delete this line and regenerate" in findings[0].message


def test_stale_atlan_yaml_with_clean_contract(tmp_path: Path) -> None:
    findings = _active(
        _scan(tmp_path, contracts={"app.pkl": _CONTRACT_CLEAN}, atlan=_ATLAN_DEPLOY_PIN)
    )
    assert [(f.file, f.line) for f in findings] == [("atlan.yaml", 4)]
    assert "generated — do not edit it" in findings[0].message


def test_no_pin(tmp_path: Path) -> None:
    assert _scan(tmp_path, atlan="name: x\ndeploy:\n  env:\n    OTHER: x\n") == []


def test_fixed_app_comment_block_is_not_a_pin(tmp_path: Path) -> None:
    atlan = (
        "deploy:\n"
        "  env:\n"
        "    # No ATLAN_TASK_QUEUE here: the SDK derives the queue as\n"
        "    # atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}, which the chart\n"
        '    # suffixes with "-od" for the on-demand variant.\n'
        "    OTHER: x\n"
    )
    assert _scan(tmp_path, atlan=atlan) == []


def test_key_outside_deploy_env_is_not_a_pin(tmp_path: Path) -> None:
    atlan = "env:\n  ATLAN_TASK_QUEUE: q\ndeploy:\n  labels:\n    ATLAN_TASK_QUEUE: q\n"
    assert _scan(tmp_path, atlan=atlan) == []


def test_pkl_comment_or_string_is_not_a_pin(tmp_path: Path) -> None:
    pkl = (
        'amends "@app-contract-toolkit/App.pkl"\n'
        '// ["ATLAN_TASK_QUEUE"] = "q"\n'
        'longDescription = "set [\\"ATLAN_TASK_QUEUE\\"] = q to pin"\n'
        'env { ["OTHER"] = "x" } // ["ATLAN_TASK_QUEUE"] = "q"\n'
    )
    assert _scan(tmp_path, contracts={"app.pkl": pkl}) == []


def test_nothing_to_scan(tmp_path: Path) -> None:
    assert discover(tmp_path) == []
    assert scan_all(discover(tmp_path), tmp_path) == []


def test_atlan_yaml_suppression(tmp_path: Path) -> None:
    atlan = _ATLAN_DEPLOY_PIN.replace(
        "atlan-example-production\n",
        "atlan-example-production  # conformance: ignore[K031] queue pinned on purpose for a test\n",
    )
    findings = _scan(tmp_path, atlan=atlan)
    assert len(findings) == 1 and findings[0].suppressed


def test_contract_suppression(tmp_path: Path) -> None:
    pkl = _CONTRACT_PIN.replace(
        '"atlan-example-production"\n',
        '"atlan-example-production" // conformance: ignore[K031] pinned on purpose for a test\n',
    )
    findings = _scan(tmp_path, contracts={"app.pkl": pkl})
    assert len(findings) == 1 and findings[0].suppressed


def test_message_warns_about_legacy_queue_pins(tmp_path: Path) -> None:
    findings = _active(_scan(tmp_path, atlan=_ATLAN_DEPLOY_PIN))
    assert "legacy" in findings[0].message
