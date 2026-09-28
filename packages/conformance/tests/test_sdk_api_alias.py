"""``application_sdk_api.X`` is the same object as ``application_sdk.X`` to every detector.

The handler surface and the error taxonomy moved into ``atlan-application-sdk-api``
(import root ``application_sdk_api``).  Detectors that match the literal
``application_sdk.errors.`` / ``application_sdk.handler.`` prefixes fold the new
spelling onto the old one through ``_ast_common.canonical_sdk_module``; these
tests pin that each of them sees the new path exactly as it sees the old one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conformance.suite.checks import error_seam, prescriptions
from conformance.suite.checks._ast_common import (
    _parse_directives,
    canonical_sdk_module,
    is_sdk_module,
)
from conformance.suite.checks.deprecation import scan_consumer
from conformance.suite.checks.deprecation._manifest import (
    DeprecatedSymbol,
    Manifest,
    build_manifest,
)
from conformance.suite.checks.error_seam._public_error_surface import build_allowlist
from conformance.suite.checks.preflight import _untyped_failure
from conformance.suite.checks.preflight._common import build_registry
from conformance.suite.checks.preflight._contracts import scan as scan_contracts
from conformance.suite.checks.prescriptions._decorator_provenance import (
    collect_import_provenance,
)

LEGACY = "application_sdk"
API = "application_sdk_api"
ROOTS = pytest.mark.parametrize("root", [LEGACY, API])


# ── the helper ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("application_sdk_api", "application_sdk"),
        ("application_sdk_api.handler", "application_sdk.handler"),
        (
            "application_sdk_api.handler.contracts.PreflightCheck",
            "application_sdk.handler.contracts.PreflightCheck",
        ),
        ("application_sdk.errors", "application_sdk.errors"),
        ("application_sdk_apiary.x", "application_sdk_apiary.x"),
        (".handler", ".handler"),
        ("", ""),
    ],
)
def test_canonical_sdk_module(name: str, expected: str) -> None:
    assert canonical_sdk_module(name) == expected


def test_is_sdk_module_accepts_both_roots_only() -> None:
    assert is_sdk_module("application_sdk")
    assert is_sdk_module("application_sdk_api.errors")
    assert not is_sdk_module("application_sdk_apiary")
    assert not is_sdk_module("app.handler")


# ── P002 CategoryFieldOverride ───────────────────────────────────────────────


@ROOTS
def test_p002_fires_for_a_leaf_imported_from_either_root(root: str) -> None:
    src = (
        f"from {root}.errors import NotFoundError, FailureCategory\n"
        "class DomainErr(NotFoundError):\n"
        "    category = FailureCategory.NOT_FOUND\n"
    )
    assert [f.rule_id for f in prescriptions.scan_text(src, "app/errors.py")] == [
        "P002"
    ]


# ── P043 / P045 (error seam) ─────────────────────────────────────────────────

_PRIVATE = "{root}.storage.formats.format_errors"


@ROOTS
def test_p045_fires_for_a_private_error_import_from_either_root(root: str) -> None:
    src = f"from {_PRIVATE.format(root=root)} import FormatReadError\n"
    ids = [f.rule_id for f in error_seam.scan_text(src, "app/io.py")]
    assert ids == ["P045"]


@ROOTS
def test_p043_fires_for_control_flow_on_a_private_error_from_either_root(
    root: str,
) -> None:
    src = (
        f"from {_PRIVATE.format(root=root)} import FormatReadError\n"
        "try:\n    pass\nexcept FormatReadError:\n    raise\n"
    )
    ids = [f.rule_id for f in error_seam.scan_text(src, "app/io.py")]
    assert "P043" in ids


def test_error_seam_silent_on_the_public_api_error_module() -> None:
    src = (
        "from application_sdk_api.errors import AppError\n"
        "try:\n    pass\nexcept AppError:\n    raise\n"
    )
    assert error_seam.scan_text(src, "app/io.py") == []


# ── preflight (F-series) ─────────────────────────────────────────────────────


def _preflight_ids(tmp_path: Path, root: str, body: str) -> list[str]:
    path = tmp_path / "handler.py"
    path.write_text(
        f"from {root}.handler.base import Handler\n"
        f"from {root}.handler.contracts import PreflightInput, PreflightOutput, "
        "PreflightCheck\n"
        f"from {root}.errors import AuthError, FailureDetails\n"
        "\nclass H(Handler):\n"
        "    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
        + "\n".join("        " + line for line in body.splitlines())
        + "\n"
    )
    return [f.rule_id for f in scan_contracts(build_registry([path], tmp_path))]


@ROOTS
@pytest.mark.parametrize(
    "body,rule",
    [
        ('return {"success": True}', "F006"),
        (
            "return PreflightOutput(checks=[PreflightCheck(passed=False, "
            'error=FailureDetails(message="Probe failed"))])',
            "F007",
        ),
        ('raise AuthError(message="Probe failed")', "F008"),
    ],
)
def test_preflight_contract_rules_see_either_root(
    tmp_path: Path, root: str, body: str, rule: str
) -> None:
    assert rule in _preflight_ids(tmp_path, root, body)


@ROOTS
def test_preflight_contract_rules_clean_for_either_root(
    tmp_path: Path, root: str
) -> None:
    assert _preflight_ids(tmp_path, root, "return PreflightOutput(checks=[])") == []


@pytest.mark.parametrize(
    "imports,constructor",
    [
        ("from application_sdk_api.handler import PreflightCheck", "PreflightCheck"),
        (
            "from application_sdk_api.handler.contracts import PreflightCheck as C",
            "C",
        ),
        ("import application_sdk_api.handler as handler", "handler.PreflightCheck"),
        (
            "import application_sdk_api.handler",
            "application_sdk_api.handler.PreflightCheck",
        ),
    ],
)
def test_preflight_untyped_failure_sees_the_api_root(
    tmp_path: Path, imports: str, constructor: str
) -> None:
    path = tmp_path / "handler.py"
    path.write_text(
        imports
        + f'\ndef make():\n    return {constructor}(name="probe", passed=False)\n'
    )
    result = _untyped_failure.scan(build_registry([path], tmp_path))
    assert [f.rule_id for f in result] == ["F003"]


# ── decorator / contract provenance (P008, P013, P014, determinism) ──────────


@ROOTS
def test_provenance_treats_either_root_as_the_sdk(root: str) -> None:
    import ast

    tree = ast.parse(
        f"from {root}.handler.contracts import PreflightInput\n"
        f"import {root}.handler.contracts as hc\n"
    )
    prov = collect_import_provenance(tree)
    assert "PreflightInput" in prov.sdk_contract_names
    assert "hc" in prov.sdk_contract_module_aliases
    assert API not in prov.non_sdk_module_names


# ── B001 DeprecatedSdkSymbolUsage ────────────────────────────────────────────


def _manifest(module: str) -> Manifest:
    return Manifest(
        symbols=(
            DeprecatedSymbol(
                symbol="HandlerError",
                kind="class",
                module=module,
                marker_via="warn",
                message="use a typed AppError subclass",
                migration_target=True,
                removal_version="4.0",
            ),
        )
    )


@pytest.mark.parametrize("record_root", [LEGACY, API])
@ROOTS
def test_b001_matches_across_roots(root: str, record_root: str) -> None:
    import ast

    src = f"from {root}.handler import HandlerError\n"
    findings = scan_consumer(
        ast.parse(src),
        "app/handler.py",
        _manifest(f"{record_root}.handler.base"),
        _parse_directives(src),
    )
    assert [f.rule_id for f in findings] == ["B001"]


def test_b001_does_not_match_a_sibling_module_through_the_alias() -> None:
    import ast

    src = "from application_sdk_api.errors import HandlerError\n"
    findings = scan_consumer(
        ast.parse(src),
        "app/handler.py",
        _manifest("application_sdk_api.handler.base"),
        _parse_directives(src),
    )
    assert findings == []


# ── generators read the api tree ─────────────────────────────────────────────


def test_build_manifest_records_deprecations_defined_in_the_api_package(
    tmp_path: Path,
) -> None:
    (tmp_path / "application_sdk").mkdir()
    (tmp_path / "application_sdk" / "__init__.py").write_text("")
    api = tmp_path / "packages" / "api" / "application_sdk_api" / "handler"
    api.mkdir(parents=True)
    (api.parent / "__init__.py").write_text("")
    (api / "__init__.py").write_text("")
    (api / "base.py").write_text(
        "import warnings\n"
        "class HandlerError(Exception):\n"
        "    def __init__(self):\n"
        "        warnings.warn('HandlerError is deprecated; use AppError', "
        "DeprecationWarning, stacklevel=2)\n"
    )
    records = build_manifest(tmp_path).symbols_named("HandlerError")
    assert [r.module for r in records] == ["application_sdk_api.handler.base"]


def test_build_allowlist_falls_back_to_the_api_package(tmp_path: Path) -> None:
    sdk = tmp_path / "application_sdk" / "errors"
    sdk.mkdir(parents=True)
    (sdk / "__init__.py").write_text(
        "import application_sdk_api.errors as _src\n__all__ = _src.__all__\n"
    )
    api = tmp_path / "packages" / "api" / "application_sdk_api" / "errors"
    api.mkdir(parents=True)
    (api / "__init__.py").write_text(
        '__all__ = ["AppError", "AuthError", "FailureDetails"]\n'
    )
    assert build_allowlist(tmp_path) == ("AppError", "AuthError")


def test_build_allowlist_prefers_a_literal_sdk_all(tmp_path: Path) -> None:
    sdk = tmp_path / "application_sdk" / "errors"
    sdk.mkdir(parents=True)
    (sdk / "__init__.py").write_text('__all__ = ["AppError"]\n')
    assert build_allowlist(tmp_path) == ("AppError",)
