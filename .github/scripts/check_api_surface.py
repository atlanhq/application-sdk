#!/usr/bin/env python3
"""Fail when a file shipped in ``atlan-application-sdk-api`` reaches worker code.

The api distribution is a listed subset of ``application_sdk/`` (see
``packages/api/api-files.txt``) that the consolidated API host installs without
the worker SDK. A listed file that imports an unlisted ``application_sdk`` module,
or a third-party package the api distribution does not declare, imports fine in
the SDK's own env and then fails on the host. This check reads every listed
file's imports and fails on:

* a listed file that does not exist;
* a listed module whose parent package ``__init__.py`` is not listed (Python
  imports the parent first);
* a module-level import (outside ``if TYPE_CHECKING:``) of an unlisted
  ``application_sdk`` module;
* a function-level import of an unlisted ``application_sdk`` module (or of a
  third-party package the api distribution does not declare) that is not inside
  a ``try`` naming ``ModuleNotFoundError`` (or ``ImportError``) in an ``except`` — on the host such an
  import raises at call time, so it must have a stated fallback;
* a module-level import of a third-party package the api distribution does not
  declare as a dependency (a function-level import of one of its extras is fine:
  the handler that needs it declares the extra, as it does on the worker);
* an import of an ``application_sdk`` module that does not exist at all.

Usage (from the repo root)::

    python3 .github/scripts/check_api_surface.py
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

API_DIR = Path("packages/api")
LIST_FILE = API_DIR / "api-files.txt"

#: Import names that a declared requirement provides under another name, or
#: that come with one (pydantic brings pydantic_core, fastapi brings starlette).
PROVIDED_BY = {
    "pydantic_core": "pydantic",
    "starlette": "fastapi",
    "typing_extensions": "pydantic",
    "dotenv": "python_dotenv",
    "opentelemetry": "opentelemetry_sdk",
    "certifi": "httpx",
    "botocore": "boto3",
}


@dataclass(frozen=True)
class Problem:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def listed_files(root: Path) -> list[str]:
    lines = (root / LIST_FILE).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def _names(reqs: list[str]) -> set[str]:
    names = set()
    for req in reqs:
        match = re.match(r"[A-Za-z0-9_.\-]+", req)
        if match:
            names.add(match.group(0).lower().replace("-", "_"))
    return names


def declared_requirements(root: Path) -> set[str]:
    project = tomllib.loads((root / API_DIR / "pyproject.toml").read_text())["project"]
    return _names(project.get("dependencies", []))


def optional_requirements(root: Path) -> set[str]:
    """Packages behind an extra: importable lazily, never at module level."""
    project = tomllib.loads((root / API_DIR / "pyproject.toml").read_text())["project"]
    extras = project.get("optional-dependencies", {})
    return _names([r for reqs in extras.values() for r in reqs])


def module_of(rel: str) -> str:
    return rel.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def file_of(root: Path, module: str) -> str | None:
    base = module.replace(".", "/")
    for rel in (f"{base}/__init__.py", f"{base}.py"):
        if (root / rel).is_file():
            return rel
    return None


def _is_type_checking(test: ast.expr) -> bool:
    return "TYPE_CHECKING" in ast.unparse(test)


def _catches_module_not_found(node: ast.Try) -> bool:
    for handler in node.handlers:
        names = (
            handler.type.elts
            if isinstance(handler.type, ast.Tuple)
            else [handler.type]
            if handler.type is not None
            else []
        )
        if any(
            isinstance(n, ast.Name) and n.id in ("ModuleNotFoundError", "ImportError")
            for n in names
        ):
            return True
    return False


def _imports(tree: ast.Module, module: str, is_package: bool):
    """Yield ``(node, module, at module level, guarded, required)``.

    ``required`` is False for the ``pkg.name`` candidates of ``from pkg import
    name``, where ``name`` may be an attribute rather than a submodule.
    """
    package = module if is_package else module.rpartition(".")[0]

    def resolve(node: ast.ImportFrom) -> list[str]:
        base = node.module or ""
        if node.level:
            parts = package.split(".")
            parts = parts[: len(parts) - (node.level - 1)]
            base = ".".join([*parts, base] if base else parts)
        # ``from pkg import name`` may import the submodule ``pkg.name``.
        return [base, *(f"{base}.{a.name}" for a in node.names if a.name != "*")]

    def walk(nodes, top: bool, guarded: bool):
        for node in nodes:
            if isinstance(node, ast.If) and _is_type_checking(node.test):
                yield from walk(node.orelse, top, guarded)
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield node, alias.name, top, guarded, True
            elif isinstance(node, ast.ImportFrom):
                base, *candidates = resolve(node)
                yield node, base, top, guarded, True
                for name in candidates:
                    yield node, name, top, guarded, False
            inner_top = top and not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            )
            if isinstance(node, ast.Try):
                body_guard = guarded or _catches_module_not_found(node)
                yield from walk(node.body, inner_top, body_guard)
                for handler in node.handlers:
                    yield from walk(handler.body, inner_top, guarded)
                yield from walk(node.orelse, inner_top, guarded)
                yield from walk(node.finalbody, inner_top, guarded)
                continue
            for field in ("body", "orelse", "handlers", "finalbody"):
                child = getattr(node, field, None)
                if isinstance(child, list):
                    yield from walk(child, inner_top, guarded)

    yield from walk(tree.body, True, False)


def check(root: Path) -> list[Problem]:
    listed = listed_files(root)
    listed_set = set(listed)
    declared = declared_requirements(root)
    optional = optional_requirements(root)
    problems: list[Problem] = []
    for rel in listed:
        path = root / rel
        if not path.is_file():
            problems.append(
                Problem(str(LIST_FILE), 1, f"listed file {rel} does not exist")
            )
            continue
        module = module_of(rel)
        parts = module.split(".")
        for i in range(1, len(parts)):
            parent = file_of(root, ".".join(parts[:i]))
            if (
                parent is not None
                and parent.endswith("__init__.py")
                and parent not in listed_set
            ):
                problems.append(
                    Problem(rel, 1, f"parent package {parent} is not listed")
                )
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        reported: set[int] = set()  # one problem per import statement
        for node, name, top, guarded, required in _imports(
            tree, module, rel.endswith("__init__.py")
        ):
            if id(node) in reported:
                continue
            before = len(problems)
            root_name = name.split(".")[0]
            if root_name == "application_sdk":
                target = file_of(root, name)
                if target is None and required:
                    problems.append(
                        Problem(
                            rel, node.lineno, f"imports {name}, which does not exist"
                        )
                    )
                    reported.add(id(node))
                    continue
                if target is None or target in listed_set:
                    continue  # a name inside a listed module, or a listed module
                if top:
                    problems.append(
                        Problem(
                            rel,
                            node.lineno,
                            f"imports {name}, which is not in {LIST_FILE}",
                        )
                    )
                elif not guarded:
                    problems.append(
                        Problem(
                            rel,
                            node.lineno,
                            f"lazily imports {name} (not in {LIST_FILE}) without a "
                            "try/except ModuleNotFoundError fallback",
                        )
                    )
                if len(problems) > before:
                    reported.add(id(node))
                continue
            if root_name in sys.stdlib_module_names or root_name == "__future__":
                continue
            provider = PROVIDED_BY.get(root_name, root_name).lower()
            if provider in declared:
                continue
            if not top and provider in optional:
                continue  # a lazy import of an extra, as on the worker
            if top or not guarded:
                problems.append(
                    Problem(
                        rel,
                        node.lineno,
                        f"imports {root_name}, which {API_DIR}/pyproject.toml does not declare",
                    )
                )
            if len(problems) > before:
                reported.add(id(node))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    problems = check(args.root)
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print(f"api surface: {len(listed_files(args.root))} files, closure clean")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
