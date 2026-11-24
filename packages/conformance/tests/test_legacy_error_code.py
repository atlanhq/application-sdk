"""E022 — a class binds DEFAULT_ERROR_CODE in the SDK's legacy AAF-* vocabulary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conformance.suite.checks.error_handling import scan_path, scan_text
from conformance.suite.checks.error_handling._constants import (
    SDK_LEGACY_CODE_NAMES,
    SDK_LEGACY_CODED_CLASSES,
)
from tests._helpers import sdk_root

_HEADER = "from typing import ClassVar\n\n"


def _e022_lines(src: str) -> list[int]:
    return [f.line for f in scan_text(src, "app/errors.py") if f.rule_id == "E022"]


def _line_of(src: str, text: str) -> int:
    return src.splitlines().index(text) + 1


# ---------------------------------------------------------------------------
# Flagged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "binding",
    [
        "DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STORAGE_CONFIG",
        "DEFAULT_ERROR_CODE = STORAGE_CONFIG",
    ],
)
def test_flags_sdk_aaf_constant(binding: str) -> None:
    src = (
        _HEADER
        + "from application_sdk.errors import STORAGE_CONFIG, ErrorCode, InvalidInputError\n\n\n"
        "class BadConfig(InvalidInputError):\n"
        '    code: ClassVar[str] = "INVALID_INPUT_BAD_CONFIG"\n'
        f"    {binding}\n"
    )
    assert _e022_lines(src) == [_line_of(src, f"    {binding}")]


def test_flags_attribute_access_through_the_errors_module() -> None:
    src = (
        _HEADER + "import application_sdk.errors as sdk_errors\n\n\n"
        "class Bad(sdk_errors.InvalidInputError):\n"
        "    DEFAULT_ERROR_CODE = sdk_errors.APP_ERROR\n"
    )
    assert _e022_lines(src) == [
        _line_of(src, "    DEFAULT_ERROR_CODE = sdk_errors.APP_ERROR")
    ]


def test_flags_annotation_only_naming_sdk_error_code() -> None:
    src = (
        _HEADER + "from application_sdk.errors import ErrorCode\n"
        "from app.codes import MY_CODE\n\n\n"
        "class Bad(Exception):\n"
        "    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = MY_CODE\n"
    )
    assert _e022_lines(src) == [
        _line_of(src, "    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = MY_CODE")
    ]


def test_flags_override_on_an_sdk_legacy_coded_subclass() -> None:
    # The value is the app's own; the base makes it an AAF-* override.
    src = (
        _HEADER + "from application_sdk.storage.errors import StorageError\n"
        "from app.codes import MY_CODE\n\n\n"
        "class AppStorageError(StorageError):\n"
        "    DEFAULT_ERROR_CODE = MY_CODE\n"
    )
    findings = [f for f in scan_text(src, "app/errors.py") if f.rule_id == "E022"]
    assert [f.line for f in findings] == [
        _line_of(src, "    DEFAULT_ERROR_CODE = MY_CODE")
    ]
    assert "StorageError" in findings[0].message


def test_flags_override_through_a_same_module_app_intermediate() -> None:
    src = (
        _HEADER + "from application_sdk.storage.errors import StorageError\n"
        "from app.codes import MY_CODE\n\n\n"
        "class AppStorageError(StorageError):\n"
        "    pass\n\n\n"
        "class CustomStorageError(AppStorageError):\n"
        "    DEFAULT_ERROR_CODE = MY_CODE\n"
    )
    findings = [f for f in scan_text(src, "app/errors.py") if f.rule_id == "E022"]
    assert [f.line for f in findings] == [
        _line_of(src, "    DEFAULT_ERROR_CODE = MY_CODE")
    ]
    assert "StorageError" in findings[0].message


def test_flags_override_through_an_app_intermediate_imported_from_the_repo(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "base.py").write_text(
        "from application_sdk.credentials.errors import CredentialError\n\n\n"
        "class AppCredentialError(CredentialError):\n"
        "    pass\n"
    )
    target = tmp_path / "app" / "errors.py"
    target.write_text(
        "from app.base import AppCredentialError\n"
        "from app.codes import MY_CODE\n\n\n"
        "class Custom(AppCredentialError):\n"
        "    DEFAULT_ERROR_CODE = MY_CODE\n"
    )
    findings = [f for f in scan_path(target, tmp_path) if f.rule_id == "E022"]
    assert [f.line for f in findings] == [6]
    assert "CredentialError" in findings[0].message


def test_flags_override_on_a_base_reached_through_a_module_alias() -> None:
    src = (
        "import application_sdk.storage.errors as storage_errors\n"
        "from app.codes import MY_CODE\n\n\n"
        "class Custom(storage_errors.StorageNotFoundError):\n"
        "    DEFAULT_ERROR_CODE = MY_CODE\n"
    )
    assert _e022_lines(src) == [_line_of(src, "    DEFAULT_ERROR_CODE = MY_CODE")]


# ---------------------------------------------------------------------------
# Not flagged
# ---------------------------------------------------------------------------


def test_app_owned_code_vocabulary_on_an_app_mixin_is_not_flagged() -> None:
    # The shape three connector apps use: their own code type on their own mixin.
    src = (
        _HEADER + "from application_sdk.errors import AuthError\n"
        "from app.errors import codes as error_codes\n"
        "from app.errors.codes import DatabricksErrorCode\n\n\n"
        "class _DatabricksErrorMixin:\n"
        "    DEFAULT_ERROR_CODE: ClassVar[DatabricksErrorCode]\n\n\n"
        "class DatabricksAuthError(_DatabricksErrorMixin, AuthError):\n"
        '    code: ClassVar[str] = "AUTH_DATABRICKS"\n'
        "    DEFAULT_ERROR_CODE = error_codes.DBX_AUTH_INVALID_PAT\n"
    )
    assert _e022_lines(src) == []


def test_canonical_app_error_base_is_not_a_legacy_coded_class() -> None:
    # application_sdk.errors.AppError carries no AAF-* code; only the
    # deprecated application_sdk.app.AppError shim of the same name does.
    src = (
        _HEADER + "from application_sdk.errors import AppError\n"
        "from app.codes import MY_CODE\n\n\n"
        "class Mine(AppError):\n"
        "    DEFAULT_ERROR_CODE = MY_CODE\n"
    )
    assert _e022_lines(src) == []


def test_deprecated_app_shim_base_is_flagged() -> None:
    src = (
        _HEADER + "from application_sdk.app import AppError\n"
        "from app.codes import MY_CODE\n\n\n"
        "class Mine(AppError):\n"
        "    DEFAULT_ERROR_CODE = MY_CODE\n"
    )
    assert _e022_lines(src) == [_line_of(src, "    DEFAULT_ERROR_CODE = MY_CODE")]


def test_same_named_local_class_is_not_flagged() -> None:
    src = (
        _HEADER + "from app.storage import StorageError\n"
        "from app.codes import MY_CODE\n\n\n"
        "class Mine(StorageError):\n"
        "    DEFAULT_ERROR_CODE = MY_CODE\n"
    )
    assert _e022_lines(src) == []


def test_reading_default_error_code_is_not_flagged() -> None:
    src = (
        "from application_sdk.storage.errors import StorageError\n\n"
        "print(StorageError.DEFAULT_ERROR_CODE)\n"
    )
    assert _e022_lines(src) == []


def test_suppression_directive_is_honoured() -> None:
    src = (
        _HEADER
        + "from application_sdk.errors import STORAGE_CONFIG, InvalidInputError\n\n\n"
        "class BadConfig(InvalidInputError):\n"
        "    DEFAULT_ERROR_CODE = STORAGE_CONFIG  # conformance: ignore[E022] migrating in FOO-1\n"
    )
    findings = [f for f in scan_text(src, "app/errors.py") if f.rule_id == "E022"]
    assert findings and all(f.suppressed for f in findings)


# ---------------------------------------------------------------------------
# The two allowlists stay in step with the SDK
# ---------------------------------------------------------------------------


def _legacy_codes_tree() -> ast.Module:
    path = sdk_root() / "application_sdk" / "errors" / "_legacy_codes.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def test_code_names_match_the_sdk_legacy_codes_module() -> None:
    tree = _legacy_codes_tree()
    constants = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_code"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert SDK_LEGACY_CODE_NAMES == constants | {"ErrorCode"}


def test_coded_classes_match_the_sdk_table_and_its_subclasses() -> None:
    # Roots: the classes _CLASS_CODES names. Closure: every application_sdk
    # class that subclasses one of them, matched by bare base name.
    roots: set[str] = set()
    for node in _legacy_codes_tree().body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_CLASS_CODES"
            and isinstance(node.value, ast.Dict)
        ):
            roots = {
                key.value.rpartition(".")[2]
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    assert roots, "_CLASS_CODES not found in application_sdk/errors/_legacy_codes.py"

    bases: dict[str, set[str]] = {}
    for path in (sdk_root() / "application_sdk").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                names = {
                    b.id if isinstance(b, ast.Name) else b.attr
                    for b in node.bases
                    if isinstance(b, ast.Name | ast.Attribute)
                }
                bases.setdefault(node.name, set()).update(names)
    # ``AppError`` is ambiguous by bare name: application_sdk.app's deprecated
    # shim is a root, but every SDK error subclasses the canonical
    # application_sdk.errors.AppError. Close over the other roots and add the
    # shim's name back; its subclasses (NonRetryableError, RetryableError) are
    # roots in their own right.
    closure = roots - {"AppError"}
    changed = True
    while changed:
        changed = False
        for cls, parents in bases.items():
            if cls not in closure and parents & closure:
                closure.add(cls)
                changed = True
    assert SDK_LEGACY_CODED_CLASSES == closure | {"AppError"}
