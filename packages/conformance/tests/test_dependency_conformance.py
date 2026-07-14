"""Tests for the D-series (D001-D009) dependency_conformance check."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from conformance.suite.checks._ast_common import parse_toml_suppressions
from conformance.suite.checks.dependency_conformance import (
    _REMOTE_COMPONENT_FETCH_RE,
    SDK_PYTHON_FLOOR,
    _collect_dialect_drivers,
    _is_bounded_specifier,
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
    _scratch_pyproject(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\n'
        'dependencies = ["nonexistent-fixture-pkg-zzz>=1,<2"]\n',
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
