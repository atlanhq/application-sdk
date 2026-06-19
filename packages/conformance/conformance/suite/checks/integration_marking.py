"""T001 UnmarkedIntegrationTest — flag unmarked tests under ``tests/integration/``.

The unit/integration split in CI is enforced purely by pytest markers: the unit
job runs with a deselection filter (``-m 'not integration and not e2e and …'``,
declared in ``pyproject.toml`` ``addopts``) and the dedicated integration jobs
select with ``-m integration`` / ``-m s3_integration`` / ….  A test under
``tests/integration/`` that carries **none** of the unit-job-deselected markers
is therefore run in the *unit* job (where an embedded Temporal/Dapr boot can
exceed the tight unit timeout) instead of the integration job built for it.

So the real invariant is not "marked ``integration``" but **"carries a marker
that keeps it out of the unit job."**  The accepted set is derived per-repo from
the ``-m`` deselection expression in the app's own ``pyproject.toml`` ``addopts``
(so an app with its own ``kafka_integration`` marker self-calibrates); it falls
back to ``{"integration"}`` when no such expression is found.

This checker mirrors pytest's collection + marker hierarchy: a test is considered
marked when the **module** declares ``pytestmark`` containing an accepted marker
(bare or in a list/tuple), when its **enclosing ``Test*`` class** carries one
(either as a class decorator or as a ``pytestmark`` in the class body), or when
the **test function/method itself** carries one.  One finding is emitted per
unmarked test item.

Discovery
---------
The shared ``_ast_common.discover`` deliberately excludes ``tests/`` (it scans
shipped application code), so this series ships its own :func:`discover` that
walks ``tests/integration/`` for pytest's default test files (``test_*.py`` /
``*_test.py``).

Inline suppression
------------------
Add ``# conformance: ignore[T001] <reason>`` on the offending ``def`` line or the
comment-only line directly above it.

Known coverage limits (intentional — biased toward zero false positives at WARN
tier; documented rather than silently capped):

* **Marker resolution is syntactic.** ``pytest.mark.<name>`` /
  ``<alias>.mark.<name>`` (e.g. ``import pytest as pt``) and ``mark.<name>``
  (``from pytest import mark``) are recognised, in both the attribute and called
  (``…<name>()``) forms.  Dynamically applied markers (``add_marker`` in a
  fixture, ``pytest.param(..., marks=...)``, an aliased
  ``integration = pytest.mark.integration``) are **not** resolved.
* **Accepted-marker derivation is syntactic.** The ``not <name>`` tokens are
  extracted from ``addopts`` when it contains a ``-m`` expression; a non-standard
  selection expression that doesn't use ``not`` falls back to ``{"integration"}``.
* **Collection mirrors pytest defaults** (``test*`` functions, ``Test*`` classes,
  ``test_*.py`` / ``*_test.py`` files).  Non-default ``python_*`` overrides are
  not honoured.
"""

from __future__ import annotations

import ast
import re
import shlex
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import TypeGuard

from conformance.suite.checks._ast_common import (
    _parse_directives,
    make_cli_main,
    make_finding,
)
from conformance.suite.schema.findings import Finding

SERIES = "T"
RULE_T001 = "T001"

# The marker assumed acceptable when an app declares no ``-m`` deselection.
_DEFAULT_ACCEPTED_MARKERS: frozenset[str] = frozenset({"integration"})

# ``not <marker>`` inside a pytest ``-m`` expression.
_NOT_MARKER_RE = re.compile(r"\bnot\s+([A-Za-z_]\w*)")

__all__ = [
    "SERIES",
    "accepted_markers_for_repo",
    "discover",
    "main",
    "scan_path",
    "scan_text",
]


# ---------------------------------------------------------------------------
# Accepted-marker derivation (from pyproject.toml addopts)
# ---------------------------------------------------------------------------


def _marker_expression(addopts: object) -> str | None:
    """Return the value of the pytest ``-m`` flag in an ``addopts`` value, if any.

    ``addopts`` may be a string (``"-m 'not a and not b'"``) or a list of tokens
    (``["-m", "not a and not b"]``).  The string form is split with ``shlex`` so a
    quoted expression survives as one token; the list form is already tokenised by
    the TOML author.  Both ``-m <expr>`` (two tokens) and ``-m<expr>`` (glued) are
    handled.  Returns ``None`` when there is no ``-m`` flag.
    """
    if addopts is None:
        return None
    if isinstance(addopts, list):
        tokens = [str(t) for t in addopts]
    else:
        try:
            tokens = shlex.split(str(addopts))
        except ValueError:
            tokens = str(addopts).split()
    for i, tok in enumerate(tokens):
        if tok == "-m":
            return tokens[i + 1] if i + 1 < len(tokens) else None
        if tok.startswith("-m") and len(tok) > 2:
            return tok[2:]
    return None


def _deselected_markers(addopts: object) -> frozenset[str]:
    """Return the ``not <marker>`` names from a pytest ``addopts`` value.

    Only the ``-m`` selection expression is scanned (not the whole ``addopts``
    text), so a stray ``not <ident>`` in some other flag — e.g. a
    ``--reason 'not enough memory'`` — can't leak in as an accepted marker.
    Returns an empty set when there is no ``-m`` expression (caller falls back to
    the default).
    """
    expr = _marker_expression(addopts)
    if expr is None:
        return frozenset()
    return frozenset(_NOT_MARKER_RE.findall(expr))


def accepted_markers_for_repo(root: Path) -> frozenset[str]:
    """Derive the set of markers that keep a test out of the unit job.

    Reads ``[tool.pytest.ini_options].addopts`` from ``root/pyproject.toml`` and
    extracts the ``-m 'not …'`` deselection set.  Falls back to
    ``{"integration"}`` when the file is missing/unparseable or declares no such
    expression.
    """
    pyproject = root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return _DEFAULT_ACCEPTED_MARKERS
    addopts = (
        data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts")
        if isinstance(data, dict)
        else None
    )
    markers = _deselected_markers(addopts)
    return markers or _DEFAULT_ACCEPTED_MARKERS


# ---------------------------------------------------------------------------
# Marker resolution
# ---------------------------------------------------------------------------


def _marker_name(node: ast.expr) -> str | None:
    """Return the marker name for ``…mark.<name>`` (optionally called), else None.

    Accepts ``pytest.mark.<name>`` / ``<alias>.mark.<name>`` and the
    ``from pytest import mark`` form ``mark.<name>``, in both bare-attribute and
    called (``…<name>()``) shapes.
    """
    if isinstance(node, ast.Call):
        node = node.func
    if not isinstance(node, ast.Attribute):
        return None
    parent = node.value
    # ``<anything>.mark.<name>`` — e.g. pytest.mark.integration, pt.mark.s3_integration
    if isinstance(parent, ast.Attribute) and parent.attr == "mark":
        return node.attr
    # ``mark.<name>`` — from pytest import mark
    if isinstance(parent, ast.Name) and parent.id == "mark":
        return node.attr
    return None


def _pytestmark_value_markers(value: ast.expr) -> set[str]:
    """Marker names referenced by a ``pytestmark`` value (single or list/tuple)."""
    if isinstance(value, (ast.List, ast.Tuple)):
        return {name for elt in value.elts if (name := _marker_name(elt))}
    name = _marker_name(value)
    return {name} if name else set()


def _pytestmark_markers_in_body(body: list[ast.stmt]) -> set[str]:
    """All marker names in ``pytestmark`` assignments directly in *body*.

    Covers both ``pytestmark = pytest.mark.integration`` and
    ``pytestmark = [pytest.mark.integration, …]`` (and the annotated form).  Used
    for both the module body and a ``Test*`` class body — the latter is a
    documented pytest idiom equivalent to decorating the class.
    """
    names: set[str] = set()
    for node in body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            names |= _pytestmark_value_markers(value)
    return names


def _decorator_markers(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> set[str]:
    return {name for d in node.decorator_list if (name := _marker_name(d))}


# ---------------------------------------------------------------------------
# pytest collection mirror (default conventions)
# ---------------------------------------------------------------------------


def _is_test_function(
    node: ast.stmt,
) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    """True for a pytest-collected test function (default ``python_functions``)."""
    return isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ) and node.name.startswith("test")


def _is_test_class(node: ast.stmt) -> TypeGuard[ast.ClassDef]:
    """True for a pytest-collected test class (default ``python_classes``)."""
    return isinstance(node, ast.ClassDef) and node.name.startswith("Test")


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------

_HINT = (
    "Add such a marker on the test, its enclosing class, or a module-level "
    "`pytestmark`, so it is deselected from the unit job and selected by the "
    "integration job."
)


def scan_text(
    text: str,
    file: str,
    *,
    accepted_markers: Iterable[str] | None = None,
) -> list[Finding]:
    """Scan a Python test file *text* and return all T-series findings.

    Parameters
    ----------
    text, file:
        Source and its repo-root-relative URI.
    accepted_markers:
        Markers that keep a test out of the unit job.  When ``None`` (the
        default — used by the standalone CLI), ``{"integration"}`` is assumed;
        :func:`scan_path` derives the real set from the repo's ``pyproject.toml``.
    """
    accepted = (
        frozenset(accepted_markers)
        if accepted_markers is not None
        else _DEFAULT_ACCEPTED_MARKERS
    )
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []

    # A module-level accepted marker covers every test in the file.
    if _pytestmark_markers_in_body(tree.body) & accepted:
        return []

    directives = _parse_directives(text)
    findings: list[Finding] = []

    def _message(label: str, extra: str = "") -> str:
        accepted_hint = (
            f" (accepted markers: {', '.join(sorted(accepted))})" if accepted else ""
        )
        return (
            f"Test '{label}' under tests/integration/ is not marked with a pytest "
            f"marker that deselects it from the unit job{accepted_hint}.{extra} "
            f"{_HINT}"
        )

    for node in tree.body:
        if _is_test_function(node):
            if not (_decorator_markers(node) & accepted):
                findings.append(
                    make_finding(
                        filename=file,
                        rule_id=RULE_T001,
                        node=node,
                        message=_message(node.name),
                        directives=directives,
                    )
                )
        elif _is_test_class(node):
            # A class-level marker covers every method it contains — whether
            # applied as a class decorator or as a `pytestmark` in the class body
            # (a documented pytest idiom equivalent to the decorator).
            class_markers = _decorator_markers(node) | _pytestmark_markers_in_body(
                node.body
            )
            if class_markers & accepted:
                continue
            for sub in node.body:
                if _is_test_function(sub) and not (_decorator_markers(sub) & accepted):
                    findings.append(
                        make_finding(
                            filename=file,
                            rule_id=RULE_T001,
                            node=sub,
                            message=_message(
                                f"{node.name}.{sub.name}",
                                extra=" (neither the method, its class, nor the"
                                " module is marked)",
                            ),
                            directives=directives,
                        )
                    )

    return findings


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single test file, producing repo-root-relative URIs.

    Derives the accepted-marker set from ``root/pyproject.toml`` so the check
    self-calibrates to the repo's own unit-job deselection filter.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel), accepted_markers=accepted_markers_for_repo(root))


def discover(root: Path) -> list[Path]:
    """Discover pytest test files under ``tests/integration/``.

    Returns ``[]`` when the directory is absent (the common case for app repos
    that have no integration suite), so the T-series simply no-ops there.
    """
    base = root / "tests" / "integration"
    if not base.is_dir():
        return []
    paths: list[Path] = []
    for path in base.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py"):
            paths.append(path)
    return sorted(paths)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

main = make_cli_main(
    scan_text,
    description="T001: scan tests/integration/ for tests missing an integration-family marker.",
    discover=discover,
    default_scan_paths=("tests/integration",),
)
"""CLI entry point for the T-series test-marking check."""


if __name__ == "__main__":
    sys.exit(main())
