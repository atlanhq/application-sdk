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
    assert [f.rule_id for f in coverage_findings(reg)] == ["P065"]
