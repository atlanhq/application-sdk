"""D016 DirectApiDependency — the api package comes transitively from the SDK."""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.dependency_conformance import (
    discover,
    scan_all,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_INDEX = '\n[[tool.uv.index]]\nname = "pypi"\nurl = "https://pypi.org/simple"\ndefault = true\n'


def _root(deps: list[str], *, name: str = "atlan-demo-app", extra: str = "") -> str:
    body = "".join(f'    "{d}",\n' for d in deps)
    return (
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
        f"dependencies = [\n{body}]\n{extra}{_INDEX}"
    )


def _d016(tmp_path: Path, root_text: str, member_text: str | None = None) -> list:
    (tmp_path / "pyproject.toml").write_text(root_text)
    if member_text is not None:
        member = tmp_path / "api"
        member.mkdir()
        (member / "pyproject.toml").write_text(member_text)
    findings = scan_all(
        discover(tmp_path),
        tmp_path,
        dist_import_map={},
        imported_modules=set(),
        dialect_drivers=set(),
        dialect_names=set(),
        dialect_entry_points={},
    )
    return [f for f in findings if f.rule_id == "D016"]


def test_fires_on_the_api_package_in_root_dependencies(tmp_path: Path) -> None:
    text = _root(
        ["atlan-application-sdk>=3.40.0,<4.0.0", "atlan-application-sdk-api==3.40.0"]
    )
    (finding,) = _d016(tmp_path, text)
    assert finding.file == "pyproject.toml"
    assert finding.line == 6
    assert not finding.suppressed
    assert "api/ member" in finding.message


def test_fires_on_any_spelling_of_the_name(tmp_path: Path) -> None:
    text = _root(["atlan-application-sdk>=3.40,<4", "Atlan_Application_SDK_API>=3.40"])
    assert len(_d016(tmp_path, text)) == 1


def test_silent_when_only_the_sdk_is_declared(tmp_path: Path) -> None:
    assert _d016(tmp_path, _root(["atlan-application-sdk>=3.40.0,<4.0.0"])) == []


def test_silent_on_the_api_member_declaring_it(tmp_path: Path) -> None:
    """The hosted member runs without the SDK, so its direct dependency is right."""
    member = (
        '[project]\nname = "demo-api"\nversion = "0.1.0"\n'
        'dependencies = [\n    "atlan-application-sdk-api==3.40.0",\n]\n'
    )
    root = _root(["atlan-application-sdk>=3.40.0,<4.0.0"])
    assert _d016(tmp_path, root, member) == []


def test_silent_in_optional_dependencies_and_groups(tmp_path: Path) -> None:
    extra = (
        '\n[project.optional-dependencies]\nserve = ["atlan-application-sdk-api"]\n'
        '\n[dependency-groups]\ndev = ["atlan-application-sdk-api"]\n'
    )
    root = _root(["atlan-application-sdk>=3.40.0,<4.0.0"], extra=extra)
    assert _d016(tmp_path, root) == []


def test_silent_on_the_sdk_repo_itself(tmp_path: Path) -> None:
    text = _root(["atlan-application-sdk-api==3.40.0"], name="atlan-application-sdk")
    assert _d016(tmp_path, text) == []


def test_suppressed_inline(tmp_path: Path) -> None:
    text = _root(["atlan-application-sdk>=3.40.0,<4.0.0"]).replace(
        '",\n]\n',
        '",\n    "atlan-application-sdk-api==3.40.0",  '
        "# conformance: ignore[D016] pinned ahead of the SDK for a hotfix\n]\n",
        1,
    )
    (finding,) = _d016(tmp_path, text)
    assert finding.suppressed


def test_d002_defers_to_d016_for_the_root_line() -> None:
    """Once the installed SDK pins the api package, D002 would report the same
    line; it defers so the line is reported once, by the specific rule."""
    text = _root(
        ["atlan-application-sdk>=3.40.0,<4.0.0", "atlan-application-sdk-api==3.40.0"]
    )
    findings = scan_text(
        text,
        "pyproject.toml",
        sdk_managed_packages={"atlan-application-sdk-api", "pydantic"},
        sdk_published_extras=set(),
    )
    assert [f.rule_id for f in findings if f.rule_id == "D002"] == []


def test_d002_still_reports_other_managed_packages() -> None:
    text = _root(["atlan-application-sdk>=3.40.0,<4.0.0", "pydantic>=2"])
    findings = scan_text(
        text,
        "pyproject.toml",
        sdk_managed_packages={"atlan-application-sdk-api", "pydantic"},
        sdk_published_extras=set(),
    )
    assert [f.rule_id for f in findings if f.rule_id == "D002"] == ["D002"]


def test_rule_metadata() -> None:
    rule = get_rule("D016")
    assert rule.scope is RuleScope.APP
    assert rule.tier is EnforcementTier.WARN
    assert rule.autofixable
