#!/usr/bin/env python3
"""Fail CI when an ``application_sdk`` re-export shim holds real code.

The error taxonomy and the handler surface live in ``packages/api``
(``atlan-application-sdk-api``). Their old ``application_sdk`` import paths are
kept as shims that resolve every name to the object in ``application_sdk_api``,
so there is exactly one implementation of each class. A shim that grows a class,
a function or a constant is a second copy that will drift, so this guard rejects
any statement a shim does not need.

A shim may contain only:

* a module docstring and ``from __future__ import annotations``;
* imports (``import x as _y``, ``from typing import ...``, ``from
  application_sdk_api... import *``);
* an ``if TYPE_CHECKING:`` block holding only imports;
* assignments to ``__all__``, ``_EXTRA`` and ``_NOT_DEPRECATED``;
* the ``__getattr__`` and ``__dir__`` functions.

Usage:
    python3 guard_api_shims.py [--root <repo root>]

Exits 1 and prints each offending statement when a shim holds anything else, or
when a module listed in :data:`SHIM_MODULES` is missing.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: Every ``application_sdk`` module whose implementation moved to the api package.
SHIM_MODULES: tuple[str, ...] = (
    "application_sdk/errors/__init__.py",
    "application_sdk/errors/base.py",
    "application_sdk/errors/categories.py",
    "application_sdk/errors/leaves.py",
    "application_sdk/errors/wire.py",
    "application_sdk/credentials/errors.py",
    "application_sdk/credentials/spec.py",
    "application_sdk/credentials/ingress.py",
    "application_sdk/handler/__init__.py",
    "application_sdk/handler/base.py",
    "application_sdk/handler/contracts.py",
    "application_sdk/handler/context.py",
    "application_sdk/handler/manifest.py",
    "application_sdk/handler/service_errors.py",
)

_ALLOWED_ASSIGN_TARGETS = frozenset({"__all__", "_EXTRA", "_NOT_DEPRECATED"})
_ALLOWED_FUNCTIONS = frozenset({"__getattr__", "__dir__"})


def _is_type_checking_test(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in {"TYPE_CHECKING", "_TYPE_CHECKING"}


def _assign_targets(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        return [t.id if isinstance(t, ast.Name) else "<complex>" for t in node.targets]
    if isinstance(node, ast.AnnAssign):
        return [node.target.id if isinstance(node.target, ast.Name) else "<complex>"]
    return []


def violations(source: str) -> list[str]:
    """Return one message per statement a shim is not allowed to hold."""
    found: list[str] = []
    tree = ast.parse(source)
    for index, node in enumerate(tree.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue  # module docstring
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            if (
                all(isinstance(n, (ast.Import, ast.ImportFrom)) for n in node.body)
                and not node.orelse
            ):
                continue
            found.append(
                f"line {node.lineno}: TYPE_CHECKING block may hold only imports"
            )
            continue
        targets = _assign_targets(node)
        if targets:
            bad = [t for t in targets if t not in _ALLOWED_ASSIGN_TARGETS]
            if not bad:
                continue
            found.append(f"line {node.lineno}: assignment to {', '.join(bad)}")
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _ALLOWED_FUNCTIONS:
                continue
            found.append(f"line {node.lineno}: function {node.name}()")
            continue
        if isinstance(node, ast.ClassDef):
            found.append(f"line {node.lineno}: class {node.name}")
            continue
        found.append(f"line {node.lineno}: {type(node).__name__} statement")
    return found


def check(root: Path) -> list[str]:
    """Return every problem across :data:`SHIM_MODULES`, as printable lines."""
    problems: list[str] = []
    for rel in SHIM_MODULES:
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel}: missing (listed in SHIM_MODULES)")
            continue
        for message in violations(path.read_text(encoding="utf-8")):
            problems.append(f"{rel}: {message}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    problems = check(args.root)
    if problems:
        print(
            "These application_sdk modules are re-export shims for code that lives in "
            "packages/api. Make the change in packages/api instead:"
        )
        for line in problems:
            print(f"  {line}")
        return 1
    print(f"guard_api_shims: {len(SHIM_MODULES)} shims hold no code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
