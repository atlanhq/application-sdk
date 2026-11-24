"""Tests for K032 TaskPoolNotDeployed and K033 UnusedWorkerPool.

``@task(pool="x")`` routes activities to ``atlan-<app>-<deployment>-x``, which
only a dedicated worker pool polls: a ``deploy.pools`` key after the first (the
first is the main worker, on the base queue), a ``deploy.workerPools`` entry, or
a pool with an explicit ``ATLAN_POOL_<POOL>_QUEUE``. K032 flags a ``pool=`` that
names none of them; K033 flags a dedicated pool no task names.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks._ast_common._pkl import pkl_mapping_keys
from conformance.suite.checks.pool_key_alignment import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding

_IMPORT = "from application_sdk.app import App, task\n\n"


def _contract(*keys: str) -> str:
    pools = "".join(
        f'    ["{k}"] = new Pool {{ keda {{ enabled = true }} }}\n' for k in keys
    )
    return (
        'amends "@app-contract-toolkit/App.pkl"\n'
        "\n"
        "deploy = new DeployConfig {\n"
        "  pools {\n"
        f"{pools}"
        "  }\n"
        "}\n"
    )


def _app(*decorators: str, prelude: str = "") -> str:
    methods = "".join(
        f"    {d}\n    async def t{i}(self, input: In) -> Out:\n        ...\n\n"
        for i, d in enumerate(decorators)
    )
    return f"{_IMPORT}{prelude}class MyApp(App):\n{methods}"


def _scan(
    tmp_path: Path,
    *,
    contract: str | None = None,
    atlan: str | None = None,
    code: str | None = None,
) -> list[Finding]:
    if contract is not None:
        path = tmp_path / "contract" / "app.pkl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contract, encoding="utf-8")
    if atlan is not None:
        (tmp_path / "atlan.yaml").write_text(atlan, encoding="utf-8")
    if code is not None:
        (tmp_path / "app").mkdir(exist_ok=True)
        (tmp_path / "app" / "app.py").write_text(code, encoding="utf-8")
    return scan_all(discover(tmp_path), tmp_path)


def _active(findings: list[Finding], rule: str) -> list[Finding]:
    return [f for f in findings if f.rule_id == rule and not f.suppressed]


def test_rule_metadata() -> None:
    k032, k033 = get_rule("K032"), get_rule("K033")
    assert (k032.name, k032.tier) == ("TaskPoolNotDeployed", EnforcementTier.BLOCK)
    assert (k033.name, k033.tier) == ("UnusedWorkerPool", EnforcementTier.WARN)
    for rule in (k032, k033):
        assert rule.scope is RuleScope.APP
        assert rule.category == "contract-toolkit"


# -- K032 ---------------------------------------------------------------------


def test_typo_against_single_pool_contract(tmp_path: Path) -> None:
    """The issue's fixture: a misspelt pool against ``pools { ["heavy"] }``."""
    findings = _scan(
        tmp_path,
        contract=_contract("heavy"),
        code=_app('@task(pool="haevy")'),
    )
    k032 = _active(findings, "K032")
    assert [(f.file, f.line) for f in k032] == [("app/app.py", 4)]
    assert "names no worker pool this app deploys" in k032[0].message
    assert "declares no dedicated worker pool" in k032[0].message
    assert _active(findings, "K033") == []


def test_typo_suggests_the_dedicated_pool(tmp_path: Path) -> None:
    findings = _scan(
        tmp_path,
        contract=_contract("default", "heavy"),
        code=_app('@task(pool="haevy")'),
    )
    [k032] = _active(findings, "K032")
    assert 'Did you mean "heavy"?' in k032.message
    # The typo leaves "heavy" with no task routed to it.
    [k033] = _active(findings, "K033")
    assert (k033.file, k033.line) == ("contract/app.pkl", 6)


def test_main_worker_key_is_not_a_routable_pool(tmp_path: Path) -> None:
    findings = _scan(
        tmp_path,
        contract=_contract("default", "heavy"),
        code=_app('@task(pool="default")', '@task(pool="heavy")'),
    )
    [k032] = _active(findings, "K032")
    assert "main worker pool" in k032.message
    assert _active(findings, "K033") == []


def test_matching_pools_are_clean(tmp_path: Path) -> None:
    findings = _scan(
        tmp_path,
        contract=_contract("default", "heavy", "cold-tier"),
        code=_app(
            "@task()",
            '@task(pool="heavy")',
            '@task(timeout_seconds=5, pool="cold-tier")',
        ),
    )
    assert findings == []


def test_module_constant_is_resolved(tmp_path: Path) -> None:
    code = _app("@task(pool=PROFILER_POOL)", prelude='PROFILER_POOL = "profiler"\n\n')
    assert _scan(tmp_path, contract=_contract("default", "profiler"), code=code) == []
    bad = _app("@task(pool=PROFILER_POOL)", prelude='PROFILER_POOL = "profiller"\n\n')
    findings = _scan(tmp_path, contract=_contract("default", "profiler"), code=bad)
    assert len(_active(findings, "K032")) == 1


def test_unresolvable_pool_is_skipped_and_silences_k033(tmp_path: Path) -> None:
    code = _app("@task(pool=pick())", '@task(pool="nope")')
    findings = _scan(tmp_path, contract=_contract("default", "heavy"), code=code)
    assert [f.line for f in _active(findings, "K032")] == [8]
    assert _active(findings, "K033") == []


def test_pool_none_is_not_a_use(tmp_path: Path) -> None:
    findings = _scan(
        tmp_path, contract=_contract("default"), code=_app("@task(pool=None)")
    )
    assert findings == []


def test_other_task_decorators_are_ignored(tmp_path: Path) -> None:
    code = (
        "from airflow.decorators import task\n\n@task(pool='etl')\ndef f():\n    ...\n"
    )
    assert _scan(tmp_path, contract=_contract("default"), code=code) == []


def test_module_attribute_decorator(tmp_path: Path) -> None:
    code = (
        "import application_sdk.app as sdk_app\n\n"
        "class MyApp(sdk_app.App):\n"
        '    @sdk_app.task(pool="hevy")\n'
        "    async def t(self, input: In) -> Out:\n"
        "        ...\n"
    )
    findings = _scan(tmp_path, contract=_contract("default", "heavy"), code=code)
    assert [f.line for f in _active(findings, "K032")] == [4]


def test_k032_inline_suppression(tmp_path: Path) -> None:
    code = _app(
        '@task(pool="ghost")  # conformance: ignore[K032] queue owned elsewhere'
    )
    findings = _scan(tmp_path, contract=_contract("default"), code=code)
    assert _active(findings, "K032") == []
    assert [f.suppressed for f in findings] == [True]


def test_pool_override_env_makes_a_pool_routable(tmp_path: Path) -> None:
    atlan = "deploy:\n  env:\n    ATLAN_POOL_COLD_TIER_QUEUE: some-queue\n"
    findings = _scan(tmp_path, atlan=atlan, code=_app('@task(pool="cold-tier")'))
    assert findings == []


def test_tests_are_not_scanned(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        _app('@task(pool="ghost")'), encoding="utf-8"
    )
    assert _scan(tmp_path, contract=_contract("default")) == []


# -- atlan.yaml without contract pools ------------------------------------------


_ATLAN_POOLS = (
    "name: example\n"
    "deploy:\n"
    "  workerPools:\n"
    "  - name: heavy\n"
    "    keda:\n"
    "      enabled: true\n"
    "  -   keda: {}\n"
    "      name: 'lineage'  # second item, name not first\n"
    "pools:\n"
    "  default:\n"
    "    keda:\n"
    "      enabled: true\n"
    "  heavy:\n"
    "    keda:\n"
    "      enabled: true\n"
    "  lineage: {}\n"
)


def test_atlan_yaml_pools_and_worker_pools(tmp_path: Path) -> None:
    code = _app('@task(pool="heavy")', '@task(pool="lineage")', '@task(pool="name")')
    findings = _scan(tmp_path, atlan=_ATLAN_POOLS, code=code)
    assert [f.line for f in _active(findings, "K032")] == [12]
    assert _active(findings, "K033") == []


def test_atlan_yaml_unused_pool(tmp_path: Path) -> None:
    findings = _scan(tmp_path, atlan=_ATLAN_POOLS, code=_app('@task(pool="heavy")'))
    [k033] = _active(findings, "K033")
    assert (k033.file, k033.line) == ("atlan.yaml", 16)
    assert "delete it from pools: in atlan.yaml" in k033.message


def test_worker_pools_without_pools_map(tmp_path: Path) -> None:
    """A hand-written atlan.yaml can deploy pools through workerPools alone."""
    atlan = "deploy:\n  workerPools:\n    - name: tier-large\n"
    findings = _scan(tmp_path, atlan=atlan, code=_app('@task(pool="tier-large")'))
    assert findings == []


# -- K033 ---------------------------------------------------------------------


def test_unused_dedicated_pool_in_contract(tmp_path: Path) -> None:
    findings = _scan(
        tmp_path,
        contract=_contract("default", "heavy", "cold"),
        code=_app('@task(pool="heavy")'),
    )
    [k033] = _active(findings, "K033")
    assert (k033.file, k033.line) == ("contract/app.pkl", 7)
    assert 'Worker pool "cold" is declared' in k033.message
    assert "regenerate atlan.yaml" in k033.message


def test_contract_wins_over_atlan_yaml_for_declarations(tmp_path: Path) -> None:
    findings = _scan(
        tmp_path,
        contract=_contract("default", "heavy"),
        atlan="pools:\n  default: {}\n  stale: {}\n",
        code=_app('@task(pool="heavy")'),
    )
    assert findings == []


def test_k033_pkl_suppression(tmp_path: Path) -> None:
    contract = _contract("default", "spare").replace(
        '    ["spare"]',
        "    // conformance: ignore[K033] routed by the app's own dispatcher\n"
        '    ["spare"]',
    )
    findings = _scan(tmp_path, contract=contract, code=_app("@task()"))
    assert _active(findings, "K033") == []
    assert [f.suppressed for f in findings] == [True]


def test_no_app_config_discovers_nothing(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(_app('@task(pool="x")'), encoding="utf-8")
    assert discover(tmp_path) == []


# -- pkl_mapping_keys -----------------------------------------------------------


def test_pkl_mapping_keys_ignores_comments_strings_and_nested_maps() -> None:
    text = (
        "deploy = new DeployConfig {\n"
        "  pools = new Mapping<String, Pool> {\n"
        '    // ["commented"] = new Pool {}\n'
        '    ["main"] = new Pool {\n'
        '      env { ["NOT_A_POOL"] = "x" }\n'
        "    }\n"
        '    ["side"] { keda { enabled = true } }\n'
        "  }\n"
        "}\n"
        'other { pools { ["elsewhere"] = 1 } }\n'
        'note = "deploy { pools { [\\"fake\\"] = 1 } }"\n'
    )
    assert pkl_mapping_keys(text, "pools", within="deploy") == [
        ("main", 4),
        ("side", 7),
    ]
