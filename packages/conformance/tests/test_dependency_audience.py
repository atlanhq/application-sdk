"""E021 — a DEPENDENCY_UNAVAILABLE error with its audience overridden to USER."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conformance.suite.checks.error_handling import scan_path, scan_text
from conformance.suite.checks.error_handling._dependency_category import (
    SDK_DEPENDENCY_UNAVAILABLE_CLASSES,
)

_HEADER = """\
from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import Audience
"""


def _e021_lines(src: str) -> list[int]:
    return [f.line for f in scan_text(src, "app/failures.py") if f.rule_id == "E021"]


def _scan_repo(tmp_path: Path, files: dict[str, str], target: str) -> list[str]:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return sorted(
        f.rule_id for f in scan_path(tmp_path / target, tmp_path) if f.rule_id == "E021"
    )


# ---------------------------------------------------------------------------
# Flagged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "assignment",
    [
        "audience: ClassVar[Audience] = Audience.USER",
        "audience = Audience.USER",
    ],
)
def test_flags_dependency_unavailable_leaf_overridden_to_user(assignment) -> None:
    src = (
        _HEADER + "from application_sdk.errors import DependencyUnavailableError\n\n\n"
        "@dataclass(kw_only=True)\n"
        "class SourceUnreachable(DependencyUnavailableError):\n"
        '    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_NETWORK"\n'
        f"    {assignment}\n"
    )
    assert _e021_lines(src) == [src.splitlines().index(f"    {assignment}") + 1]


@pytest.mark.parametrize(
    ("module", "leaf"),
    [
        ("application_sdk.clients.azure.azure_errors", "AzureConnectionError"),
        ("application_sdk.storage.errors", "StorageError"),
        ("application_sdk.storage", "StorageError"),
        ("application_sdk.errors", "ObjectStoreReadError"),
    ],
)
def test_flags_sdk_dependency_subclass_outside_errors_package(module, leaf) -> None:
    src = (
        _HEADER + f"from {module} import {leaf}\n\n\n"
        f"class CustomerStoreDown({leaf}):\n"
        "    audience: ClassVar[Audience] = Audience.USER\n"
    )
    assert len(_e021_lines(src)) == 1


def test_flags_module_alias_base() -> None:
    src = (
        _HEADER + "from application_sdk import errors\n\n\n"
        "class SourceDown(errors.DependencyUnavailableError):\n"
        "    audience = errors.Audience.USER\n"
    )
    assert len(_e021_lines(src)) == 1


def test_flags_app_intermediate_in_same_module_only_where_override_is_set() -> None:
    src = (
        _HEADER + "from application_sdk.errors import DependencyUnavailableError\n\n\n"
        "class AppDependencyError(DependencyUnavailableError):\n"
        "    pass\n\n\n"
        "class SourceDown(AppDependencyError):\n"
        "    audience = Audience.USER\n\n\n"
        "class SourceDownLeaf(SourceDown):\n"
        '    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_LEAF"\n'
    )
    assert len(_e021_lines(src)) == 1


def test_flags_mixin_before_the_error_base() -> None:
    """A non-error mixin first in the bases does not decide the category."""
    src = (
        _HEADER + "from application_sdk.errors import DependencyUnavailableError\n\n\n"
        "class ReprMixin:\n    pass\n\n\n"
        "class SourceDown(ReprMixin, DependencyUnavailableError):\n"
        "    audience = Audience.USER\n"
    )
    assert len(_e021_lines(src)) == 1


@pytest.mark.parametrize("relative", [False, True])
def test_follows_an_app_intermediate_in_another_module(tmp_path, relative) -> None:
    importer = "from .base" if relative else "from app.base"
    files = {
        "app/__init__.py": "",
        "app/base.py": (
            "from application_sdk.errors import DependencyUnavailableError\n\n\n"
            "class AppDependencyError(DependencyUnavailableError):\n    pass\n"
        ),
        "app/failures.py": (
            _HEADER + f"{importer} import AppDependencyError\n\n\n"
            "class SourceDown(AppDependencyError):\n"
            "    audience = Audience.USER\n"
        ),
    }
    assert _scan_repo(tmp_path, files, "app/failures.py") == ["E021"]


# ---------------------------------------------------------------------------
# Not flagged
# ---------------------------------------------------------------------------


def test_source_unavailable_leaf_is_clean() -> None:
    src = (
        _HEADER + "from application_sdk.errors import SourceUnavailableError\n\n\n"
        "class SourceDown(SourceUnavailableError):\n"
        '    code: ClassVar[str] = "SOURCE_UNAVAILABLE_NETWORK"\n'
    )
    assert _e021_lines(src) == []


def test_dependency_unavailable_leaf_without_override_is_clean() -> None:
    src = (
        _HEADER + "from application_sdk.errors import DependencyUnavailableError\n\n\n"
        "class ObjectStoreDown(DependencyUnavailableError):\n"
        '    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_OBJECT_STORE"\n'
    )
    assert _e021_lines(src) == []


@pytest.mark.parametrize("audience", ["APP_OWNER", "PLATFORM"])
def test_other_audience_override_is_clean(audience) -> None:
    src = (
        _HEADER + "from application_sdk.errors import DependencyUnavailableError\n\n\n"
        "class Down(DependencyUnavailableError):\n"
        f"    audience = Audience.{audience}\n"
    )
    assert _e021_lines(src) == []


def test_timeout_overridden_to_user_is_clean() -> None:
    """The sanctioned override: a source-side timeout routed to USER."""
    src = (
        _HEADER + "from application_sdk.errors import AppTimeoutError\n\n\n"
        "class SourceQueryTimeout(AppTimeoutError):\n"
        "    audience = Audience.USER\n"
    )
    assert _e021_lines(src) == []


def test_categorical_parent_first_decides_the_category() -> None:
    """``(NotFoundError, StorageError)`` is NOT_FOUND by MRO, like the SDK's own
    StorageNotFoundError; a USER audience on it is legitimate."""
    src = (
        _HEADER + "from application_sdk.errors import NotFoundError\n"
        "from application_sdk.storage.errors import StorageError\n\n\n"
        "class BucketMissing(NotFoundError, StorageError):\n"
        "    audience = Audience.USER\n"
    )
    assert _e021_lines(src) == []


def test_own_category_assignment_wins() -> None:
    src = (
        _HEADER
        + "from application_sdk.errors import DependencyUnavailableError, FailureCategory\n\n\n"
        "class Recategorised(DependencyUnavailableError):\n"
        "    category = FailureCategory.SOURCE_UNAVAILABLE\n"
        "    audience = Audience.USER\n"
    )
    assert _e021_lines(src) == []


def test_unresolvable_app_base_stays_unflagged() -> None:
    """Without a repo root, an imported app base cannot be proven."""
    src = (
        _HEADER + "from app.base import AppDependencyError\n\n\n"
        "class SourceDown(AppDependencyError):\n"
        "    audience = Audience.USER\n"
    )
    assert _e021_lines(src) == []


def test_non_sdk_class_with_the_same_name_is_clean() -> None:
    src = (
        _HEADER + "from vendor.errors import DependencyUnavailableError\n\n\n"
        "class SourceDown(DependencyUnavailableError):\n"
        "    audience = Audience.USER\n"
    )
    assert _e021_lines(src) == []


def test_inline_suppression() -> None:
    src = (
        _HEADER + "from application_sdk.errors import DependencyUnavailableError\n\n\n"
        "class SourceDown(DependencyUnavailableError):\n"
        "    # conformance: ignore[E021] migration tracked separately\n"
        "    audience = Audience.USER\n"
    )
    findings = [f for f in scan_text(src, "x.py") if f.rule_id == "E021"]
    assert all(f.suppressed for f in findings)


# ---------------------------------------------------------------------------
# Drift: the SDK allowlist equals a fresh read of the SDK source
# ---------------------------------------------------------------------------

_SDK_PACKAGE_RELPATH = ("application_sdk", "errors", "leaves.py")


def _find_sdk_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if parent.joinpath(*_SDK_PACKAGE_RELPATH).is_file():
            return parent
    return None


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _sdk_dependency_unavailable_classes(sdk_root: Path) -> set[str]:
    """Classes under application_sdk/ (minus testing/) whose MRO category is
    DEPENDENCY_UNAVAILABLE, by fixed point over class names."""
    package = sdk_root / "application_sdk"
    classes: dict[str, ast.ClassDef] = {}
    for path in sorted(package.rglob("*.py")):
        if "testing" in path.relative_to(package).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, node)

    # category per class name: True = DEPENDENCY_UNAVAILABLE, False = other error.
    verdict: dict[str, bool] = {"AppError": False}
    changed = True
    while changed:
        changed = False
        for name, node in classes.items():
            if name in verdict:
                continue
            own = next(
                (
                    stmt.value
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign | ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "category"
                        for t in (
                            [stmt.target]
                            if isinstance(stmt, ast.AnnAssign)
                            else stmt.targets
                        )
                    )
                ),
                None,
            )
            if isinstance(own, ast.Attribute):
                verdict[name] = own.attr == "DEPENDENCY_UNAVAILABLE"
                changed = True
                continue
            for base in node.bases:
                base_name = _base_name(base)
                if base_name in verdict:
                    verdict[name] = verdict[base_name]
                    changed = True
                    break
                if base_name in classes:
                    break  # undecided earlier base: wait, never skip past it
    return {name for name, is_dependency in verdict.items() if is_dependency}


def test_sdk_allowlist_matches_sdk_source() -> None:
    sdk_root = _find_sdk_root()
    if sdk_root is None:
        pytest.skip("SDK source not on disk — allowlist drift cannot be checked here.")

    expected = _sdk_dependency_unavailable_classes(sdk_root)

    assert set(SDK_DEPENDENCY_UNAVAILABLE_CLASSES) == expected, (
        "SDK_DEPENDENCY_UNAVAILABLE_CLASSES is stale. Missing: "
        f"{sorted(expected - SDK_DEPENDENCY_UNAVAILABLE_CLASSES)}; extra: "
        f"{sorted(SDK_DEPENDENCY_UNAVAILABLE_CLASSES - expected)}"
    )
