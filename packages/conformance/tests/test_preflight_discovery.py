from pathlib import Path

from conformance.suite.checks.preflight import (
    _metadata_parity,
    _untyped_failure,
    _warning_log,
)
from conformance.suite.checks.preflight._common import (
    build_registry,
    find_preflight_check_sites,
)


def registry(tmp_path: Path, files: dict[str, str]):
    paths = []
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        paths.append(path)
    return build_registry(paths, tmp_path)


def test_module_callback_and_local_alias(tmp_path):
    reg = registry(
        tmp_path,
        {
            "app/crawler/handler.py": "async def probe(input, ctx):\n    return None\npreflight_check = probe\n"
        },
    )
    assert [f.name for _, f in find_preflight_check_sites(reg)] == ["probe"]


def test_imported_handler_alias(tmp_path):
    reg = registry(
        tmp_path,
        {
            "base.py": "from application_sdk.handler import Handler as Root\nclass Base(Root):\n    pass\n",
            "handler.py": "from base import Base as Parent\nclass Concrete(Parent):\n    async def preflight_check(self, input):\n        return None\n",
        },
    )
    assert len(find_preflight_check_sites(reg)) == 1


def test_reachable_warning_only(tmp_path):
    reg = registry(
        tmp_path,
        {
            "app/crawler/handler.py": "async def probe():\n    logger.warning('failed')\nasync def unused():\n    logger.warning('unrelated')\nasync def preflight_check(input, ctx):\n    await probe()\n"
        },
    )
    assert len(_warning_log.scan(reg)) == 1


def test_local_failed_kwargs(tmp_path):
    reg = registry(
        tmp_path,
        {
            "handler.py": "from application_sdk.handler.contracts import PreflightCheck\ndef make():\n    failed = False\n    values = {'passed': failed, 'error': None}\n    return PreflightCheck(name='probe', **values)\n"
        },
    )
    assert len(_untyped_failure.scan(reg)) == 1


def test_unknown_mutated_kwargs_not_proven(tmp_path):
    reg = registry(
        tmp_path,
        {
            "handler.py": "from application_sdk.handler.contracts import PreflightCheck\ndef make(error):\n    values = {'passed': False}\n    values.update(error)\n    return PreflightCheck(**values)\n"
        },
    )
    assert _untyped_failure.scan(reg) == []


def test_selected_entrypoint_contract(tmp_path):
    reg = registry(
        tmp_path,
        {
            "app.py": "from application_sdk.app import App, entrypoint\nclass CrawlInput:\n    crawler_only: str\nclass MineInput:\n    miner_only: str\nclass Example(App):\n    @entrypoint(name='crawler')\n    async def crawl(self, input: CrawlInput): pass\n    @entrypoint(name='miner')\n    async def mine(self, input: MineInput): pass\n",
            "app/miner/handler.py": "async def preflight_check(input, ctx):\n    return input.metadata.get('crawler_only')\n",
        },
    )
    assert len(_metadata_parity.scan(reg)) == 1


def test_unrelated_handler_base_is_not_sdk(tmp_path):
    reg = registry(
        tmp_path,
        {
            "other.py": "class Handler: pass\nclass Other(Handler):\n    async def preflight_check(self, value): pass\n"
        },
    )
    assert find_preflight_check_sites(reg) == []


def test_parse_failure_is_incomplete(tmp_path):
    from conformance.suite.checks.preflight._common import coverage_findings

    reg = registry(tmp_path, {"handler.py": "async def preflight_check(:"})
    assert [f.rule_id for f in coverage_findings(reg)] == ["F019"]


def test_module_level_contract_alias_resolves(tmp_path):
    """An entrypoint input declared under a module-level alias stays analysable.

    ``app/contracts.py`` re-binds the pkl-generated contract to a domain name
    and the entrypoint annotates that name. Until the registry recorded
    aliases, the annotated name was absent from ``by_name`` and F019 reported
    the contract as unanalysable even though its class was in the same scan.
    """
    from conformance.suite.checks.preflight._common import (
        collect_entrypoint_input_contract_names,
        coverage_findings,
    )

    reg = registry(
        tmp_path,
        {
            "app/generated/_input.py": "class AppInputContract:\n    include_database_regex: str\n",
            "app/contracts.py": "from app.generated._input import AppInputContract\nOpenAPIConnectorInput = AppInputContract\n",
            "app/connector.py": "from application_sdk.app import App\nfrom app.contracts import OpenAPIConnectorInput\nclass Connector(App):\n    async def run(self, input: OpenAPIConnectorInput): pass\n",
            "app/handler.py": "async def preflight_check(input, ctx):\n    return input.metadata.get('include_database_regex')\n",
        },
    )

    assert collect_entrypoint_input_contract_names(reg) == frozenset(
        {"OpenAPIConnectorInput"}
    )
    assert coverage_findings(reg) == []
    assert reg.by_name["OpenAPIConnectorInput"] is reg.by_name["AppInputContract"]
    assert _metadata_parity.scan(reg) == []


def test_contract_alias_chain_and_annotated_form(tmp_path):
    """A cross-module re-export chain and a ``TypeAlias`` rebinding both resolve."""
    reg = registry(
        tmp_path,
        {
            "generated.py": "class AppInputContract:\n    pass\n",
            "mid.py": "from typing import TypeAlias\nfrom generated import AppInputContract\nMidInput: TypeAlias = AppInputContract\n",
            "public.py": "from mid import MidInput\nPublicInput = MidInput\n",
        },
    )
    assert reg.by_name["MidInput"] is reg.by_name["AppInputContract"]
    assert reg.by_name["PublicInput"] is reg.by_name["AppInputContract"]


def test_alias_rebinding_shapes_all_reach_the_class(tmp_path):
    """Every right-hand side and target shape a rebinding can take resolves.

    ``AttrAlias`` pins the subtlest of them: a qualified ``g.Target`` is an
    alias of ``Target``, because the registry is keyed on bare names and the
    module qualifier carries no information it can use.
    """
    reg = registry(
        tmp_path,
        {
            "g.py": "class Target:\n    pass\n",
            "a.py": (
                "import g\n"
                "from g import Target as _T\n"
                "AttrAlias = g.Target\n"
                "ImportAlias = _T\n"
                "A = B = Target\n"
            ),
        },
    )
    target = reg.by_name["Target"]
    assert target.node.name == "Target"
    assert target.file == "g.py"
    for name in ("AttrAlias", "ImportAlias", "A", "B"):
        assert reg.by_name[name] is target, name


def test_self_assignment_does_not_poison_a_real_alias(tmp_path):
    """``X = X`` must not claim the registry slot a real ``X = Class`` needs.

    Alias targets are recorded first-wins across files, so a self-assignment
    that got recorded would take the slot and resolve to itself — dropping the
    binding in the other file, which is the one that names a class.
    """
    reg = registry(
        tmp_path,
        {
            "real.py": "class RealClass:\n    pass\n",
            "a_shim.py": "from real import RealClass\nExported = Exported\n",
            "b_alias.py": "from real import RealClass\nExported = RealClass\n",
        },
    )
    assert reg.by_name["Exported"] is reg.by_name["RealClass"]


def test_non_class_and_cyclic_aliases_are_not_registered(tmp_path):
    """Only a chain landing on a scanned class registers; a cycle terminates."""
    reg = registry(
        tmp_path,
        {
            "mod.py": "VALUE = 3\nAlias = VALUE\nLeft = Right\nRight = Left\nBoxed = list[int]\n"
        },
    )
    assert reg.by_name == {}


def test_alias_never_shadows_a_real_class(tmp_path):
    """A class definition always wins over an alias binding the same name."""
    reg = registry(
        tmp_path,
        {
            "real.py": "class Target:\n    pass\nclass Shadow:\n    pass\n",
            "alias.py": "from real import Target\nShadow = Target\n",
        },
    )
    assert reg.by_name["Shadow"].node.name == "Shadow"
