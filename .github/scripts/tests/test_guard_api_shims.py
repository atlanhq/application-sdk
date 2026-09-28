"""Tests for guard_api_shims.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import guard_api_shims as guard  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]

_SHIM = '''"""Re-export of x."""

from __future__ import annotations

import warnings as _warnings
from typing import TYPE_CHECKING as _TYPE_CHECKING

import application_sdk_api.errors.base as _src
from application_sdk_api.errors.base import *  # noqa: F401,F403

if _TYPE_CHECKING:
    from application_sdk_api.errors.base import *  # noqa: F401,F403

_EXTRA: dict[str, str] = {}
_NOT_DEPRECATED: frozenset[str] = frozenset()
__all__ = list(_src.__all__)


def __getattr__(name):
    return getattr(_src, name)


def __dir__():
    return __all__
'''


def test_a_plain_shim_passes() -> None:
    assert guard.violations(_SHIM) == []


def test_a_class_is_rejected() -> None:
    found = guard.violations(_SHIM + "\n\nclass AuthError(Exception):\n    pass\n")
    assert found == ["line 27: class AuthError"]


def test_a_helper_function_is_rejected() -> None:
    found = guard.violations(_SHIM + "\n\ndef redact(text):\n    return text\n")
    assert any("function redact()" in f for f in found)


def test_a_constant_is_rejected() -> None:
    found = guard.violations(_SHIM + '\n_CATEGORY_TO_HTTP = {"AUTH": 401}\n')
    assert any("assignment to _CATEGORY_TO_HTTP" in f for f in found)


def test_logic_inside_type_checking_is_rejected() -> None:
    src = _SHIM + "\nif _TYPE_CHECKING:\n    X = 1\n"
    assert any("TYPE_CHECKING block" in f for f in guard.violations(src))


def test_a_listed_module_that_is_missing_is_reported(tmp_path: Path) -> None:
    problems = guard.check(tmp_path)
    assert len(problems) == len(guard.SHIM_MODULES)
    assert all("missing" in p for p in problems)


def test_the_real_shims_hold_no_code() -> None:
    assert guard.check(_REPO_ROOT) == []
