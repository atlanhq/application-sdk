"""Tests for K029 DeploymentObjectStoreDisabled.

The SDK requires the deployment object store, and the atlan-app chart omits
its Dapr component when ``atlan.yaml`` carries ``deploy.dapr.objectstore:
false`` — the worker and handler then fail at startup. K029 flags the setting
at its source (an explicit ``objectstore = false`` in ``contract/*.pkl``) or,
when the source does not say it, in the generated ``atlan.yaml`` (where an
older contract-toolkit defaulted it to ``false``).

The ``atlan.yaml`` fixtures follow the ``deploy:`` block shape contract-toolkit
renders from ``deploy = new DeployConfig { dapr { secretstore = true } ... }``
(``contract-toolkit/examples/deploy`` evaluated with that toggle added).
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.dapr_objectstore import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding

_RULE = "K029"

_CONTRACT_NO_DAPR = 'amends "@app-contract-toolkit/App.pkl"\n\nname = "x"\n'


def _atlan(objectstore: str | None) -> str:
    dapr = (
        ""
        if objectstore is None
        else (
            "  dapr:\n"
            f"    objectstore: {objectstore}\n"
            "    secretstore: true\n"
            "    statestore: false\n"
        )
    )
    return (
        "name: x\n"
        "release_model: semver\n"
        "deploy:\n"
        "  execution_mode: native\n"
        "  splitDeploymentEnabled: true\n"
        f"{dapr}"
        "  keda:\n"
        "    enabled: true\n"
    )


def _scan(
    tmp_path: Path,
    *,
    contract: str | None = _CONTRACT_NO_DAPR,
    atlan: str | None = None,
) -> list[Finding]:
    if contract is not None:
        (tmp_path / "contract").mkdir()
        (tmp_path / "contract" / "app.pkl").write_text(contract, encoding="utf-8")
    if atlan is not None:
        (tmp_path / "atlan.yaml").write_text(atlan, encoding="utf-8")
    return scan_all(discover(tmp_path), tmp_path)


def _active(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.rule_id == _RULE and not f.suppressed]


def test_rule_metadata() -> None:
    rule = get_rule(_RULE)
    assert rule.name == "DeploymentObjectStoreDisabled"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


def test_generated_objectstore_false_is_flagged_on_atlan_yaml(tmp_path: Path) -> None:
    findings = _active(_scan(tmp_path, atlan=_atlan("false")))

    assert [(f.file, f.line) for f in findings] == [("atlan.yaml", 7)]
    assert "deploy.dapr.objectstore" in findings[0].message


def test_generated_objectstore_true_is_not_flagged(tmp_path: Path) -> None:
    assert _active(_scan(tmp_path, atlan=_atlan("true"))) == []


def test_absent_dapr_block_is_not_flagged(tmp_path: Path) -> None:
    """At the toolkit defaults no dapr block is rendered; the store stays on."""
    assert _active(_scan(tmp_path, atlan=_atlan(None))) == []


def test_objectstore_false_outside_deploy_dapr_is_not_flagged(tmp_path: Path) -> None:
    atlan = "name: x\nsettings:\n  dapr:\n    objectstore: false\n"
    assert _active(_scan(tmp_path, atlan=atlan)) == []


def test_explicit_false_in_contract_is_flagged_once_at_the_source(
    tmp_path: Path,
) -> None:
    contract = (
        'amends "@app-contract-toolkit/App.pkl"\n'
        "\n"
        "deploy = new DeployConfig {\n"
        "  dapr { objectstore = false; secretstore = true }\n"
        "}\n"
    )
    findings = _active(_scan(tmp_path, contract=contract, atlan=_atlan("false")))

    assert [(f.file, f.line) for f in findings] == [("contract/app.pkl", 4)]


def test_explicit_true_in_contract_is_not_flagged(tmp_path: Path) -> None:
    contract = "deploy = new DeployConfig {\n  dapr { objectstore = true }\n}\n"
    assert _active(_scan(tmp_path, contract=contract, atlan=_atlan("true"))) == []


def test_commented_out_assignment_is_not_flagged(tmp_path: Path) -> None:
    contract = "deploy = new DeployConfig {\n  // dapr { objectstore = false }\n}\n"
    assert _active(_scan(tmp_path, contract=contract)) == []


def test_contract_finding_can_be_suppressed(tmp_path: Path) -> None:
    contract = (
        "deploy = new DeployConfig {\n"
        "  // conformance: ignore[K029] this app writes nothing to the object store\n"
        "  dapr { objectstore = false }\n"
        "}\n"
    )
    findings = [f for f in _scan(tmp_path, contract=contract) if f.rule_id == _RULE]

    assert [f.suppressed for f in findings] == [True]


def test_repo_without_contract_dir_is_skipped(tmp_path: Path) -> None:
    assert _scan(tmp_path, contract=None, atlan=_atlan("false")) == []


def test_runner_exclude_drops_findings_anchored_under_the_prefix(
    tmp_path: Path,
) -> None:
    """``--exclude contract/`` drops K029's contract finding.

    K029 discovers the repo root, which no prefix excludes, so the runner must
    filter at the finding level for ``--exclude`` to apply to every check.
    """
    import json

    from conformance.suite.runner import main as runner_main

    contract = (
        'amends "@app-contract-toolkit/App.pkl"\n'
        "\n"
        "deploy = new DeployConfig {\n"
        "  dapr { objectstore = false }\n"
        "}\n"
    )
    _scan(tmp_path, contract=contract)

    def uris(*extra: str) -> set[str]:
        out = tmp_path / "out.sarif"
        runner_main(
            ["--repo", str(tmp_path), "--rule", _RULE, "--output", str(out), *extra]
        )
        results = json.loads(out.read_text())["runs"][0]["results"]
        return {
            r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
            for r in results
        }

    assert uris() == {"contract/app.pkl"}
    assert uris("--exclude", "contract/") == set()


def test_objectstore_false_inside_a_string_is_not_flagged(tmp_path: Path) -> None:
    contract = (
        'amends "@app-contract-toolkit/App.pkl"\n'
        'longDescription = """\n'
        "Do not set objectstore = false.\n"
        '"""\n'
        "/* objectstore = false */\n"
    )
    assert _active(_scan(tmp_path, contract=contract, atlan=_atlan("true"))) == []
