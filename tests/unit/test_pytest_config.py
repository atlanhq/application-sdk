"""Guards on the SDK suite's own pytest configuration (FND-5474).

The ``filterwarnings`` tests run under the root ``[tool.pytest.ini_options]``,
which pytest applies around every test, so they exercise the live policy
rather than a copy of it.
"""

from __future__ import annotations

import tomllib
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ini(pyproject: Path) -> dict[str, object]:
    return tomllib.loads(pyproject.read_text())["tool"]["pytest"]["ini_options"]


@pytest.mark.parametrize(
    "pyproject",
    [REPO_ROOT / "pyproject.toml", REPO_ROOT / "packages/conformance/pyproject.toml"],
    ids=["root", "conformance"],
)
def test_fixture_loop_scope_is_pinned(pyproject: Path) -> None:
    # Unset, pytest-asyncio warns at configure time, and under
    # `-W error::DeprecationWarning` that warning aborts the run before a
    # single test is collected.
    assert _ini(pyproject)["asyncio_default_fixture_loop_scope"] == "function"


def _warn_from(module: str) -> None:
    warnings.warn_explicit(
        "deprecated", DeprecationWarning, "somewhere.py", 1, module=module
    )


@pytest.mark.parametrize("module", ["application_sdk", "application_sdk.io.json"])
def test_deprecation_attributed_to_the_sdk_fails_the_test(module: str) -> None:
    with pytest.raises(DeprecationWarning):
        _warn_from(module)


@pytest.mark.parametrize(
    "module", ["pandas.core.frame", "tests.unit.test_x", "application_sdk_other"]
)
def test_deprecation_attributed_elsewhere_only_warns(module: str) -> None:
    with warnings.catch_warnings(record=True) as caught:
        # Keep the ini filters, but let a non-matching warning be recorded
        # rather than deduplicated away.
        warnings.filterwarnings("always", category=DeprecationWarning, append=True)
        _warn_from(module)
    assert [w.category for w in caught] == [DeprecationWarning]


_SDK = "application_sdk(\\.|$)"


def test_allowlist_entries_are_narrower_than_the_policy() -> None:
    # An entry with no message on the whole SDK would exempt every deprecation
    # attributed to it, third-party ones included, and silently undo the policy.
    # Each exemption names a message, or a deprecated subpackage.
    filters = _ini(REPO_ROOT / "pyproject.toml")["filterwarnings"]
    assert isinstance(filters, list)
    assert filters[0] == f"error::DeprecationWarning:{_SDK}"
    for entry in filters[1:]:
        action, message, category, module = entry.split(":")
        assert (action, category) == ("default", "DeprecationWarning"), entry
        if module == _SDK:
            assert message, entry
        else:
            assert module.startswith("application_sdk\\."), entry


def test_allowlisted_sdk_notice_only_warns() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.filterwarnings("always", category=DeprecationWarning, append=True)
        warnings.warn_explicit(
            "PreflightStatus.PARTIAL is deprecated; use READY",
            DeprecationWarning,
            "somewhere.py",
            1,
            module="application_sdk.handler.service",
        )
    assert [w.category for w in caught] == [DeprecationWarning]
