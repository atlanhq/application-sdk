"""E023 — error_code= / .error_code / .DEFAULT_ERROR_CODE on an SDK legacy-coded error."""

from __future__ import annotations

from pathlib import Path

import pytest

from conformance.suite.checks.error_handling import scan_path, scan_text

_STORAGE = "from application_sdk.storage.errors import StorageError\n"


def _e023(src: str) -> list[tuple[int, str]]:
    return [
        (f.line, f.message)
        for f in scan_text(src, "app/handler.py")
        if f.rule_id == "E023"
    ]


def _lines(src: str) -> list[int]:
    return [line for line, _ in _e023(src)]


def _line_of(src: str, text: str) -> int:
    return [i for i, line in enumerate(src.splitlines(), 1) if text in line][0]


# ---------------------------------------------------------------------------
# Flagged: the error_code= keyword
# ---------------------------------------------------------------------------


def test_flags_error_code_keyword_on_an_sdk_class() -> None:
    src = _STORAGE + (
        "from app.codes import MY_CODE\n\n\n"
        "def f():\n"
        "    raise StorageError('boom', error_code=MY_CODE)\n"
    )
    found = _e023(src)
    assert [line for line, _ in found] == [_line_of(src, "error_code=MY_CODE")]
    assert "StorageError" in found[0][1]
    assert "error_code= keyword" in found[0][1]


def test_flags_error_code_keyword_through_a_module_alias() -> None:
    src = (
        "import application_sdk.storage.errors as storage_errors\n\n\n"
        "def f(code):\n"
        "    raise storage_errors.StorageError('boom', error_code=code)\n"
    )
    assert _lines(src) == [5]


def test_flags_error_code_keyword_on_a_same_module_app_subclass() -> None:
    src = _STORAGE + (
        "\n\nclass MyStorageError(StorageError):\n"
        "    pass\n\n\n"
        "def f(code):\n"
        "    raise MyStorageError('boom', error_code=code)\n"
    )
    assert _lines(src) == [_line_of(src, "error_code=code")]


def test_flags_error_code_keyword_forwarded_through_super_init() -> None:
    src = _STORAGE + (
        "\n\nclass MyStorageError(StorageError):\n"
        "    def __init__(self, message, code):\n"
        "        super().__init__(message, error_code=code)\n"
    )
    assert _lines(src) == [_line_of(src, "super().__init__")]


def test_flags_error_code_keyword_on_an_app_subclass_imported_from_the_repo(
    tmp_path: Path,
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "base.py").write_text(
        "from application_sdk.credentials.errors import CredentialError\n\n\n"
        "class AppCredentialError(CredentialError):\n"
        "    pass\n"
    )
    target = tmp_path / "app" / "handler.py"
    target.write_text(
        "from app.base import AppCredentialError\n\n\n"
        "def f(code):\n"
        "    raise AppCredentialError('boom', error_code=code)\n"
    )
    findings = [f for f in scan_path(target, tmp_path) if f.rule_id == "E023"]
    assert [f.line for f in findings] == [5]
    assert "CredentialError" in findings[0].message


@pytest.mark.parametrize(
    ("imports", "expr"),
    [
        ("import app.base as base\n", "base.AppCredentialError"),
        ("import app.base\n", "app.base.AppCredentialError"),
        ("from app import base\n", "base.AppCredentialError"),
        ("from . import base\n", "base.AppCredentialError"),
    ],
)
def test_flags_error_code_keyword_on_a_module_qualified_repo_subclass(
    tmp_path: Path, imports: str, expr: str
) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "base.py").write_text(
        "from application_sdk.credentials.errors import CredentialError\n\n\n"
        "class AppCredentialError(CredentialError):\n"
        "    pass\n"
    )
    target = tmp_path / "app" / "handler.py"
    target.write_text(
        imports + f"\n\ndef f(code):\n    raise {expr}('boom', error_code=code)\n"
    )
    findings = [f for f in scan_path(target, tmp_path) if f.rule_id == "E023"]
    assert [f.line for f in findings] == [5]
    assert "CredentialError" in findings[0].message


# ---------------------------------------------------------------------------
# Flagged: reads
# ---------------------------------------------------------------------------


def test_flags_error_code_read_on_a_caught_sdk_error() -> None:
    src = _STORAGE + (
        "\n\ndef f(op):\n"
        "    try:\n"
        "        op()\n"
        "    except StorageError as exc:\n"
        "        return exc.error_code.code\n"
    )
    found = _e023(src)
    assert [line for line, _ in found] == [_line_of(src, "exc.error_code")]
    assert "Reads error_code" in found[0][1]


def test_flags_error_code_read_on_a_tuple_handler() -> None:
    src = _STORAGE + (
        "\n\ndef f(op):\n"
        "    try:\n"
        "        op()\n"
        "    except (ValueError, StorageError) as exc:\n"
        "        return exc.error_code\n"
    )
    assert _lines(src) == [_line_of(src, "exc.error_code")]


@pytest.mark.parametrize(
    "expr",
    ["StorageError.DEFAULT_ERROR_CODE", "MyStorageError.DEFAULT_ERROR_CODE"],
)
def test_flags_default_error_code_read_on_a_resolving_class(expr: str) -> None:
    src = _STORAGE + (
        f"\n\nclass MyStorageError(StorageError):\n    pass\n\n\nCODE = {expr}\n"
    )
    assert _lines(src) == [_line_of(src, "CODE =")]


@pytest.mark.parametrize("expr", ["self.error_code", "self.DEFAULT_ERROR_CODE"])
def test_flags_self_reads_inside_a_resolving_class(expr: str) -> None:
    src = _STORAGE + (
        "\n\nclass MyStorageError(StorageError):\n"
        "    def describe(self):\n"
        f"        return {expr}\n"
    )
    assert _lines(src) == [_line_of(src, "return")]


# ---------------------------------------------------------------------------
# Not flagged
# ---------------------------------------------------------------------------


def test_app_owned_error_code_vocabulary_is_not_flagged() -> None:
    # An app's own error classes with their own error_code attribute and
    # keyword: the app's design, not the SDK's legacy vocabulary.
    src = (
        "from application_sdk.errors import InternalError\n"
        "from app.codes import MY_CODE\n\n\n"
        "class MyError(Exception):\n"
        "    def __init__(self, message, error_code=None):\n"
        "        self.error_code = error_code\n\n"
        "    def describe(self):\n"
        "        return self.error_code\n\n\n"
        "def f(op):\n"
        "    try:\n"
        "        op()\n"
        "    except MyError as exc:\n"
        "        return exc.error_code.code\n"
        "    raise MyError('boom', error_code=MY_CODE)\n"
    )
    assert _lines(src) == []


def test_canonical_errors_module_classes_are_not_legacy_coded() -> None:
    src = (
        "from application_sdk.errors import AppError\n\n\n"
        "def f(op):\n"
        "    try:\n"
        "        op()\n"
        "    except AppError as exc:\n"
        "        return exc.error_code\n"
    )
    assert _lines(src) == []


@pytest.mark.parametrize(
    "body",
    [
        "def f(StorageError, code):\n    raise StorageError('x', error_code=code)\n",
        "def f(make, code):\n"
        "    StorageError = make()\n"
        "    raise StorageError('x', error_code=code)\n",
    ],
)
def test_shadowed_import_is_not_flagged(body: str) -> None:
    assert _lines(_STORAGE + "\n\n" + body) == []


def test_rebound_module_alias_head_is_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "base.py").write_text(
        "from application_sdk.credentials.errors import CredentialError\n\n\n"
        "class AppCredentialError(CredentialError):\n"
        "    pass\n"
    )
    target = tmp_path / "app" / "handler.py"
    target.write_text(
        "import app.base as base\n\n\n"
        "def f(base, code):\n"
        "    raise base.AppCredentialError('boom', error_code=code)\n"
    )
    assert [f for f in scan_path(target, tmp_path) if f.rule_id == "E023"] == []


def test_unnarrowed_value_is_not_tracked() -> None:
    src = _STORAGE + (
        "\n\ndef f(mapped):\n"
        "    if isinstance(mapped, StorageError):\n"
        "        return mapped.error_code\n"
    )
    assert _lines(src) == []


def test_private_slot_and_assignment_are_not_flagged() -> None:
    src = _STORAGE + (
        "\n\nclass MyStorageError(StorageError):\n"
        "    def reset(self):\n"
        "        self._error_code = None\n"
    )
    assert _lines(src) == []


def test_handler_name_outside_its_handler_is_not_flagged() -> None:
    src = _STORAGE + (
        "\n\ndef f(op, exc):\n"
        "    try:\n"
        "        op()\n"
        "    except StorageError:\n"
        "        pass\n"
        "    return exc.error_code\n"
    )
    assert _lines(src) == []


def test_suppression_directive_is_honoured() -> None:
    src = _STORAGE + (
        "\n\ndef f(code):\n"
        "    raise StorageError(  # conformance: ignore[E023] migrating in FOO-1\n"
        "        'boom', error_code=code  # conformance: ignore[E023] migrating in FOO-1\n"
        "    )\n"
    )
    findings = [f for f in scan_text(src, "app/handler.py") if f.rule_id == "E023"]
    assert findings and all(f.suppressed for f in findings)
