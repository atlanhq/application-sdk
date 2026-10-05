"""Tests for K027 EntrypointContractClassNameCollision (FND-3140).

The contract ledger keys every entrypoint contract by its bare class name. Two
entrypoints that bind *different* classes under one name therefore share one
ledger identity, and B005/B006 compare each against the other's fields. The
toolkit's bundle mode used to make this the default: every per-entrypoint
``_input.py`` declared ``class AppInputContract``.

Assert on ``finding.discriminator`` (the colliding class name) and the
entrypoint anchor, never on message wording.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks.manifest_contract import scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_RULE = "K027"

_GENERATED_INPUT = (
    "from application_sdk.templates.contracts import ExtractionInput\n"
    "\n"
    "\n"
    "class AppInputContract(ExtractionInput):\n"
    "    {field}: int = 0\n"
)

_APP_HEADER = (
    "from application_sdk.app import App, entrypoint\n"
    "from application_sdk.contracts.base import Output\n"
)


def _write(tmp_path: Path, files: dict[str, str]) -> list[Path]:
    paths: list[Path] = []
    for name, src in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        paths.append(p)
    return paths


def _k027(tmp_path: Path, files: dict[str, str]):
    paths = _write(tmp_path, files)
    return [f for f in scan_all(paths, tmp_path) if f.rule_id == _RULE]


def _bundle_inputs() -> dict[str, str]:
    return {
        "app/__init__.py": "",
        "app/generated/__init__.py": "",
        "app/generated/crawler/__init__.py": "",
        "app/generated/crawler/_input.py": _GENERATED_INPUT.format(field="a"),
        "app/generated/miner/__init__.py": "",
        "app/generated/miner/_input.py": _GENERATED_INPUT.format(field="b"),
    }


def test_rule_metadata() -> None:
    rule = get_rule(_RULE)
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.autofixable is True
    assert rule.category == "contract-toolkit"


def test_two_entrypoints_same_name_different_modules_fires(tmp_path: Path) -> None:
    files = _bundle_inputs()
    files["app/app.py"] = (
        _APP_HEADER + "from app.generated.crawler._input import AppInputContract\n"
        "from app.generated.miner import _input as miner_input\n"
        "\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def crawler(self, input: AppInputContract) -> Output:\n"
        "        return Output()\n"
        "\n"
        "    @entrypoint\n"
        "    async def miner(self, input: miner_input.AppInputContract) -> Output:\n"
        "        return Output()\n"
    )
    findings = _k027(tmp_path, files)
    assert sorted((f.file, f.line) for f in findings) == [
        ("app/app.py", 9),
        ("app/app.py", 13),
    ]
    assert {f.discriminator for f in findings} == {"AppInputContract"}
    assert not any(f.suppressed for f in findings)


def test_subclass_of_a_shared_name_fires(tmp_path: Path) -> None:
    # The ledger resolves MinerInputContract's base by the bare name
    # AppInputContract, which also names the crawler's class, so the miner's
    # ledger entry can carry the crawler's fields.
    files = _bundle_inputs()
    files["app/miner.py"] = (
        "from app.generated.miner._input import AppInputContract as _GeneratedMinerInput\n"
        "\n"
        "\n"
        "class MinerInputContract(_GeneratedMinerInput):\n"
        "    extra: int = 0\n"
    )
    files["app/app.py"] = (
        _APP_HEADER + "from app.generated.crawler._input import AppInputContract\n"
        "from app.miner import MinerInputContract\n"
        "\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def crawler(self, input: AppInputContract) -> Output:\n"
        "        return Output()\n"
        "\n"
        "    @entrypoint\n"
        "    async def miner(self, input: MinerInputContract) -> Output:\n"
        "        return Output()\n"
    )
    findings = _k027(tmp_path, files)
    assert sorted(f.line for f in findings) == [9, 13]
    assert {f.discriminator for f in findings} == {"AppInputContract"}


def _regenerated_bundle_inputs() -> dict[str, str]:
    files = {"app/__init__.py": "", "app/generated/__init__.py": ""}
    for entrypoint, field in (("crawler", "a"), ("miner", "b")):
        cls = f"{entrypoint.capitalize()}AppInputContract"
        files[f"app/generated/{entrypoint}/__init__.py"] = ""
        files[f"app/generated/{entrypoint}/_input.py"] = (
            "from application_sdk.templates.contracts import ExtractionInput\n"
            "\n"
            "\n"
            f"class {cls}(ExtractionInput):\n"
            f"    {field}: int = 0\n"
            "\n"
            "\n"
            f"AppInputContract = {cls}\n"
        )
    return files


_WRAPPER_APP = (
    _APP_HEADER + "from app.crawler import CrawlerInput\n"
    "from app.miner import MinerInput\n"
    "\n"
    "\n"
    "class MyApp(App):\n"
    "    @entrypoint\n"
    "    async def crawler(self, input: CrawlerInput) -> Output:\n"
    "        return Output()\n"
    "\n"
    "    @entrypoint\n"
    "    async def miner(self, input: MinerInput) -> Output:\n"
    "        return Output()\n"
)


def _wrapper(entrypoint: str, base: str) -> str:
    return (
        f"from app.generated.{entrypoint}._input import {base}\n"
        "\n"
        "\n"
        f"class {entrypoint.capitalize()}Input({base}):\n"
        "    extra: int = 0\n"
    )


def test_unique_wrappers_of_the_shared_alias_fire(tmp_path: Path) -> None:
    files = _regenerated_bundle_inputs()
    files["app/crawler.py"] = _wrapper("crawler", "AppInputContract")
    files["app/miner.py"] = _wrapper("miner", "AppInputContract")
    files["app/app.py"] = _WRAPPER_APP
    findings = _k027(tmp_path, files)
    assert sorted((f.file, f.line) for f in findings) == [
        ("app/app.py", 9),
        ("app/app.py", 13),
    ]
    assert {f.discriminator for f in findings} == {"AppInputContract"}


def test_unique_wrappers_of_unique_bases_are_silent(tmp_path: Path) -> None:
    files = _regenerated_bundle_inputs()
    files["app/crawler.py"] = _wrapper("crawler", "CrawlerAppInputContract")
    files["app/miner.py"] = _wrapper("miner", "MinerAppInputContract")
    files["app/app.py"] = _WRAPPER_APP
    assert _k027(tmp_path, files) == []


def test_same_class_reused_by_two_entrypoints_is_silent(tmp_path: Path) -> None:
    files = _bundle_inputs()
    files["app/app.py"] = (
        _APP_HEADER + "from app.generated.crawler._input import AppInputContract\n"
        "\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def crawler(self, input: AppInputContract) -> Output:\n"
        "        return Output()\n"
        "\n"
        "    @entrypoint\n"
        "    async def recrawl(self, input: AppInputContract) -> Output:\n"
        "        return Output()\n"
    )
    assert _k027(tmp_path, files) == []


def test_sdk_class_shared_by_two_entrypoints_is_silent(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "",
        "app/app.py": (
            "from application_sdk.app import App, entrypoint\n"
            "from application_sdk.contracts.base import Input, Output\n"
            "\n"
            "\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def one(self, input: Input) -> Output:\n"
            "        return Output()\n"
            "\n"
            "    @entrypoint\n"
            "    async def two(self, input: Input) -> Output:\n"
            "        return Output()\n"
        ),
    }
    assert _k027(tmp_path, files) == []


def test_aliased_imports_of_two_same_named_classes_fire(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "",
        "app/contracts/__init__.py": "",
        "app/contracts/_input.py": _GENERATED_INPUT.format(field="a"),
        "app/contracts/miner_input.py": _GENERATED_INPUT.format(field="b"),
        "app/app.py": (
            _APP_HEADER + "from app.contracts._input import AppInputContract\n"
            "from app.contracts.miner_input import AppInputContract as MinerInput\n"
            "\n"
            "\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def extract(self, input: AppInputContract) -> Output:\n"
            "        return Output()\n"
            "\n"
            "    @entrypoint\n"
            "    async def miner(self, input: MinerInput) -> Output:\n"
            "        return Output()\n"
        ),
    }
    findings = _k027(tmp_path, files)
    assert len(findings) == 2
    assert {f.discriminator for f in findings} == {"AppInputContract"}


def test_relative_imports_resolve_to_their_own_package(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "",
        "app/one/__init__.py": "",
        "app/one/_input.py": _GENERATED_INPUT.format(field="a"),
        "app/one/activity.py": (
            _APP_HEADER + "from ._input import AppInputContract\n"
            "\n"
            "\n"
            "class OneApp(App):\n"
            "    @entrypoint\n"
            "    async def one(self, input: AppInputContract) -> Output:\n"
            "        return Output()\n"
        ),
        "app/two/__init__.py": "",
        "app/two/_input.py": _GENERATED_INPUT.format(field="b"),
        "app/two/activity.py": (
            _APP_HEADER + "from ._input import AppInputContract\n"
            "\n"
            "\n"
            "class TwoApp(App):\n"
            "    @entrypoint\n"
            "    async def two(self, input: AppInputContract) -> Output:\n"
            "        return Output()\n"
        ),
    }
    findings = _k027(tmp_path, files)
    assert sorted(f.file for f in findings) == [
        "app/one/activity.py",
        "app/two/activity.py",
    ]


def test_regenerated_bundle_alias_imports_still_fire(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "",
        "app/generated/__init__.py": "",
        "app/generated/crawler/__init__.py": "",
        "app/generated/crawler/_input.py": (
            "from application_sdk.templates.contracts import ExtractionInput\n"
            "\n"
            "\n"
            "class CrawlerAppInputContract(ExtractionInput):\n"
            "    a: int = 0\n"
            "\n"
            "\n"
            "AppInputContract = CrawlerAppInputContract\n"
        ),
        "app/generated/miner/__init__.py": "",
        "app/generated/miner/_input.py": (
            "from application_sdk.templates.contracts import ExtractionInput\n"
            "\n"
            "\n"
            "class MinerAppInputContract(ExtractionInput):\n"
            "    b: int = 0\n"
            "\n"
            "\n"
            "AppInputContract = MinerAppInputContract\n"
        ),
        "app/app.py": (
            _APP_HEADER + "from app.generated.crawler import _input as crawler_input\n"
            "from app.generated.miner import _input as miner_input\n"
            "\n"
            "\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def crawler(self, input: crawler_input.AppInputContract) -> Output:\n"
            "        return Output()\n"
            "\n"
            "    @entrypoint\n"
            "    async def miner(self, input: miner_input.AppInputContract) -> Output:\n"
            "        return Output()\n"
        ),
    }
    findings = _k027(tmp_path, files)
    assert len(findings) == 2
    assert {f.discriminator for f in findings} == {"AppInputContract"}


def test_regenerated_bundle_unique_names_are_silent(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "",
        "app/generated/__init__.py": "",
        "app/generated/crawler/__init__.py": "",
        "app/generated/crawler/_input.py": (
            "from application_sdk.templates.contracts import ExtractionInput\n"
            "\n"
            "\n"
            "class CrawlerAppInputContract(ExtractionInput):\n"
            "    a: int = 0\n"
            "\n"
            "\n"
            "AppInputContract = CrawlerAppInputContract\n"
        ),
        "app/generated/miner/__init__.py": "",
        "app/generated/miner/_input.py": (
            "from application_sdk.templates.contracts import ExtractionInput\n"
            "\n"
            "\n"
            "class MinerAppInputContract(ExtractionInput):\n"
            "    b: int = 0\n"
            "\n"
            "\n"
            "AppInputContract = MinerAppInputContract\n"
        ),
        "app/app.py": (
            _APP_HEADER
            + "from app.generated.crawler._input import CrawlerAppInputContract\n"
            "from app.generated.miner._input import MinerAppInputContract\n"
            "\n"
            "\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def crawler(self, input: CrawlerAppInputContract) -> Output:\n"
            "        return Output()\n"
            "\n"
            "    @entrypoint\n"
            "    async def miner(self, input: MinerAppInputContract) -> Output:\n"
            "        return Output()\n"
        ),
    }
    assert _k027(tmp_path, files) == []


def test_output_classes_with_one_name_fire(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "",
        "app/one.py": (
            "from application_sdk.contracts.base import Output\n"
            "\n"
            "\n"
            "class AppOutput(Output):\n"
            "    a: int = 0\n"
        ),
        "app/two.py": (
            "from application_sdk.contracts.base import Output\n"
            "\n"
            "\n"
            "class AppOutput(Output):\n"
            "    b: int = 0\n"
        ),
        "app/app.py": (
            "from application_sdk.app import App, entrypoint\n"
            "from application_sdk.contracts.base import Input\n"
            "from app import one, two\n"
            "\n"
            "\n"
            "class MyApp(App):\n"
            "    @entrypoint\n"
            "    async def one(self, input: Input) -> one.AppOutput:\n"
            "        return one.AppOutput()\n"
            "\n"
            "    @entrypoint\n"
            "    async def two(self, input: Input) -> two.AppOutput:\n"
            "        return two.AppOutput()\n"
        ),
    }
    findings = _k027(tmp_path, files)
    assert {f.discriminator for f in findings} == {"AppOutput"}
    assert len(findings) == 2


def test_suppression_on_entrypoint(tmp_path: Path) -> None:
    files = _bundle_inputs()
    files["app/app.py"] = (
        _APP_HEADER + "from app.generated.crawler._input import AppInputContract\n"
        "from app.generated.miner import _input as miner_input\n"
        "\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    # conformance: ignore[K027] rename tracked separately\n"
        "    async def crawler(self, input: AppInputContract) -> Output:\n"
        "        return Output()\n"
        "\n"
        "    @entrypoint\n"
        "    async def miner(self, input: miner_input.AppInputContract) -> Output:\n"
        "        return Output()\n"
    )
    findings = _k027(tmp_path, files)
    assert sorted(f.suppressed for f in findings) == [False, True]


def test_single_entrypoint_app_is_silent(tmp_path: Path) -> None:
    files = _bundle_inputs()
    files["app/app.py"] = (
        _APP_HEADER + "from app.generated.crawler._input import AppInputContract\n"
        "\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def crawler(self, input: AppInputContract) -> Output:\n"
        "        return Output()\n"
    )
    assert _k027(tmp_path, files) == []


_RUN_ON_TEMPLATE_BODY = (
    "from app.generated.crawler._input import AppInputContract\n"
    "from app.generated.miner import _input as miner_input\n"
    "\n"
    "\n"
    "class Crawler({base}):\n"
    "    async def run(self, input: AppInputContract) -> Output:\n"
    "        return Output()\n"
    "\n"
    "    @entrypoint\n"
    "    async def miner(self, input: miner_input.AppInputContract) -> Output:\n"
    "        return Output()\n"
)


@pytest.mark.parametrize(
    ("header", "base", "extra"),
    [
        (
            "from application_sdk.templates import SqlMetadataExtractor\n",
            "SqlMetadataExtractor",
            {},
        ),
        ("import application_sdk.templates as t\n", "t.SqlApp", {}),
        (
            "from app.base import Base\n",
            "Base",
            {
                "app/base.py": "from application_sdk.templates import SqlApp\n"
                "\n"
                "\n"
                "class Base(SqlApp):\n"
                "    pass\n"
            },
        ),
    ],
    ids=["imported-template", "attribute-template", "in-repo-base-of-template"],
)
def test_run_on_sdk_template_base_is_an_entrypoint(
    tmp_path: Path, header: str, base: str, extra: dict[str, str]
) -> None:
    files = {**_bundle_inputs(), **extra}
    files["app/app.py"] = (
        "from application_sdk.app import entrypoint\n"
        "from application_sdk.contracts.base import Output\n"
        + header
        + _RUN_ON_TEMPLATE_BODY.format(base=base)
    )
    findings = _k027(tmp_path, files)
    assert {f.discriminator for f in findings} == {"AppInputContract"}
    assert len(findings) == 2


def test_run_on_local_class_named_like_a_template_is_silent(tmp_path: Path) -> None:
    files = _bundle_inputs()
    files["app/app.py"] = (
        "from application_sdk.app import entrypoint\n"
        "from application_sdk.contracts.base import Output\n"
        "\n"
        "\n"
        "class SqlMetadataExtractor:\n"
        "    pass\n"
        "\n"
        "\n" + _RUN_ON_TEMPLATE_BODY.format(base="SqlMetadataExtractor")
    )
    assert _k027(tmp_path, files) == []


def test_run_on_a_same_named_non_app_base_is_silent(tmp_path: Path) -> None:
    # app/base.py's Base reaches App, but Utility subclasses the unrelated Base
    # declared in util.py, so Utility.run is not an entrypoint.
    files = _bundle_inputs()
    files["app/base.py"] = (
        "from application_sdk.app import App\n"
        "\n"
        "\n"
        "class Base(App):\n"
        "    pass\n"
    )
    files["app/util.py"] = (
        "from application_sdk.contracts.base import Output\n"
        "from app.generated.miner import _input as miner_input\n"
        "\n"
        "\n"
        "class Base:\n"
        "    pass\n"
        "\n"
        "\n"
        "class Utility(Base):\n"
        "    async def run(self, input: miner_input.AppInputContract) -> Output:\n"
        "        return Output()\n"
    )
    files["app/app.py"] = (
        _APP_HEADER + "from app.generated.crawler._input import AppInputContract\n"
        "\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def crawler(self, input: AppInputContract) -> Output:\n"
        "        return Output()\n"
    )
    assert _k027(tmp_path, files) == []


def test_contract_named_like_its_own_base_is_silent(tmp_path: Path) -> None:
    # One annotation reaching two declarations under one name (the contract and
    # the generated base it shadows) is not two entrypoints colliding.
    files = _bundle_inputs()
    files["app/contracts.py"] = (
        "from app.generated.crawler import _input\n"
        "\n"
        "\n"
        "class AppInputContract(_input.AppInputContract):\n"
        "    extra: int = 0\n"
    )
    files["app/app.py"] = (
        _APP_HEADER + "from app.contracts import AppInputContract\n"
        "\n"
        "\n"
        "class MyApp(App):\n"
        "    @entrypoint\n"
        "    async def crawler(self, input: AppInputContract) -> Output:\n"
        "        return Output()\n"
        "\n"
        "    @entrypoint\n"
        "    async def recrawl(self, input: AppInputContract) -> Output:\n"
        "        return Output()\n"
    )
    assert _k027(tmp_path, files) == []
