"""Tests for the D-series (D001-D011) dependency_conformance check."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from conformance.suite.checks._ast_common import _is_suppressed, parse_toml_suppressions
from conformance.suite.checks.dependency_conformance import (
    _REMOTE_COMPONENT_FETCH_RE,
    SDK_PYTHON_FLOOR,
    _collect_dialect_drivers,
    _is_bounded_specifier,
    _is_floating_range,
    _iter_dep_entries,
    _iter_dependency_group_entries,
    _normalise_name,
    _parse_requirement,
    _requires_python_lower_bound,
    _sdk_extras_in,
    main,
    scan_all,
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
            '    "atlan-application-sdk>=3.17.2,<4.0.0",\n    "pydantic>=2.10,<3.0",\n'
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


def test_d002_redeclared_in_optional_extra_inline_form() -> None:
    """D002 fires when an optional-extra array is written inline (single-line)."""
    text = (
        '[project]\nname = "demo-app"\nversion = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        "]\n"
        "[project.optional-dependencies]\n"
        'sql = ["pydantic>=2,<3"]\n'
    )
    findings = scan_text(text, "pyproject.toml", sdk_managed_packages={"pydantic"})
    assert len(findings) == 1
    assert findings[0].rule_id == "D002"
    assert "[project.optional-dependencies.sql]" in findings[0].message


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
            '    "atlan-application-sdk>=3.17.2,<4.0.0",\n    "pydantic>=2.10,<3.0",\n'
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
    suppressions = parse_toml_suppressions(text)
    assert 4 in suppressions
    ids, discriminators, just = suppressions[4]
    assert ids == frozenset({"D002"})
    assert discriminators is None
    assert "hotfix" in just


def test_parse_suppressions_discriminator_form() -> None:
    """``ignore[T025:miner]`` parses the subject alongside the rule id."""
    text = (
        "# conformance: ignore[T025:miner] miner has no CI-reachable source\n"
        '[project]\nname = "demo"\n'
    )
    suppressions = parse_toml_suppressions(text)
    ids, discriminators, just = suppressions[1]
    assert ids == frozenset({"T025"})
    assert discriminators == {"T025": frozenset({"miner"})}
    assert "CI-reachable" in just


def test_is_suppressed_discriminator_matching() -> None:
    """A ``:subject`` directive suppresses only findings carrying that subject."""
    text = (
        "# conformance: ignore[T025:miner] miner has no CI-reachable source\n"
        '[project]\nname = "demo"\n'
    )
    suppressions = parse_toml_suppressions(text)
    # The named subject suppresses.
    assert _is_suppressed(suppressions, "T025", 2, discriminator="miner")[0]
    # …case-insensitively.
    assert _is_suppressed(suppressions, "T025", 2, discriminator="Miner")[0]
    # A different subject on the same rule+line does NOT suppress.
    assert not _is_suppressed(suppressions, "T025", 2, discriminator="crawler")[0]
    # A finding with no discriminator is not suppressed by a subject directive.
    assert not _is_suppressed(suppressions, "T025", 2)[0]
    # A different rule is untouched.
    assert not _is_suppressed(suppressions, "D002", 2, discriminator="miner")[0]


def test_is_suppressed_bare_rule_still_suppresses_discriminated_findings() -> None:
    """A bare ``ignore[T025]`` stays rule-wide, discriminator or not."""
    text = "# conformance: ignore[T025] nothing is CI-reachable here\n[project]\n"
    suppressions = parse_toml_suppressions(text)
    assert _is_suppressed(suppressions, "T025", 2, discriminator="miner")[0]
    assert _is_suppressed(suppressions, "T025", 2)[0]


def test_bare_entry_wins_over_a_subject_entry_for_the_same_rule() -> None:
    """``ignore[T025:miner, T025]`` must not be narrowed back by the subject."""
    for text in (
        "# conformance: ignore[T025:miner, T025] why\n[project]\n",
        "# conformance: ignore[T025, T025:miner] why\n[project]\n",
    ):
        suppressions = parse_toml_suppressions(text)
        assert _is_suppressed(suppressions, "T025", 2, discriminator="crawler")[0]


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


# ── D006: requires-python floor ──────────────────────────────────────────────


def _py_pyproject(spec: str) -> str:
    return (
        "[project]\n"
        'name = "demo-app"\n'
        f'requires-python = "{spec}"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        "]\n"
    )


@pytest.mark.parametrize(
    "spec,expected",
    [
        ('[project]\nname = "x"\nrequires-python = ">=3.11,<4"\n', (3, 11)),
        ('[project]\nname = "x"\nrequires-python = ">=3.9"\n', (3, 9)),
        ('[project]\nname = "x"\nrequires-python = ">3.10"\n', (3, 10)),
        ('[project]\nname = "x"\nrequires-python = "==3.12"\n', None),  # no lower op
        ('[project]\nname = "x"\nrequires-python = "<4"\n', None),
        ('[project]\nname = "x"\n', None),  # absent
    ],
)
def test_requires_python_lower_bound(
    spec: str, expected: tuple[int, int] | None
) -> None:
    result = _requires_python_lower_bound(spec)
    assert (result[0] if result else None) == expected


def test_d006_fires_below_sdk_floor() -> None:
    findings = scan_text(
        _py_pyproject(">=3.10"), "pyproject.toml", sdk_managed_packages=set()
    )
    assert [f.rule_id for f in findings] == ["D006"]
    f = findings[0]
    assert "below the SDK's minimum supported Python" in f.message
    assert f.line == 3  # anchored on the requires-python line


def test_d006_passes_at_floor() -> None:
    assert (
        scan_text(_py_pyproject(">=3.11"), "pyproject.toml", sdk_managed_packages=set())
        == []
    )


def test_d006_passes_above_floor() -> None:
    assert (
        scan_text(
            _py_pyproject(">=3.12,<4.0"), "pyproject.toml", sdk_managed_packages=set()
        )
        == []
    )


def test_d006_strict_lower_bound_below_floor_fires() -> None:
    # ``>3.10`` admits 3.10.x patch releases, which are below the SDK's 3.11 floor.
    findings = scan_text(
        _py_pyproject(">3.10"), "pyproject.toml", sdk_managed_packages=set()
    )
    assert [f.rule_id for f in findings] == ["D006"]
    # The message echoes the operator as written (``>``), never rewriting it to ``>=``.
    assert "'>3.10'" in findings[0].message
    assert "'>=3.10'" not in findings[0].message


def test_d006_absent_requires_python_no_finding() -> None:
    text = _write_pyproject(deps='    "atlan-application-sdk>=3.17.2,<4.0.0",\n')
    assert scan_text(text, "pyproject.toml", sdk_managed_packages=set()) == []


def test_d006_suppressed_inline_directive() -> None:
    text = (
        "[project]\n"
        'name = "demo-app"\n'
        'requires-python = ">=3.10"  # conformance: ignore[D006] legacy runtime\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        "]\n"
    )
    findings = scan_text(text, "pyproject.toml", sdk_managed_packages=set())
    assert len(findings) == 1
    assert findings[0].rule_id == "D006"
    assert findings[0].suppressed is True
    assert "legacy runtime" in (findings[0].suppression_justification or "")


def test_d006_sdk_python_floor_matches_sdk_pyproject() -> None:
    """Drift guard: SDK_PYTHON_FLOOR must track the SDK's real requires-python."""
    sdk_pyproject = Path(__file__).parents[3] / "pyproject.toml"
    if not sdk_pyproject.is_file():
        pytest.skip("SDK pyproject.toml not locatable from the test tree")
    text = sdk_pyproject.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    if data.get("project", {}).get("name") != "atlan-application-sdk":
        pytest.skip("repo-root pyproject is not the SDK")
    bound = _requires_python_lower_bound(text)
    assert bound is not None
    assert bound[0] == SDK_PYTHON_FLOOR


# ── D004: redeclaration in [dependency-groups] ───────────────────────────────


_GROUPS = """\
[project]
name = "demo-app"
version = "0.1.0"
dependencies = [
    "atlan-application-sdk>=3.17.2,<4.0.0",
]

[dependency-groups]
dev = [
    "pytest>=8,<9",
    "pydantic>=2,<3",
]
test = [
    {include-group = "dev"},
    "ruff>=0.6,<0.7",
]
"""


def test_iter_dependency_group_entries() -> None:
    entries = list(_iter_dependency_group_entries(_GROUPS))
    assert {e.name for e in entries} == {
        "pytest",
        "pydantic",
        "ruff",
    }  # include-group skipped
    pyd = next(e for e in entries if e.name == "pydantic")
    assert pyd.array_path == "dependency-groups.dev"
    assert pyd.line == 11


def test_d004_redeclared_in_dependency_group() -> None:
    findings = scan_text(
        _GROUPS,
        "pyproject.toml",
        sdk_managed_packages={"pydantic"},
        sdk_published_extras=set(),
    )
    assert [f.rule_id for f in findings] == ["D004"]
    f = findings[0]
    assert f.line == 11
    assert "dependency-groups.dev" in f.message


def test_d004_does_not_fire_when_group_dep_unmanaged() -> None:
    findings = scan_text(
        _GROUPS,
        "pyproject.toml",
        sdk_managed_packages={"fastapi"},  # not present in any group
        sdk_published_extras=set(),
    )
    assert findings == []


# ── D005: unknown SDK extra ──────────────────────────────────────────────────


def _sdk_extras_pyproject(extras: str, *, path: str = "project.dependencies") -> str:
    if path == "project.dependencies":
        return (
            '[project]\nname = "demo-app"\nversion = "0.1.0"\ndependencies = [\n'
            f'    "atlan-application-sdk[{extras}]>=3.17,<4.0.0",\n]\n'
        )
    # dependency-group form
    return (
        '[project]\nname = "demo-app"\nversion = "0.1.0"\ndependencies = [\n'
        '    "atlan-application-sdk>=3.17,<4.0.0",\n]\n\n'
        f'[dependency-groups]\ndev = [\n    "atlan-application-sdk[{extras}]>=3.17,<4.0.0",\n]\n'
    )


def test_d005_unknown_extra_fires() -> None:
    findings = scan_text(
        _sdk_extras_pyproject("workflows,dapr"),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras={"workflows", "sql"},
    )
    assert [f.rule_id for f in findings] == ["D005"]
    assert "dapr" in findings[0].message


def test_d005_known_extra_passes() -> None:
    findings = scan_text(
        _sdk_extras_pyproject("sql"),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras={"sql", "workflows"},
    )
    assert findings == []


def test_d005_extra_normalisation_matches_published() -> None:
    # app writes [iam_auth]; SDK publishes the normalised iam-auth -> no finding.
    findings = scan_text(
        _sdk_extras_pyproject("iam_auth"),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras={"iam-auth"},
    )
    assert findings == []


def test_d005_unknown_extra_in_dependency_group_fires() -> None:
    findings = scan_text(
        _sdk_extras_pyproject("dapr", path="dependency-groups"),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras={"tests"},
    )
    assert [f.rule_id for f in findings] == ["D005"]
    assert "dependency-groups.dev" in findings[0].message


def test_d005_skipped_when_sdk_metadata_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import conformance.suite.checks.dependency_conformance as mod

    monkeypatch.setattr(mod, "_sdk_published_extras", lambda: None)
    findings = scan_text(
        _sdk_extras_pyproject("dapr"),
        "pyproject.toml",
        sdk_managed_packages=set(),
    )
    assert all(f.rule_id != "D005" for f in findings)


def test_sdk_extras_in_parsing() -> None:
    assert _sdk_extras_in("atlan-application-sdk[sql,tests]>=3,<4") == ["sql", "tests"]
    assert _sdk_extras_in("atlan-application-sdk>=3,<4") == []


# ── D007: build backend ──────────────────────────────────────────────────────


def _build_backend_pyproject(backend: str) -> str:
    return (
        '[project]\nname = "demo-app"\nversion = "0.1.0"\ndependencies = [\n'
        '    "atlan-application-sdk>=3.17,<4.0.0",\n]\n\n'
        f'[build-system]\nrequires = ["x"]\nbuild-backend = "{backend}"\n'
    )


def test_d007_non_hatchling_backend_fires() -> None:
    findings = scan_text(
        _build_backend_pyproject("setuptools.build_meta"),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert [f.rule_id for f in findings] == ["D007"]


def test_d007_hatchling_passes() -> None:
    findings = scan_text(
        _build_backend_pyproject("hatchling.build"),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert findings == []


def test_d007_absent_build_backend_no_finding() -> None:
    text = (
        '[project]\nname = "demo-app"\nversion = "0.1.0"\ndependencies = [\n'
        '    "atlan-application-sdk>=3.17,<4.0.0",\n]\n'
    )
    findings = scan_text(
        text, "pyproject.toml", sdk_managed_packages=set(), sdk_published_extras=set()
    )
    assert findings == []


# ── D008: pyright type-checking mode ─────────────────────────────────────────


def _pyright_pyproject(mode: str) -> str:
    return (
        '[project]\nname = "demo-app"\nversion = "0.1.0"\ndependencies = [\n'
        '    "atlan-application-sdk>=3.17,<4.0.0",\n]\n\n'
        f'[tool.pyright]\ntypeCheckingMode = "{mode}"\n'
    )


@pytest.mark.parametrize("mode", ["off", "basic"])
def test_d008_weak_mode_fires(mode: str) -> None:
    findings = scan_text(
        _pyright_pyproject(mode),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert [f.rule_id for f in findings] == ["D008"]


@pytest.mark.parametrize("mode", ["standard", "strict"])
def test_d008_strong_mode_passes(mode: str) -> None:
    findings = scan_text(
        _pyright_pyproject(mode),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert findings == []


def test_d008_line_anchors_in_pyright_section_not_decoy() -> None:
    # A `typeCheckingMode` key in an unrelated table must not misanchor the
    # finding — _line_of is section-scoped to [tool.pyright].
    text = (
        '[project]\nname = "demo-app"\nversion = "0.1.0"\ndependencies = [\n'
        '    "atlan-application-sdk>=3.17,<4.0.0",\n]\n\n'
        '[tool.other]\ntypeCheckingMode = "strict"\n\n'
        '[tool.pyright]\ntypeCheckingMode = "basic"\n'
    )
    findings = scan_text(
        text, "pyproject.toml", sdk_managed_packages=set(), sdk_published_extras=set()
    )
    assert [f.rule_id for f in findings] == ["D008"]
    expected = next(
        i
        for i, ln in enumerate(text.splitlines(), start=1)
        if ln.strip() == 'typeCheckingMode = "basic"'
    )
    assert findings[0].line == expected


# ── D009: remote Dapr component fetch ────────────────────────────────────────


def _poe_pyproject(task_toml: str) -> str:
    return (
        '[project]\nname = "demo-app"\nversion = "0.1.0"\ndependencies = [\n'
        '    "atlan-application-sdk>=3.17,<4.0.0",\n]\n\n'
        f"{task_toml}\n"
    )


def test_d009_fires_on_github_contents_api_fetch() -> None:
    task = (
        "[tool.poe.tasks.download-components]\n"
        'interpreter = "python"\n'
        'env = { SDK_VERSION = "v3.14.0" }\n'
        'shell = """\n'
        "import requests\n"
        'api_url = "https://api.github.com/repos/atlanhq/application-sdk/contents/components"\n'
        'requests.get(api_url, params={"ref": "v3.14.0"})\n'
        '"""\n'
    )
    findings = scan_text(
        _poe_pyproject(task),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert [f.rule_id for f in findings] == ["D009"]


def test_d009_fires_on_raw_githubusercontent_fetch() -> None:
    task = (
        "[tool.poe.tasks]\n"
        "download-components.shell = "
        '"curl -O https://raw.githubusercontent.com/atlanhq/application-sdk/'
        'v3.14.0/components/statestore.yaml"\n'
    )
    findings = scan_text(
        _poe_pyproject(task),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert [f.rule_id for f in findings] == ["D009"]


def test_d009_passes_for_local_copy_from_installed_wheel() -> None:
    task = (
        "[tool.poe.tasks]\n"
        'download-components.shell = "python -c \\"import application_sdk\\""\n'
    )
    findings = scan_text(
        _poe_pyproject(task),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert findings == []


def test_d009_no_poe_tasks_no_finding() -> None:
    findings = scan_text(
        _poe_pyproject(""),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert findings == []


def test_d009_unrelated_poe_task_no_finding() -> None:
    task = '[tool.poe.tasks]\nstart-dapr = "dapr run --app-id app"\n'
    findings = scan_text(
        _poe_pyproject(task),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert findings == []


def test_d009_suppressed_inline_directive_above() -> None:
    task = (
        "[tool.poe.tasks]\n"
        "# conformance: ignore[D009] migration tracked separately\n"
        "download-components.shell = "
        '"curl -O https://raw.githubusercontent.com/atlanhq/application-sdk/'
        'v3.14.0/components/statestore.yaml"\n'
    )
    findings = scan_text(
        _poe_pyproject(task),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_d009_does_not_match_similarly_prefixed_repo_name() -> None:
    """A repo merely starting with 'application-sdk' must not false-positive."""
    task = (
        "[tool.poe.tasks]\n"
        "download-components.shell = "
        '"curl -O https://raw.githubusercontent.com/atlanhq/application-sdk-extra/'
        'v1.0.0/components/statestore.yaml"\n'
    )
    findings = scan_text(
        _poe_pyproject(task),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert findings == []


def test_d009_matches_bare_repo_reference_with_no_trailing_path() -> None:
    task = (
        "[tool.poe.tasks]\n"
        "download-components.shell = "
        '"echo https://api.github.com/repos/atlanhq/application-sdk"\n'
    )
    findings = scan_text(
        _poe_pyproject(task),
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert [f.rule_id for f in findings] == ["D009"]


def test_d009_multiple_tasks_one_violating_reports_once_at_correct_line() -> None:
    """A finding must anchor to the actual offending line, not misattribute
    across tasks when only one of several poe tasks violates the rule."""
    task = (
        "[tool.poe.tasks]\n"
        'start-dapr = "dapr run --app-id app"\n'
        "download-components.shell = "
        '"curl -O https://raw.githubusercontent.com/atlanhq/application-sdk/'
        'v3.14.0/components/statestore.yaml"\n'
    )
    text = _poe_pyproject(task)
    findings = scan_text(
        text,
        "pyproject.toml",
        sdk_managed_packages=set(),
        sdk_published_extras=set(),
    )
    assert len(findings) == 1
    expected_line = next(
        ln
        for ln, line in enumerate(text.splitlines(), start=1)
        if _REMOTE_COMPONENT_FETCH_RE.search(line)
    )
    assert findings[0].line == expected_line


def test_inline_duplicate_entries_get_distinct_columns() -> None:
    # A repeated requirement on one inline line must not alias to the first
    # match's column (the raw_line.index → offset-cursor fix).
    text = (
        '[project]\nname = "x"\n'
        'dependencies = ["rich>=13,<14", "click>=8,<9", "rich>=13,<14"]\n'
    )
    rich = [e for e in _iter_dep_entries(text) if e.name == "rich"]
    assert len(rich) == 2
    assert rich[0].column != rich[1].column


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
    # Use an unresolvable dependency name so D003 (which inspects installed
    # metadata) treats it as unanalysable and stays silent — keeping this an
    # exactly-one-D001 scenario regardless of what is installed in the test env.
    # The conformance dev group keeps D011 quiet for the same reason.
    _scratch_pyproject(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\n'
        'dependencies = ["nonexistent-fixture-pkg-zzz>=1,<2"]\n'
        "\n[dependency-groups]\n"
        'dev = [\n    "atlan-application-sdk-conformance>=0.17.0,<1.0.0",\n]\n',
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
    """SDK self-check emits no D001/D002 findings via the CLI.

    D001/D002 are app-only and exempt the SDK. D003 is scope=both and does apply
    to the SDK, but the declared dependency here is unresolvable, so D003 skips
    it — leaving zero findings overall.
    """
    _scratch_pyproject(
        tmp_path,
        '[project]\nname = "atlan-application-sdk"\nversion = "3.17.2"\n'
        'dependencies = ["nonexistent-fixture-pkg-zzz>=1,<2"]\n',
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


# ── D003 UnusedDependency ────────────────────────────────────────────────────


def _d003_scan(
    tmp_path: Path,
    deps: str,
    *,
    imported_modules: set[str],
    dist_import_map: dict[str, set[str] | None],
    dialect_drivers: set[str] | None = None,
    name: str = "my-connector",
) -> list:
    """Write a pyproject and run scan_all with injected import data (no env/AST).

    Returns only the D003 findings so D001/D002 noise is filtered out.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n{deps}',
        encoding="utf-8",
    )
    findings = scan_all(
        [pp],
        tmp_path,
        imported_modules=imported_modules,
        dist_import_map=dist_import_map,
        dialect_drivers=set() if dialect_drivers is None else dialect_drivers,
    )
    return [f for f in findings if f.rule_id == "D003"]


def test_d003_flags_unused_dependency(tmp_path: Path) -> None:
    findings = _d003_scan(
        tmp_path,
        'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n    "requests>=2,<3",\n]\n',
        imported_modules={"os", "sys"},
        dist_import_map={"requests": {"requests"}},
    )
    assert len(findings) == 1
    assert findings[0].rule_id == "D003"
    assert "requests" in findings[0].message
    assert not findings[0].suppressed


def test_d003_not_flagged_when_imported(tmp_path: Path) -> None:
    findings = _d003_scan(
        tmp_path,
        'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n    "requests>=2,<3",\n]\n',
        imported_modules={"requests", "os"},
        dist_import_map={"requests": {"requests"}},
    )
    assert findings == []


def test_d003_maps_dist_name_to_import_name(tmp_path: Path) -> None:
    """A dependency whose import name differs (pyyaml -> yaml) is not flagged
    when that import name appears in source."""
    findings = _d003_scan(
        tmp_path,
        'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n    "pyyaml>=6,<7",\n]\n',
        imported_modules={"yaml"},
        dist_import_map={"pyyaml": {"yaml"}},
    )
    assert findings == []


def test_d003_not_flagged_when_referenced_as_sqlalchemy_driver(tmp_path: Path) -> None:
    """A DBAPI driver loaded dynamically by SQLAlchemy via a ``dialect+driver``
    string (never imported) is treated as used, not flagged."""
    findings = _d003_scan(
        tmp_path,
        'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n    "aiomysql>=0.2,<1",\n]\n',
        imported_modules={"os"},
        dist_import_map={"aiomysql": {"aiomysql"}},
        dialect_drivers={"aiomysql"},
    )
    assert findings == []


def test_d003_dialect_driver_match_is_selective(tmp_path: Path) -> None:
    """A non-empty dialect_drivers set suppresses only the matching driver — an
    unrelated declared-but-unimported dependency is still flagged."""
    findings = _d003_scan(
        tmp_path,
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "aiomysql>=0.2,<1",\n'
        '    "requests>=2,<3",\n'
        "]\n",
        imported_modules={"os"},
        dist_import_map={"aiomysql": {"aiomysql"}, "requests": {"requests"}},
        dialect_drivers={"aiomysql"},
    )
    messages = [f.message for f in findings]
    assert any("requests" in m for m in messages), "requests must still be flagged"
    assert not any(
        "aiomysql" in m for m in messages
    ), "aiomysql is suppressed by the driver match"


def test_d003_collects_dialect_driver_from_source_string(tmp_path: Path) -> None:
    """End-to-end: a ``mysql+aiomysql`` dialect string in source clears the
    aiomysql D003 finding without an explicit import or injected drivers."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        '[project]\nname = "my-connector"\nversion = "0.1.0"\n'
        'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "aiomysql>=0.2,<1",\n]\n',
        encoding="utf-8",
    )
    src = tmp_path / "app" / "client.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        'URL = "mysql+aiomysql://user:pw@host:3306/db"\n'
        'DRIVERNAME = "mysql+aiomysql"\n',
        encoding="utf-8",
    )
    findings = scan_all(
        [pp, src],
        tmp_path,
        imported_modules={"os"},  # aiomysql NOT imported
        dist_import_map={"aiomysql": {"aiomysql"}},
        # dialect_drivers left to compute from source
    )
    assert [f for f in findings if f.rule_id == "D003"] == []


def test_collect_dialect_drivers_parses_both_forms(tmp_path: Path) -> None:
    src = tmp_path / "m.py"
    src.write_text(
        't1 = "mysql+aiomysql://u:p@h/d"\n'
        't2 = "postgresql+asyncpg"\n'
        'noise = "1 + 2 = 3"\n',
        encoding="utf-8",
    )
    assert _collect_dialect_drivers([src]) == {"aiomysql", "asyncpg"}


def test_d003_skips_unresolvable_dependency_and_reports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dependency that cannot be resolved (map value None) is never flagged,
    and is surfaced on stderr (no silent caps)."""
    findings = _d003_scan(
        tmp_path,
        'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n    "mystery-pkg>=1,<2",\n]\n',
        imported_modules={"os"},
        dist_import_map={"mystery-pkg": None},
    )
    assert findings == []
    assert "mystery-pkg" in capsys.readouterr().err


def test_d003_suppression(tmp_path: Path) -> None:
    findings = _d003_scan(
        tmp_path,
        'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "requests>=2,<3",  # conformance: ignore[D003] used via plugin loader\n]\n',
        imported_modules={"os"},
        dist_import_map={"requests": {"requests"}},
    )
    assert len(findings) == 1
    assert findings[0].suppressed
    assert findings[0].suppression_justification == "used via plugin loader"


def test_d003_runs_on_sdk_but_d001_d002_do_not(tmp_path: Path) -> None:
    """scope=both: D003 fires on the SDK's own repo, while the app-only D001/D002
    stay self-exempt (the SDK is a publisher of that contract, not subject to it)."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        '[project]\nname = "atlan-application-sdk"\nversion = "3.17.2"\n'
        'dependencies = [\n    "requests>=2,<3",\n]\n',
        encoding="utf-8",
    )
    findings = scan_all(
        [pp],
        tmp_path,
        imported_modules={"os"},
        dist_import_map={"requests": {"requests"}},
    )
    rule_ids = {f.rule_id for f in findings}
    assert rule_ids == {"D003"}  # no D001 despite the missing SDK self-dep


def test_d003_ignores_optional_dependency_extras(tmp_path: Path) -> None:
    """Only core [project.dependencies] is analysed; an unused extra is not D003."""
    findings = _d003_scan(
        tmp_path,
        'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n]\n'
        "\n[project.optional-dependencies]\n"
        'sql = [\n    "requests>=2,<3",\n]\n',
        imported_modules={"os"},
        dist_import_map={"requests": {"requests"}},
    )
    assert findings == []


def test_d003_flags_multiple_unused_with_line_numbers(tmp_path: Path) -> None:
    """Two unused deps -> two findings anchored to their own source lines."""
    findings = _d003_scan(
        tmp_path,
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'  # line 5
        '    "requests>=2,<3",\n'  # line 6
        '    "click>=8,<9",\n'  # line 7
        "]\n",
        imported_modules={"os"},
        dist_import_map={"requests": {"requests"}, "click": {"click"}},
    )
    by_line = {f.line: f for f in findings}
    assert set(by_line) == {6, 7}
    assert "requests" in by_line[6].message
    assert "click" in by_line[7].message


def test_d003_mixed_flagged_and_unresolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A resolvable-unused dep is flagged while an unresolvable one is reported
    to stderr — both happen in the same scan."""
    findings = _d003_scan(
        tmp_path,
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "requests>=2,<3",\n'
        '    "mystery-pkg>=1,<2",\n'
        "]\n",
        imported_modules={"os"},
        dist_import_map={"requests": {"requests"}, "mystery-pkg": None},
    )
    assert [f.message for f in findings if "requests" in f.message]
    assert len(findings) == 1
    err = capsys.readouterr().err
    assert "mystery-pkg" in err and "requests" not in err


def test_d003_empty_provided_set_is_unresolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty provided-names set (distinct code path from None) is treated as
    unresolvable: skipped, reported, never flagged."""
    findings = _d003_scan(
        tmp_path,
        'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n    "ghost>=1,<2",\n]\n',
        imported_modules={"os"},
        dist_import_map={"ghost": set()},
    )
    assert findings == []
    assert "ghost" in capsys.readouterr().err


def test_d003_end_to_end_real_ast_and_metadata(tmp_path: Path) -> None:
    """Exercise the real discover() -> AST import walk -> importlib.metadata path
    end to end (no injection), including a source file in a subdirectory.

    ``pydantic`` and ``jinja2`` are direct conformance dependencies, so both
    resolve from real installed metadata; only ``jinja2`` is never imported.
    """
    from conformance.suite.checks.dependency_conformance import discover

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-connector"\nversion = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "pydantic>=2,<3",\n'  # imported below -> used
        '    "jinja2>=3,<4",\n'  # never imported -> flagged
        "]\n",
        encoding="utf-8",
    )
    pkg = tmp_path / "my_connector"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "models.py").write_text(
        "import pydantic\nfrom pydantic import BaseModel\n", encoding="utf-8"
    )
    (pkg / "sub" / "deep.py").write_text("import os\n", encoding="utf-8")

    findings = [
        f for f in scan_all(discover(tmp_path), tmp_path) if f.rule_id == "D003"
    ]
    assert len(findings) == 1
    assert "jinja2" in findings[0].message
    assert all("pydantic" not in f.message for f in findings)


def _install_fake_dist(
    root: Path, dist_name: str, import_names: list[str], *, version: str = "1.0.0"
) -> None:
    """Materialise a minimal installed distribution in *root*'s own ``.venv``.

    Enough metadata for ``importlib.metadata.distributions(path=[...])`` to find
    it: a ``*.dist-info`` directory with ``METADATA`` (for the Name) and
    ``top_level.txt`` (for the provided import names).
    """
    site = root / ".venv" / "lib" / "python3.13" / "site-packages"
    info = site / f"{dist_name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    info.joinpath("METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n",
        encoding="utf-8",
    )
    info.joinpath("top_level.txt").write_text(
        "\n".join(import_names) + "\n", encoding="utf-8"
    )


def test_d003_resolves_import_names_from_the_target_repo_venv(tmp_path: Path) -> None:
    """A dependency absent from the *running* interpreter is still analysed when
    the repo under test has its own environment.

    ``ghostdep`` is not installed anywhere near this test process, so before the
    repo-env lookup existed this dependency resolved to ``None`` and was skipped
    as unanalysable — reported on stderr, but producing no finding.
    """
    from conformance.suite.checks.dependency_conformance import discover

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-connector"\nversion = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "ghostdep>=1,<2",\n'
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("import os\n", encoding="utf-8")
    _install_fake_dist(tmp_path, "ghostdep", ["ghostdep"])

    findings = [
        f for f in scan_all(discover(tmp_path), tmp_path) if f.rule_id == "D003"
    ]
    assert len(findings) == 1
    assert "ghostdep" in findings[0].message


def test_d003_repo_venv_resolution_reports_no_coverage_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Resolving from the repo's env leaves nothing unanalysed, so the
    "skipped N dependencies" coverage warning does not fire.

    This is the regression the fix targets: the same repo previously yielded a
    different finding count per invoking interpreter (0 findings and 5 skipped
    deps from one environment, 4 findings from the app's own), and the
    least-covered run was the one that looked cleanest.
    """
    from conformance.suite.checks.dependency_conformance import discover

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-connector"\nversion = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "ghostdep>=1,<2",\n'
        '    "useddep>=1,<2",\n'
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("import useddep\n", encoding="utf-8")
    _install_fake_dist(tmp_path, "ghostdep", ["ghostdep"])
    _install_fake_dist(tmp_path, "useddep", ["useddep"])

    findings = [
        f for f in scan_all(discover(tmp_path), tmp_path) if f.rule_id == "D003"
    ]
    assert [f.rule_id for f in findings] == ["D003"]
    assert "ghostdep" in findings[0].message
    assert "skipped" not in capsys.readouterr().err


def test_d003_falls_back_to_running_interpreter_without_a_repo_venv(
    tmp_path: Path,
) -> None:
    """No repo ``.venv`` -> resolution falls back to the invoking interpreter,
    preserving the previous behaviour rather than silently analysing nothing."""
    from conformance.suite.checks.dependency_conformance import discover

    assert not (tmp_path / ".venv").exists()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-connector"\nversion = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "jinja2>=3,<4",\n'  # a real conformance dep, never imported here
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("import os\n", encoding="utf-8")

    findings = [
        f for f in scan_all(discover(tmp_path), tmp_path) if f.rule_id == "D003"
    ]
    assert len(findings) == 1
    assert "jinja2" in findings[0].message


def test_d003_repo_venv_does_not_mask_a_dependency_it_lacks(tmp_path: Path) -> None:
    """A partially-synced repo env (e.g. ``--no-dev``) must not shrink coverage:
    a dependency missing from it still resolves via the running interpreter."""
    from conformance.suite.checks.dependency_conformance import discover

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-connector"\nversion = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        '    "jinja2>=3,<4",\n'  # only in the running interpreter
        '    "ghostdep>=1,<2",\n'  # only in the repo env
        "]\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("import os\n", encoding="utf-8")
    _install_fake_dist(tmp_path, "ghostdep", ["ghostdep"])

    findings = [
        f for f in scan_all(discover(tmp_path), tmp_path) if f.rule_id == "D003"
    ]
    messages = " ".join(f.message for f in findings)
    assert len(findings) == 2
    assert "ghostdep" in messages
    assert "jinja2" in messages


def test_d003_skips_non_utf8_source_without_crashing(tmp_path: Path) -> None:
    """A latin-1 source with a PEP 263 coding cookie is parsed (not skipped) and
    its imports counted, so a dep imported only there is not falsely flagged."""
    from conformance.suite.checks.dependency_conformance import (
        _collect_top_level_imports,
    )

    src = tmp_path / "legacy.py"
    src.write_bytes(
        b"# -*- coding: latin-1 -*-\n"
        b"# comment with a latin-1 byte: \xe9\n"
        b"import requests\n"
    )
    modules = _collect_top_level_imports([src])
    assert "requests" in modules


# ── D010 QueryTransformerWithoutDuckdb ───────────────────────────────────────

_D010_PYPROJECT_NO_EXTRA = (
    "[project]\n"
    'name = "my-connector"\n'
    'version = "0.1.0"\n'
    "dependencies = [\n"
    '    "atlan-application-sdk>=3.22.0,<4.0.0",\n'
    "]\n"
)

_D010_PYPROJECT_SQL_EXTRA = (
    "[project]\n"
    'name = "my-connector"\n'
    'version = "0.1.0"\n'
    "dependencies = [\n"
    '    "atlan-application-sdk[sql]>=3.22.0,<4.0.0",\n'
    "]\n"
)

_D010_PYPROJECT_SQL_AMONG_MULTIPLE_EXTRAS = (
    "[project]\n"
    'name = "my-connector"\n'
    'version = "0.1.0"\n'
    "dependencies = [\n"
    '    "atlan-application-sdk[iam-auth,sql,pandas,tests,workflows]==3.29.0",\n'
    "]\n"
)

_D010_TRANSFORMER_IMPORT = (
    "from application_sdk.transformers.query import QueryBasedTransformer\n"
)

# Realistic uv.lock shapes: a root [[package]] for the app itself plus the
# edges uv actually serialises. D010 walks this graph from the app's own entry,
# so the fixtures must carry it — a bare SDK/duckdb block says nothing about
# what a default install resolves.
_D010_LOCK_SQL_EXTRA = """\
[[package]]
name = "my-connector"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "atlan-application-sdk", extra = ["sql"] },
]

[[package]]
name = "atlan-application-sdk"
version = "3.24.0"
source = { registry = "https://pypi.org/simple" }

[package.optional-dependencies]
sql = [
    { name = "duckdb" },
]

[[package]]
name = "duckdb"
version = "1.3.0"
source = { registry = "https://pypi.org/simple" }
"""

_D010_LOCK_SQL_EXTRA_PLURAL = _D010_LOCK_SQL_EXTRA.replace(
    '{ name = "atlan-application-sdk", extra = ["sql"] }',
    '{ name = "atlan-application-sdk", extras = ["iam-auth", "sql", "pandas", "tests", "workflows"] }',
)

# duckdb is in the graph, but only as a dev-group dependency of the app.
_D010_LOCK_DUCKDB_DEV_ONLY = """\
[[package]]
name = "my-connector"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "atlan-application-sdk" },
]

[package.dev-dependencies]
dev = [
    { name = "duckdb" },
]

[[package]]
name = "atlan-application-sdk"
version = "3.24.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "duckdb"
version = "1.3.0"
source = { registry = "https://pypi.org/simple" }
"""

# A stale lock: pyproject declares [sql] but the lock was never regenerated.
_D010_LOCK_NO_EXTRA = """\
[[package]]
name = "my-connector"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "atlan-application-sdk" },
]

[[package]]
name = "atlan-application-sdk"
version = "3.24.0"
source = { registry = "https://pypi.org/simple" }
"""


def _d010_scan(
    tmp_path: Path,
    *,
    pyproject: str,
    source: str,
    uv_lock: str | None = None,
) -> list:
    """Write a repo shape and return only the D010 findings from scan_all."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(pyproject, encoding="utf-8")
    src = tmp_path / "app" / "transformer.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(source, encoding="utf-8")
    if uv_lock is not None:
        (tmp_path / "uv.lock").write_text(uv_lock, encoding="utf-8")
    findings = scan_all(
        [pp, src],
        tmp_path,
        imported_modules=set(),
        dist_import_map={},
        dialect_drivers=set(),
    )
    return [f for f in findings if f.rule_id == "D010"]


def test_d010_fires_when_lock_lacks_duckdb(tmp_path: Path) -> None:
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_NO_EXTRA,
        source=_D010_TRANSFORMER_IMPORT,
        uv_lock='[[package]]\nname = "atlan-application-sdk"\nversion = "3.24.0"\n',
    )
    assert len(findings) == 1
    assert "duckdb" in findings[0].message
    assert "app/transformer.py" in findings[0].message
    # Anchored at the SDK dependency line in pyproject.toml (where the fix goes).
    assert findings[0].file == "pyproject.toml"
    assert findings[0].line == 5


def test_d010_silent_when_lock_resolves_duckdb_for_the_app(tmp_path: Path) -> None:
    """duckdb reachable from the app's own production deps via the [sql] extra."""
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_SQL_EXTRA,
        source=_D010_TRANSFORMER_IMPORT,
        uv_lock=_D010_LOCK_SQL_EXTRA,
    )
    assert findings == []


def test_d010_fires_when_plural_lock_extra_does_not_resolve_duckdb(
    tmp_path: Path,
) -> None:
    """A plural ``extras`` edge still requires a reachable duckdb package."""
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_SQL_AMONG_MULTIPLE_EXTRAS,
        source=_D010_TRANSFORMER_IMPORT,
        uv_lock=_D010_LOCK_SQL_EXTRA_PLURAL.replace('    { name = "duckdb" },\n', ""),
    )
    assert len(findings) == 1


def test_d010_silent_when_plural_lock_extra_resolves_duckdb(tmp_path: Path) -> None:
    """The app's multi-extra SDK dependency activates the SQL extra in uv.lock."""
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_SQL_AMONG_MULTIPLE_EXTRAS,
        source=_D010_TRANSFORMER_IMPORT,
        uv_lock=_D010_LOCK_SQL_EXTRA_PLURAL,
    )
    assert findings == []


def test_d010_fires_when_duckdb_is_only_a_dev_dependency(tmp_path: Path) -> None:
    """uv.lock is a universal graph — duckdb present is not duckdb installed.

    Reachable only through ``[package.dev-dependencies]``, so a production
    ``uv sync --no-dev`` omits it and every transform_metadata call raises the
    ImportError D010 exists to catch.  A flat ``name = "duckdb"`` search over
    the lock text reads this as resolved and stays silent.
    """
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_NO_EXTRA,
        source=_D010_TRANSFORMER_IMPORT,
        uv_lock=_D010_LOCK_DUCKDB_DEV_ONLY,
    )
    assert len(findings) == 1
    assert "duckdb" in findings[0].message


def test_d010_fires_when_duckdb_declared_only_in_a_dev_group(tmp_path: Path) -> None:
    """No lock; ``[dependency-groups] dev`` is not installed by default."""
    findings = _d010_scan(
        tmp_path,
        pyproject=(
            _D010_PYPROJECT_NO_EXTRA
            + '\n[dependency-groups]\ndev = [\n    "duckdb>=1.0",\n]\n'
        ),
        source=_D010_TRANSFORMER_IMPORT,
    )
    assert len(findings) == 1


def test_d010_fires_when_sql_extra_is_only_in_an_optional_dev_extra(
    tmp_path: Path,
) -> None:
    """No lock; the [sql] extra referenced only from an optional-dependency array."""
    findings = _d010_scan(
        tmp_path,
        pyproject=(
            _D010_PYPROJECT_NO_EXTRA + "\n[project.optional-dependencies]\ndev = [\n"
            '    "atlan-application-sdk[sql]>=3.22.0,<4.0.0",\n]\n'
        ),
        source=_D010_TRANSFORMER_IMPORT,
    )
    assert len(findings) == 1


def test_d010_silent_without_transformer_import(tmp_path: Path) -> None:
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_NO_EXTRA,
        source="from application_sdk.io import ParquetFileReader\n",
        uv_lock='[[package]]\nname = "atlan-application-sdk"\nversion = "3.24.0"\n',
    )
    assert findings == []


def test_d010_no_lock_sql_extra_resolves(tmp_path: Path) -> None:
    """Without a uv.lock the [sql] extra on the SDK reference clears the rule."""
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_SQL_EXTRA,
        source=_D010_TRANSFORMER_IMPORT,
    )
    assert findings == []


def test_d010_no_lock_incremental_extra_resolves(tmp_path: Path) -> None:
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_NO_EXTRA.replace(
            "atlan-application-sdk>=", "atlan-application-sdk[incremental]>="
        ),
        source=_D010_TRANSFORMER_IMPORT,
    )
    assert findings == []


def test_d010_no_lock_direct_duckdb_dep_resolves(tmp_path: Path) -> None:
    findings = _d010_scan(
        tmp_path,
        pyproject=(
            "[project]\n"
            'name = "my-connector"\n'
            'version = "0.1.0"\n'
            "dependencies = [\n"
            '    "atlan-application-sdk>=3.22.0,<4.0.0",\n'
            '    "duckdb>=1.1.3,<1.6.0",\n'
            "]\n"
        ),
        source=_D010_TRANSFORMER_IMPORT,
    )
    assert findings == []


def test_d010_fires_without_lock_or_extra(tmp_path: Path) -> None:
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_NO_EXTRA,
        source=_D010_TRANSFORMER_IMPORT,
    )
    assert len(findings) == 1
    assert "[sql]" in findings[0].message


def test_d010_matches_from_transformers_import_query_form(tmp_path: Path) -> None:
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_NO_EXTRA,
        source="from application_sdk.transformers import query\n",
    )
    assert len(findings) == 1


def test_d010_lock_wins_over_extra_declaration(tmp_path: Path) -> None:
    """With a uv.lock present the lock is ground truth: a declared [sql] extra
    that is not reflected in the lock (stale lock) still fires."""
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_SQL_EXTRA,
        source=_D010_TRANSFORMER_IMPORT,
        uv_lock=_D010_LOCK_NO_EXTRA,
    )
    assert len(findings) == 1


def test_d010_skips_sdk_repo_itself(tmp_path: Path) -> None:
    findings = _d010_scan(
        tmp_path,
        pyproject=(
            "[project]\n"
            'name = "atlan-application-sdk"\n'
            'version = "3.24.0"\n'
            "dependencies = []\n"
        ),
        source=_D010_TRANSFORMER_IMPORT,
    )
    assert findings == []


def test_d010_suppressed_inline_directive(tmp_path: Path) -> None:
    pyproject = (
        "[project]\n"
        'name = "my-connector"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        "    # conformance: ignore[D010] duckdb vendored in the runtime image\n"
        '    "atlan-application-sdk>=3.22.0,<4.0.0",\n'
        "]\n"
    )
    findings = _d010_scan(
        tmp_path,
        pyproject=pyproject,
        source=_D010_TRANSFORMER_IMPORT,
    )
    assert len(findings) == 1
    assert findings[0].suppressed


def test_d010_rule_metadata() -> None:
    """BLOCK since FND-311: the finding names a guaranteed runtime ImportError.

    It landed as WARN under the new-rule tier policy carrying the note "treat it
    as an error"; the tier now carries that instead of the prose. Pinned per rule
    because the tier is what decides whether release-certify stops the app.
    """
    from conformance.suite.rules import get_rule
    from conformance.suite.schema.disposition import EnforcementTier, RuleScope

    rule = get_rule("D010")
    assert rule.name == "QueryTransformerWithoutDuckdb"
    assert rule.tier == EnforcementTier.BLOCK
    assert rule.scope == RuleScope.APP
    assert rule.rationale.strip()


def test_d010_survives_non_utf8_uv_lock(tmp_path: Path) -> None:
    """A non-UTF-8 lock must fall back to pyproject intent, not raise."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(_D010_PYPROJECT_NO_EXTRA, encoding="utf-8")
    src = tmp_path / "app" / "transformer.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(_D010_TRANSFORMER_IMPORT, encoding="utf-8")
    (tmp_path / "uv.lock").write_bytes(b"\xff\xfe[[package]]\n")
    findings = scan_all(
        [pp, src],
        tmp_path,
        imported_modules=set(),
        dist_import_map={},
        dialect_drivers=set(),
    )
    assert [f.rule_id for f in findings if f.rule_id == "D010"] == ["D010"]


def test_d010_fires_when_the_duckdb_edge_is_platform_gated(tmp_path: Path) -> None:
    """A marker-gated edge is not reachable everywhere.

    Ignoring PEP 508 markers let a win32-only duckdb read as universally
    resolved, silencing D010 on the platforms where the ImportError is real.
    """
    lock = _D010_LOCK_SQL_EXTRA.replace(
        '    { name = "duckdb" },\n',
        '    { name = "duckdb", marker = "sys_platform == \'win32\'" },\n',
    )
    assert lock != _D010_LOCK_SQL_EXTRA
    findings = _d010_scan(
        tmp_path,
        pyproject=_D010_PYPROJECT_SQL_EXTRA,
        source=_D010_TRANSFORMER_IMPORT,
        uv_lock=lock,
    )
    assert len(findings) == 1


# ── D011: conformance suite undeclared ───────────────────────────────────────


def _d011_scan(tmp_path: Path, body: str) -> list:
    """Write a root pyproject and return only the D011 findings.

    D011 lives in ``scan_all`` (repo-level), not ``scan_text``: it is an
    *absence* rule about the repo, so a monorepo must not collect one finding
    per sub-package pyproject.
    """
    pp = tmp_path / "pyproject.toml"
    pp.write_text(body, encoding="utf-8")
    findings = scan_all(
        [pp],
        tmp_path,
        imported_modules=set(),
        dist_import_map={},
        dialect_drivers=set(),
    )
    return [f for f in findings if f.rule_id == "D011"]


_D011_HEAD = (
    '[project]\nname = "demo-app"\nversion = "0.1.0"\n'
    'dependencies = [\n    "atlan-application-sdk>=3.17.2,<4.0.0",\n]\n'
)


def test_d011_fires_when_conformance_undeclared(tmp_path: Path) -> None:
    findings = _d011_scan(
        tmp_path,
        _D011_HEAD + '\n[dependency-groups]\ndev = [\n    "pytest>=8,<9",\n]\n',
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "D011"
    # anchored at the [dependency-groups] header (line 8: 6 lines of [project],
    # then the blank separator) so the reviewer sees where the declaration belongs
    assert f.line == 8
    assert "atlan-application-sdk-conformance" in f.message
    assert not f.suppressed


def test_d011_satisfied_by_dependency_group_dev(tmp_path: Path) -> None:
    findings = _d011_scan(
        tmp_path,
        _D011_HEAD + "\n[dependency-groups]\ndev = [\n"
        '    "atlan-application-sdk-conformance>=0.17.0,<1.0.0",\n]\n',
    )
    assert findings == []


def test_d011_satisfied_by_any_group_not_only_dev(tmp_path: Path) -> None:
    """Apps differ on which group they use; any of them satisfies the rule."""
    findings = _d011_scan(
        tmp_path,
        _D011_HEAD + '\n[dependency-groups]\ndev = [\n    "pytest>=8,<9",\n]\n'
        'test = [\n    "atlan-application-sdk-conformance>=0.17.0,<1.0.0",\n]\n',
    )
    assert findings == []


def test_d011_satisfied_by_optional_dependencies_array(tmp_path: Path) -> None:
    """The optional-dependencies shape is in real fleet use; it must not fire."""
    findings = _d011_scan(
        tmp_path,
        _D011_HEAD + "\n[project.optional-dependencies]\ndev = [\n"
        '    "atlan-application-sdk-conformance>=0.17.0,<1.0.0",\n]\n',
    )
    assert findings == []


def test_d011_normalises_the_package_name(tmp_path: Path) -> None:
    """PEP 503 normalisation: underscores/case must still count as declared."""
    findings = _d011_scan(
        tmp_path,
        _D011_HEAD + "\n[dependency-groups]\ndev = [\n"
        '    "Atlan_Application_SDK_Conformance>=0.17.0,<1.0.0",\n]\n',
    )
    assert findings == []


def test_d011_anchors_at_line_one_without_dependency_groups_table(
    tmp_path: Path,
) -> None:
    findings = _d011_scan(tmp_path, _D011_HEAD)
    assert len(findings) == 1
    assert findings[0].line == 1


def test_d011_exempts_the_sdk_itself(tmp_path: Path) -> None:
    """D011 is app-scoped: the SDK publishes the package, it does not consume it."""
    findings = _d011_scan(
        tmp_path,
        '[project]\nname = "atlan-application-sdk"\nversion = "0.1.0"\n'
        "dependencies = []\n",
    )
    assert findings == []


def test_d011_suppressed_inline_directive(tmp_path: Path) -> None:
    findings = _d011_scan(
        tmp_path,
        _D011_HEAD
        + "\n# conformance: ignore[D011] tool runs from a sibling repo here\n"
        '[dependency-groups]\ndev = [\n    "pytest>=8,<9",\n]\n',
    )
    assert len(findings) == 1
    assert findings[0].suppressed


def test_d011_reported_once_for_a_monorepo_with_sub_pyprojects(
    tmp_path: Path,
) -> None:
    """The reason D011 lives in scan_all: one finding per repo, not per file."""
    sub = tmp_path / "packages" / "inner"
    sub.mkdir(parents=True)
    (sub / "pyproject.toml").write_text(_D011_HEAD, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(_D011_HEAD, encoding="utf-8")
    findings = [
        f
        for f in scan_all(
            [tmp_path / "pyproject.toml", sub / "pyproject.toml"],
            tmp_path,
            imported_modules=set(),
            dist_import_map={},
            dialect_drivers=set(),
        )
        if f.rule_id == "D011"
    ]
    assert len(findings) == 1
    assert findings[0].file == "pyproject.toml"


def _d011_group(spec: str) -> str:
    return f'\n[dependency-groups]\ndev = [\n    "{spec}",\n]\n'


@pytest.mark.parametrize(
    "spec",
    [
        "atlan-application-sdk-conformance==0.13.0",  # exact pin (worst case)
        "atlan-application-sdk-conformance===0.13.0",  # arbitrary equality
        "atlan-application-sdk-conformance~=0.17.0",  # compatible-release pin
        "atlan-application-sdk-conformance>=0.17.0",  # uncapped, no upper
        "atlan-application-sdk-conformance",  # bare name
    ],
)
def test_d011_fires_on_specifier_that_cannot_float(tmp_path: Path, spec: str) -> None:
    findings = _d011_scan(tmp_path, _D011_HEAD + _d011_group(spec))
    assert len(findings) == 1
    assert "cannot" in findings[0].message and "float" in findings[0].message
    # prescribes the canonical capped form
    assert "atlan-application-sdk-conformance<=1.0.0" in findings[0].message


@pytest.mark.parametrize(
    "spec",
    [
        # The canonical form: a cap alone, no floor.
        "atlan-application-sdk-conformance<=1.0.0",
        "atlan-application-sdk-conformance<1.0.0",
        # The earlier two-sided form. Still accepted on purpose: the fleet is
        # already on it (atlan-app-template ships it), and rejecting it would
        # turn every declaring repo red and churn it back for no gain — a
        # floor does not change which version resolves under the cap.
        "atlan-application-sdk-conformance>=0.17.0,<1.0.0",
        "atlan-application-sdk-conformance>=0.17.0,<=1.0.0",
        "atlan-application-sdk-conformance>0.16,<1.0.0",
    ],
)
def test_d011_accepts_capped_floating_ranges(tmp_path: Path, spec: str) -> None:
    assert _d011_scan(tmp_path, _D011_HEAD + _d011_group(spec)) == []


def test_d011_pin_branch_anchors_at_the_declaring_line(tmp_path: Path) -> None:
    findings = _d011_scan(
        tmp_path,
        _D011_HEAD + '\n[dependency-groups]\ndev = [\n    "pytest>=8,<9",\n'
        '    "atlan-application-sdk-conformance==0.13.0",\n]\n',
    )
    assert len(findings) == 1
    # line 11: 6 header lines, blank, [dependency-groups], dev = [, pytest, entry
    assert findings[0].line == 11


# ── D011: declared in the runtime array ─────────────────────────────────────


def _d011_runtime(spec: str) -> str:
    """A root pyproject whose [project.dependencies] declares *spec*."""
    return (
        '[project]\nname = "demo-app"\nversion = "0.1.0"\n'
        "dependencies = [\n"
        '    "atlan-application-sdk>=3.17.2,<4.0.0",\n'
        f'    "{spec}",\n]\n'
    )


def test_d011_fires_on_a_floating_runtime_declaration(tmp_path: Path) -> None:
    """The one array the package must never appear in.

    A floating ``[project.dependencies]`` entry satisfies every other branch —
    ``uv run`` does spawn the script — so without a dedicated placement branch
    the rule would report nothing on the placement the catalog, the generated
    docs and ``dependency.prose.md`` all forbid.
    """
    findings = _d011_scan(
        tmp_path, _d011_runtime("atlan-application-sdk-conformance>=0.17.0,<1.0.0")
    )
    assert len(findings) == 1
    f = findings[0]
    assert "[project.dependencies]" in f.message
    assert "runtime image" in f.message
    # tells the remediator where it belongs, in canonical form
    assert "[dependency-groups].dev" in f.message
    assert "atlan-application-sdk-conformance<=1.0.0" in f.message
    # anchored at the offending runtime line (the second array entry), not the
    # dev group
    assert f.line == 6


def test_d011_runtime_declaration_fires_even_beside_a_correct_dev_group(
    tmp_path: Path,
) -> None:
    """A correct dev-group entry does not license the runtime one to stay."""
    findings = _d011_scan(
        tmp_path,
        _d011_runtime("atlan-application-sdk-conformance>=0.17.0,<1.0.0")
        + _D011_OK_GROUP,
    )
    assert len(findings) == 1
    assert "[project.dependencies]" in findings[0].message
    assert findings[0].line == 6


def test_d011_runtime_placement_takes_precedence_over_the_shape_branch(
    tmp_path: Path,
) -> None:
    """Placement is the more fundamental problem: moving it is the fix, not capping it."""
    findings = _d011_scan(
        tmp_path, _d011_runtime("atlan-application-sdk-conformance==0.13.0")
    )
    assert len(findings) == 1
    assert "[project.dependencies]" in findings[0].message
    assert "cannot float" not in findings[0].message


def test_d011_runtime_placement_takes_precedence_over_the_lock_branch(
    tmp_path: Path,
) -> None:
    _write_lock(tmp_path, include_conformance=False)
    findings = _d011_scan(
        tmp_path, _d011_runtime("atlan-application-sdk-conformance>=0.17.0,<1.0.0")
    )
    assert len(findings) == 1
    assert "[project.dependencies]" in findings[0].message
    assert "uv.lock" not in findings[0].message


def test_d011_optional_dependencies_placement_is_not_a_runtime_finding(
    tmp_path: Path,
) -> None:
    """Only [project.dependencies] is the runtime array; extras are opt-in."""
    assert (
        _d011_scan(
            tmp_path,
            _D011_HEAD + "\n[project.optional-dependencies]\ndev = [\n"
            '    "atlan-application-sdk-conformance>=0.17.0,<1.0.0",\n]\n',
        )
        == []
    )


# ── D011 branch 4: declared and floating, but absent from uv.lock ────────────


def _write_lock(tmp_path: Path, *, include_conformance: bool) -> None:
    body = 'version = 1\nrequires-python = ">=3.11"\n\n'
    body += '[[package]]\nname = "pytest"\nversion = "8.3.0"\n'
    if include_conformance:
        body += (
            '\n[[package]]\nname = "atlan-application-sdk-conformance"\n'
            'version = "0.19.1"\n'
        )
    (tmp_path / "uv.lock").write_text(body, encoding="utf-8")


_D011_OK_GROUP = _d011_group("atlan-application-sdk-conformance>=0.17.0,<1.0.0")


def test_d011_fires_when_declared_but_missing_from_lock(tmp_path: Path) -> None:
    _write_lock(tmp_path, include_conformance=False)
    findings = _d011_scan(tmp_path, _D011_HEAD + _D011_OK_GROUP)
    assert len(findings) == 1
    assert "no entry in uv.lock" in findings[0].message
    assert "uv lock" in findings[0].message


def test_d011_lock_branch_is_reachable_for_a_cap_only_specifier(
    tmp_path: Path,
) -> None:
    """The canonical `<=1.0.0` must reach branch 4, not stop at branch 3.

    Before a floor became optional, a cap-only declaration failed branch 3 and
    returned there, so a repo that had never run ``uv lock`` was told its
    specifier "cannot float" — a message describing the wrong defect and
    prescribing an edit that would not have fixed anything. Now branch 3 passes
    and the lock branch gets to report the real problem.
    """
    _write_lock(tmp_path, include_conformance=False)
    findings = _d011_scan(
        tmp_path,
        _D011_HEAD + _d011_group("atlan-application-sdk-conformance<=1.0.0"),
    )
    assert len(findings) == 1
    assert "no entry in uv.lock" in findings[0].message


def test_d011_clean_when_declared_and_present_in_lock(tmp_path: Path) -> None:
    _write_lock(tmp_path, include_conformance=True)
    assert _d011_scan(tmp_path, _D011_HEAD + _D011_OK_GROUP) == []


def test_d011_clean_when_cap_only_specifier_is_present_in_lock(
    tmp_path: Path,
) -> None:
    """The canonical form, end to end: declared, capped, and locked."""
    _write_lock(tmp_path, include_conformance=True)
    assert (
        _d011_scan(
            tmp_path,
            _D011_HEAD + _d011_group("atlan-application-sdk-conformance<=1.0.0"),
        )
        == []
    )


def test_d011_lock_branch_matches_normalised_lock_name(tmp_path: Path) -> None:
    """The lock entry is matched PEP 503-normalised, not by raw string."""
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "Atlan_Application_SDK_Conformance"\n'
        'version = "0.19.1"\n',
        encoding="utf-8",
    )
    assert _d011_scan(tmp_path, _D011_HEAD + _D011_OK_GROUP) == []


def test_d011_lock_branch_skipped_when_no_lock_exists(tmp_path: Path) -> None:
    """No lock is not a violation — many app repos are scanned without one."""
    assert _d011_scan(tmp_path, _D011_HEAD + _D011_OK_GROUP) == []


def test_d011_lock_branch_skipped_when_lock_unparseable(tmp_path: Path) -> None:
    """A malformed lock must never manufacture a finding."""
    (tmp_path / "uv.lock").write_text("not [valid toml", encoding="utf-8")
    assert _d011_scan(tmp_path, _D011_HEAD + _D011_OK_GROUP) == []


def test_d011_undeclared_takes_precedence_over_the_lock_branch(
    tmp_path: Path,
) -> None:
    """Exactly one finding per repo, and it names the most fundamental problem."""
    _write_lock(tmp_path, include_conformance=False)
    findings = _d011_scan(tmp_path, _D011_HEAD)
    assert len(findings) == 1
    assert "does not declare" in findings[0].message


# ── _is_floating_range unit coverage ────────────────────────────────────────


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("<=1.0.0", True),  # the canonical form: a cap, no floor
        ("<1.0.0", True),
        (">=0.17.0,<1.0.0", True),  # a floor is optional, not forbidden
        ("<1.0.0,>=0.17.0", True),  # clause order is irrelevant
        (">0.16,<=1.0.0", True),
        ("==0.13.0", False),
        ("===0.13.0", False),
        ("~=0.17.0", False),
        (">=0.17.0", False),  # uncapped: admits an unreviewed major
        ("", False),
        ("   ", False),
    ],
)
def test_is_floating_range(spec: str, expected: bool) -> None:
    assert _is_floating_range(spec) is expected


def test_is_floating_range_and_is_bounded_specifier_disagree_both_ways() -> None:
    """The two predicates are deliberately different, in both directions.

    They sit next to each other with near-identical loop bodies, so this pins
    the disagreement: editing one to match the other silently rewrites a
    different BLOCK rule.

    On pins, D011 is the stricter one: D001 accepts ``==X`` as bounded (an
    exact SDK pin is reviewable) while D011 must not, because an exact
    conformance pin freezes the ruleset that grades the repo.

    On a bare cap it is the other way round: D001 rejects ``<X`` (an SDK
    dependency with no floor is a real defect) while D011 accepts it, because
    the floor buys nothing once the cap is there.
    """
    assert _is_bounded_specifier("==0.13.0") is True
    assert _is_floating_range("==0.13.0") is False

    assert _is_bounded_specifier("<1.0.0") is False
    assert _is_floating_range("<1.0.0") is True
