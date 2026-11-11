"""Tests for K030 CredentialSecretStoreDisabled.

The SDK resolves every credential through the Dapr secret store, and the
deployer leaves the component out when ``atlan.yaml`` carries
``deploy.dapr.secretstore: false``. K030 flags that on an app that resolves
credentials — at the explicit ``secretstore = false`` in ``contract/*.pkl`` or,
when the source does not say it, in the generated ``atlan.yaml``.

The ``atlan.yaml`` fixtures follow the ``deploy:`` block shape contract-toolkit
renders for ``deploy = new DeployConfig {}`` (``contract-toolkit/examples/deploy``).
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.dapr_secretstore import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding

_RULE = "K030"

_HEADER = 'amends "@app-contract-toolkit/App.pkl"\n\nname = "x"\n'
_CRED_APP = _HEADER + "deploy = new DeployConfig {}\n"
_NO_CRED_APP = _HEADER + "hasCredentialConfig = false\ndeploy = new DeployConfig {}\n"
_SYSTEM_APP = _HEADER + 'type = "system"\ndeploy = new DeployConfig {}\n'


def _atlan(secretstore: str | None) -> str:
    dapr = (
        ""
        if secretstore is None
        else (
            "  dapr:\n"
            "    objectstore: true\n"
            f"    secretstore: {secretstore}\n"
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
    contracts: dict[str, str] | None = None,
    atlan: str | None = None,
) -> list[Finding]:
    if contracts is not None:
        for name, text in contracts.items():
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
    assert rule.name == "CredentialSecretStoreDisabled"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.category == "contract-toolkit"


def test_credential_app_generated_false_is_flagged_on_atlan_yaml(
    tmp_path: Path,
) -> None:
    """The inherited case: the toolkit rendered false for a contract that never said it."""
    findings = _active(
        _scan(tmp_path, contracts={"app.pkl": _CRED_APP}, atlan=_atlan("false"))
    )

    assert [(f.file, f.line) for f in findings] == [("atlan.yaml", 8)]
    assert "deploy.dapr.secretstore" in findings[0].message


def test_credential_app_generated_true_is_not_flagged(tmp_path: Path) -> None:
    assert (
        _active(_scan(tmp_path, contracts={"app.pkl": _CRED_APP}, atlan=_atlan("true")))
        == []
    )


def test_absent_dapr_block_is_not_flagged(tmp_path: Path) -> None:
    """With no dapr block the deployer's own default turns the secret store on."""
    assert (
        _active(_scan(tmp_path, contracts={"app.pkl": _CRED_APP}, atlan=_atlan(None)))
        == []
    )


def test_app_without_credentials_is_not_flagged(tmp_path: Path) -> None:
    assert (
        _active(
            _scan(tmp_path, contracts={"app.pkl": _NO_CRED_APP}, atlan=_atlan("false"))
        )
        == []
    )


def test_system_app_is_not_flagged(tmp_path: Path) -> None:
    """``hasCredentialConfig`` defaults to false for ``type = "system"``."""
    assert (
        _active(
            _scan(tmp_path, contracts={"app.pkl": _SYSTEM_APP}, atlan=_atlan("false"))
        )
        == []
    )


def test_system_app_that_opts_into_credentials_is_flagged(tmp_path: Path) -> None:
    contract = _SYSTEM_APP + "hasCredentialConfig = true\n"
    findings = _active(
        _scan(tmp_path, contracts={"app.pkl": contract}, atlan=_atlan("false"))
    )

    assert [f.file for f in findings] == ["atlan.yaml"]


def test_additional_credential_configs_count_as_credentials(tmp_path: Path) -> None:
    contract = (
        _NO_CRED_APP
        + 'additionalCredentialConfigs {\n  new CredentialConfig { name = "x" }\n}\n'
    )
    findings = _active(
        _scan(tmp_path, contracts={"app.pkl": contract}, atlan=_atlan("false"))
    )

    assert [f.file for f in findings] == ["atlan.yaml"]


def test_bundle_counts_entrypoint_credentials(tmp_path: Path) -> None:
    """A bundle root without credentials whose entrypoint keeps them on."""
    root = (
        _NO_CRED_APP
        + 'import "crawler.pkl" as Crawler\n'
        + "entrypoints { new Entrypoint { contract = Crawler } }\n"
    )
    crawler = 'amends "@app-contract-toolkit/App.pkl"\n\nname = "crawler"\n'
    findings = _active(
        _scan(
            tmp_path,
            contracts={"app.pkl": root, "crawler.pkl": crawler},
            atlan=_atlan("false"),
        )
    )

    assert [f.file for f in findings] == ["atlan.yaml"]


def test_helper_module_is_not_a_contract(tmp_path: Path) -> None:
    """A pkl file that does not amend App.pkl carries no credential default."""
    helper = 'module objectstore\n\nimport "@app-contract-toolkit/App.pkl"\n'
    assert (
        _active(
            _scan(
                tmp_path,
                contracts={"app.pkl": _NO_CRED_APP, "objectstore.pkl": helper},
                atlan=_atlan("false"),
            )
        )
        == []
    )


def test_commented_out_has_credential_config_false_is_ignored(tmp_path: Path) -> None:
    contract = _HEADER + "// hasCredentialConfig = false\n"
    findings = _active(
        _scan(tmp_path, contracts={"app.pkl": contract}, atlan=_atlan("false"))
    )

    assert [f.file for f in findings] == ["atlan.yaml"]


def test_explicit_false_in_contract_is_flagged_once_at_the_source(
    tmp_path: Path,
) -> None:
    contract = _HEADER + (
        "deploy = new DeployConfig {\n"
        "  dapr { objectstore = true; secretstore = false }\n"
        "}\n"
    )
    findings = _active(
        _scan(tmp_path, contracts={"app.pkl": contract}, atlan=_atlan("false"))
    )

    assert [(f.file, f.line) for f in findings] == [("contract/app.pkl", 5)]


def test_explicit_false_on_app_without_credentials_is_not_flagged(
    tmp_path: Path,
) -> None:
    contract = (
        _NO_CRED_APP
        + "deploy = new DeployConfig {\n  dapr { secretstore = false }\n}\n"
    )
    assert _active(_scan(tmp_path, contracts={"app.pkl": contract})) == []


def test_contract_finding_can_be_suppressed(tmp_path: Path) -> None:
    contract = _HEADER + (
        "deploy = new DeployConfig {\n"
        "  // conformance: ignore[K030] credentials come from env, not Dapr\n"
        "  dapr { secretstore = false }\n"
        "}\n"
    )
    findings = [
        f
        for f in _scan(tmp_path, contracts={"app.pkl": contract})
        if f.rule_id == _RULE
    ]

    assert [f.suppressed for f in findings] == [True]


def test_atlan_yaml_finding_can_be_suppressed(tmp_path: Path) -> None:
    atlan = _atlan("false").replace(
        "    secretstore: false\n",
        "    secretstore: false  # conformance: ignore[K030] credentials come from env\n",
    )
    findings = [
        f
        for f in _scan(tmp_path, contracts={"app.pkl": _CRED_APP}, atlan=atlan)
        if f.rule_id == _RULE
    ]

    assert [f.suppressed for f in findings] == [True]


def test_repo_without_contract_dir_is_skipped(tmp_path: Path) -> None:
    assert _scan(tmp_path, atlan=_atlan("false")) == []
