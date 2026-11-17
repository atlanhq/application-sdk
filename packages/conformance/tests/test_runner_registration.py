"""Every check module under ``conformance/suite/checks`` is registered with the runner.

A check module is one that assigns ``SERIES`` at module level: a flat module,
a package (``SERIES`` in its ``__init__`` or a private ``_``-prefixed submodule
it re-exports), or a public submodule of a package, which is its own check.  Forgetting to add it to
``runner._CHECK_MODULES`` yields a rule that is documented and unit-tested but
never runs, so this test pins the two sets equal.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conformance.suite import runner
from conformance.suite.runner import _CHECK_MODULES, _CHECKS, CheckRegistration
from conformance.suite.schema.findings import Finding

_CHECKS_ROOT = Path(runner.__file__).resolve().parent / "checks"


def _assigns_series(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "SERIES" for t in targets):
            return True
    return False


def _check_module_name(path: Path, checks_root: Path) -> str:
    """Dotted name, relative to *checks_root*, of the check module *path* belongs to.

    A package's ``__init__.py`` is the package.  A private (``_``-prefixed)
    submodule belongs to its package, which re-exports its ``SERIES`` (e.g.
    ``error_handling/_constants.py``).  Any other file is its own check module,
    so a public ``pkg/new_check.py`` that assigns ``SERIES`` must be registered
    itself, not hidden behind an already-registered ``pkg``.
    """
    parts = list(path.relative_to(checks_root).with_suffix("").parts)
    if parts[-1] == "__init__" or parts[-1].startswith("_"):
        parts.pop()
    return ".".join(parts)


def _check_modules_on_disk(checks_root: Path = _CHECKS_ROOT) -> set[str]:
    return {
        _check_module_name(path, checks_root)
        for path in checks_root.rglob("*.py")
        if "__pycache__" not in path.relative_to(checks_root).parts
        and _assigns_series(path)
    }


def _registered() -> set[str]:
    prefix = "conformance.suite.checks."
    return {module.__name__.removeprefix(prefix) for module in _CHECK_MODULES}


def test_guard_sees_a_public_check_module_inside_a_registered_package(
    tmp_path: Path,
) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('SERIES = "Z"\n')
    (pkg / "_constants.py").write_text('SERIES = "Z"\n')
    (pkg / "new_check.py").write_text('SERIES = "Y"\n')
    (tmp_path / "flat.py").write_text('SERIES: str = "X"\n')
    (tmp_path / "helper.py").write_text("x = 1\n")

    assert _check_modules_on_disk(tmp_path) == {"pkg", "pkg.new_check", "flat"}


def test_every_check_module_is_registered() -> None:
    on_disk = _check_modules_on_disk()
    missing = sorted(on_disk - _registered())
    assert not missing, (
        f"check modules define SERIES but are not in runner._CHECK_MODULES: {missing}"
    )


def test_every_registered_module_is_a_check_module() -> None:
    stray = sorted(_registered() - _check_modules_on_disk())
    assert not stray, f"runner._CHECK_MODULES lists non-check modules: {stray}"


def test_no_module_is_registered_twice() -> None:
    names = [module.__name__ for module in _CHECK_MODULES]
    assert len(names) == len(set(names))


def test_registrations_are_built_from_the_module_tuple() -> None:
    assert len(_CHECKS) == len(_CHECK_MODULES)
    for registration, module in zip(_CHECKS, _CHECK_MODULES, strict=True):
        assert registration.series == module.SERIES
        assert registration.discover is module.discover
        assert registration.scan_all is getattr(module, "scan_all", None)
        assert registration.scan_path is getattr(module, "scan_path", None)


def test_registration_requires_a_scan_entry_point() -> None:
    with pytest.raises(TypeError, match="neither scan_path nor scan_all"):
        CheckRegistration(series="Z", discover=lambda root: [])


def test_scan_prefers_scan_all_over_scan_path(tmp_path: Path) -> None:
    calls: list[str] = []

    def scan_path(path: Path, root: Path) -> list[Finding]:
        calls.append("scan_path")
        return []

    def scan_all(paths: list[Path], root: Path) -> list[Finding]:
        calls.append("scan_all")
        return []

    CheckRegistration(
        series="Z", discover=lambda root: [], scan_path=scan_path, scan_all=scan_all
    ).scan([tmp_path / "a.py", tmp_path / "b.py"], tmp_path)
    assert calls == ["scan_all"]


def test_scan_without_scan_all_calls_scan_path_per_file(tmp_path: Path) -> None:
    seen: list[Path] = []

    def scan_path(path: Path, root: Path) -> list[Finding]:
        seen.append(path)
        return []

    paths = [tmp_path / "a.py", tmp_path / "b.py"]
    CheckRegistration(series="Z", discover=lambda root: [], scan_path=scan_path).scan(
        paths, tmp_path
    )
    assert seen == paths
