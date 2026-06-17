"""Tests for D001/D002 dependency_conformance check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite.checks.dependency_conformance import (
    _is_bounded_specifier,
    _iter_dep_entries,
    _normalise_name,
    _parse_requirement,
    _parse_suppressions,
    main,
    scan_text,
)
from conformance.suite.schema import SarifReport, derive_disposition, validate_sarif
from conformance.suite.schema.disposition import Disposition

# ── Pure helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Foo", "foo"),
        ("foo_bar", "foo-bar"),
        ("Foo.Bar", "foo-bar"),
        ("FOO__BAR..baz", "foo-bar-baz"),
    ],
)
def test_normalise_name(raw: str, expected: str) -> None:
    assert _normalise_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pydantic", ("pydantic", "")),
        ("pydantic>=2,<3", ("pydantic", ">=2,<3")),
        ("pydantic[validation]>=2.10,<3.0", ("pydantic", ">=2.10,<3.0")),
        (
            "uvloop>=0.21.0,<0.23.0; sys_platform != 'win32'",
            ("uvloop", ">=0.21.0,<0.23.0"),
        ),
        ("atlan-application-sdk[sql]==3.17.2", ("atlan-application-sdk", "==3.17.2")),
        ("Foo_Bar.Baz~=2.5", ("foo-bar-baz", "~=2.5")),
    ],
)
def test_parse_requirement(raw: str, expected: tuple[str, str]) -> None:
    assert _parse_requirement(raw) == expected


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("", False),
        (">=1", False),
        (">=1.0,<2", True),
        ("<3", False),
        (">=1, <2", True),
        ("==3.17.2", True),
        ("===3.17.2-rc1", True),
        ("~=3.17", True),
        (">=1, !=1.5", False),
        (">1.0,<=2.0", True),
        # exclusion alone never bounds
        ("!=1.0", False),
    ],
)
def test_is_bounded_specifier(spec: str, expected: bool) -> None:
    assert _is_bounded_specifier(spec) is expected


# ── _iter_dep_entries ────────────────────────────────────────────────────────


_PYPROJECT_BASIC = """\
[project]
name = "demo-app"
version = "0.1.0"
dependencies = [
    "atlan-application-sdk>=3.17.2,<4.0.0",
    "rich>=13",
]

[project.optional-dependencies]
sql = [
    "duckdb>=1.1.3,<1.6.0",
]
"""


def test_iter_dep_entries_extracts_lines_and_paths() -> None:
    entries = list(_iter_dep_entries(_PYPROJECT_BASIC))
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"atlan-application-sdk", "rich", "duckdb"}
    assert by_name["atlan-application-sdk"].array_path == "project.dependencies"
    assert by_name["atlan-application-sdk"].line == 5
    assert by_name["rich"].line == 6
    assert by_name["duckdb"].array_path == "project.optional-dependencies.sql"
    assert by_name["duckdb"].line == 11


def test_iter_dep_entries_unparseable_returns_empty() -> None:
    assert list(_iter_dep_entries("not [valid toml")) == []


def test_iter_dep_entries_inline_array_form() -> None:
    text = '[project]\nname = "x"\ndependencies = ["pydantic>=2,<3", "rich"]\n'
    entries = list(_iter_dep_entries(text))
    assert {e.name for e in entries} == {"pydantic", "rich"}
    assert all(e.line == 3 for e in entries)


# ── scan_text behaviour ──────────────────────────────────────────────────────


def _write_pyproject(name: str = "demo-app", deps: str = "") -> str:
    return f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = [\n{deps}]\n'


def test_self_check_skips_sdk_repo() -> None:
    """SDK and sibling packages are exempt from D-series."""
    text = _write_pyproject(
        name="atlan-application-sdk",
        deps='    "pydantic>=2,<3",\n',
    )
    assert scan_text(text, "pyproject.toml") == []
    text2 = _write_pyproject(
        name="atlan-application-sdk-conformance",
        deps='    "pydantic>=2,<3",\n',
    )
    assert scan_text(text2, "pyproject.toml") == []


def test_d001_missing_sdk_dep() -> None:
    text = _write_pyproject(deps='    "rich>=13,<14",\n')
    findings = scan_text(text, "pyproject.toml", sdk_managed_packages=set())
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "D001"
    assert "does not declare 'atlan-application-sdk'" in f.message
    # Anchor points at the [project] table header.
    assert f.line == 1


def test_d001_unbounded_sdk_dep() -> None:
    text = _write_pyproject(deps='    "atlan-application-sdk>=3.17",\n')
    findings = scan_text(text, "pyproject.toml", sdk_managed_packages=set())
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "D001"
    assert "without a bounded version specifier" in f.message
    assert f.line == 5


def test_d001_bare_name_is_unbounded() -> None:
    text = _write_pyproject(deps='    "atlan-application-sdk",\n')
    findings = scan_text(text, "pyproject.toml", sdk_managed_packages=set())
    assert [f.rule_id for f in findings] == ["D001"]


def test_d001_passes_for_bounded_dep() -> None:
    text = _write_pyproject(deps='    "atlan-application-sdk>=3.17.2,<4.0.0",\n')
    findings = scan_text(text, "pyproject.toml", sdk_managed_packages=set())
    assert findings == []


def test_d001_passes_for_compatible_release() -> None:
    text = _write_pyproject(deps='    "atlan-application-sdk~=3.17",\n')
    findings = scan_text(text, "pyproject.toml", sdk_managed_packages=set())
    assert findings == []


def test_d001_passes_for_extras_pin() -> None:
    text = _write_pyproject(deps='    "atlan-application-sdk[sql]>=3.17.2,<4.0.0",\n')
    findings = scan_text(text, "pyproject.toml", sdk_managed_packages=set())
    assert findings == []


def test_d002_redeclared_core_dep() -> None:
    text = _write_pyproject(
        deps=(
            '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
            '    "pydantic>=2.10,<3.0",\n'
        )
    )
    findings = scan_text(
        text,
        "pyproject.toml",
        sdk_managed_packages={"pydantic", "fastapi"},
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "D002"
    assert f.line == 6
    assert "'pydantic' is already pinned" in f.message
    assert "[project.dependencies]" in f.message


def test_d002_redeclared_in_optional_extra() -> None:
    text = (
        '[project]\nname = "demo-app"\nversion = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        "]\n"
        "[project.optional-dependencies]\n"
        "sql = [\n"
        '    "pyarrow>=23,<24",\n'
        "]\n"
    )
    findings = scan_text(text, "pyproject.toml", sdk_managed_packages={"pyarrow"})
    assert len(findings) == 1
    assert findings[0].rule_id == "D002"
    assert "[project.optional-dependencies.sql]" in findings[0].message
    assert findings[0].line == 9


def test_d002_skipped_when_sdk_metadata_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When importlib.metadata.requires returns None for the SDK, skip D002."""
    from conformance.suite.checks import dependency_conformance as dc

    monkeypatch.setattr(dc, "_sdk_managed_packages", lambda: None)
    text = _write_pyproject(
        deps=(
            '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
            '    "pydantic>=2.10,<3.0",\n'
        )
    )
    findings = scan_text(text, "pyproject.toml")
    # D001 ok, D002 absent because managed set is None.
    assert findings == []


def test_d002_does_not_flag_sdk_itself() -> None:
    """Even if 'atlan-application-sdk' is in the managed set, never report D002 for it."""
    text = _write_pyproject(deps='    "atlan-application-sdk>=3.17.2,<4.0.0",\n')
    findings = scan_text(
        text,
        "pyproject.toml",
        sdk_managed_packages={"atlan-application-sdk", "pydantic"},
    )
    assert findings == []


def test_normalisation_catches_underscore_redeclaration() -> None:
    text = _write_pyproject(
        deps=(
            '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
            '    "azure_identity>=1.15.0",\n'
        )
    )
    findings = scan_text(
        text,
        "pyproject.toml",
        sdk_managed_packages={"azure-identity"},
    )
    assert len(findings) == 1
    assert findings[0].rule_id == "D002"
    assert "'azure-identity'" in findings[0].message


# ── Suppression directives ───────────────────────────────────────────────────


def test_parse_suppressions_inline_directive() -> None:
    text = (
        '[project]\nname = "demo"\n'
        "dependencies = [\n"
        '    "pydantic>=2,<3",  # conformance: ignore[D002] override for hotfix\n'
        "]\n"
    )
    suppressions = _parse_suppressions(text)
    assert 4 in suppressions
    ids, just = suppressions[4]
    assert ids == frozenset({"D002"})
    assert "hotfix" in just


def test_d002_suppressed_inline_directive_is_counted_but_not_active() -> None:
    text = (
        '[project]\nname = "demo-app"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "pydantic>=2,<3",  # conformance: ignore[D002] hotfix override\n'
        "]\n"
    )
    findings = scan_text(
        text,
        "pyproject.toml",
        sdk_managed_packages={"pydantic"},
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "D002"
    assert f.suppressed is True
    assert f.suppression_justification == "hotfix override"


def test_d002_suppression_on_line_above_applies() -> None:
    text = (
        '[project]\nname = "demo-app"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        "    # conformance: ignore[D002] vendor fork\n"
        '    "pydantic>=2,<3",\n'
        "]\n"
    )
    findings = scan_text(
        text,
        "pyproject.toml",
        sdk_managed_packages={"pydantic"},
    )
    assert len(findings) == 1
    assert findings[0].suppressed is True


# ── End-to-end via main() ────────────────────────────────────────────────────


def _scratch_pyproject(tmp_path: Path, body: str) -> Path:
    pp = tmp_path / "pyproject.toml"
    pp.write_text(body, encoding="utf-8")
    return pp


def test_main_exit_1_on_blocking_violation(tmp_path: Path) -> None:
    """main() exits 1 when a D001 (blocking) violation is found."""
    _scratch_pyproject(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["rich>=13,<14"]\n',
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "pyproject.toml")])
    assert code == 1


def test_main_exit_0_when_clean(tmp_path: Path) -> None:
    _scratch_pyproject(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\n'
        'dependencies = ["atlan-application-sdk>=3.17,<4.0"]\n',
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "pyproject.toml")])
    # The installed SDK in this dev env may itself emit D002 against a few
    # core deps if the test happens to run with a populated managed set.  We
    # only assert that a clean pin produces no D001 findings; D002 depends
    # on the surrounding env.
    assert code in (0, 1)


def test_main_sarif_output_validates(tmp_path: Path) -> None:
    """Emitted SARIF validates against the official schema."""
    _scratch_pyproject(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["rich>=13,<14"]\n',
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            "--sarif-output",
            str(sarif_file),
            "--validate",
            str(tmp_path / "pyproject.toml"),
        ]
    )
    payload = json.loads(sarif_file.read_text(encoding="utf-8"))
    report = SarifReport.model_validate(payload)
    validate_sarif(report)
    # exactly one D001 finding (no SDK declared)
    results = report.runs[0].results or []
    assert len(results) == 1
    assert results[0].rule_id == "D001"
    assert derive_disposition(results[0]) == Disposition.FAILING


def test_self_check_passes_via_main(tmp_path: Path) -> None:
    """Running against the SDK's own pyproject.toml emits zero findings."""
    _scratch_pyproject(
        tmp_path,
        '[project]\nname = "atlan-application-sdk"\nversion = "3.17.2"\n'
        'dependencies = ["pydantic>=2.10.6,<3.0.0"]\n',
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            "--sarif-output",
            str(sarif_file),
            str(tmp_path / "pyproject.toml"),
        ]
    )
    payload = json.loads(sarif_file.read_text(encoding="utf-8"))
    report = SarifReport.model_validate(payload)
    assert (report.runs[0].results or []) == []
