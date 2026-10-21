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
* an import of an ``application_sdk`` module that does not exist at all;
* a **lazy export** of a listed module (a PEP 562 ``__getattr__`` over a
  ``name -> (module, attribute)`` map such as ``_LAZY``, or over the submodule
  names passed to ``_install.lazy_getattr``) whose target module is not listed
  either. Those targets are strings, so the import closure never sees them, and
  ``from application_sdk.credentials import expand_dotted_keys`` failed on the
  host because ``common.transforms`` was not shipped. A target that is
  deliberately worker-only is named in ``_WORKER_ONLY_PREFIXES``
  (``application_sdk/_install.py``) as an exact module, never a package that
  holds listed files; there it fails on the host with the ``AttributeError``
  ``lazy_getattr`` raises for a module that is not installed.

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
INSTALL_FILE = Path("application_sdk/_install.py")

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
    "pyatlan_v9": "pyatlan",
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


def extra_requirements(root: Path) -> dict[str, set[str]]:
    """``{extra: package names}`` from ``[project.optional-dependencies]``."""
    project = tomllib.loads((root / API_DIR / "pyproject.toml").read_text())["project"]
    return {k: _names(v) for k, v in project.get("optional-dependencies", {}).items()}


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
    """``if TYPE_CHECKING:`` or ``if typing.TYPE_CHECKING:`` and nothing else.

    A negated or compound test (``if not TYPE_CHECKING:``) runs its body, so its
    imports are runtime imports and are checked like any other.
    """
    return ast.unparse(test).strip() in ("TYPE_CHECKING", "typing.TYPE_CHECKING")


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


def worker_only_prefixes(root: Path) -> tuple[str, ...]:
    """``_WORKER_ONLY_PREFIXES`` from ``application_sdk/_install.py`` (read, not imported)."""
    path = root / INSTALL_FILE
    if not path.is_file():
        return ()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == "_WORKER_ONLY_PREFIXES"
            and node.value is not None
        ):
            return tuple(ast.literal_eval(node.value))
    return ()


def _is_worker_only(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _sdk_module(value: ast.expr) -> str | None:
    """The module of one lazy-map value: ``"mod"`` or ``("mod", "attr")``."""
    if isinstance(value, ast.Tuple) and value.elts:
        value = value.elts[0]
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        if value.value == "application_sdk" or value.value.startswith(
            "application_sdk."
        ):
            return value.value
    return None


def lazy_exports(
    tree: ast.Module, module: str, is_package: bool
) -> list[tuple[int, str, str]]:
    """``(line, exported name, target module)`` for each lazy export of a module.

    Only a module that defines a module-level ``__getattr__`` exports lazily. Its
    targets are (1) every module-level dict whose values name an
    ``application_sdk`` module, as ``"module"`` or ``("module", "attribute")``,
    and (2) for a package, the submodule names it passes to
    ``lazy_getattr(..., submodules)`` (the 5th argument or ``submodules=``).
    """
    if not any(
        isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in tree.body
    ):
        return []
    assigned: dict[str, ast.expr] = {}
    found: list[tuple[int, str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if value is None:
            continue
        assigned[name] = value
        if isinstance(value, ast.Dict):
            for key, item in zip(value.keys, value.values):
                target = _sdk_module(item)
                if target is not None:
                    label = (
                        key.value
                        if isinstance(key, ast.Constant)
                        else ast.unparse(key)
                        if key is not None
                        else "**"
                    )
                    found.append((item.lineno, str(label), target))
    package = module if is_package else module.rpartition(".")[0]
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "lazy_getattr"
        ):
            continue
        arg = call.args[4] if len(call.args) > 4 else None
        for kw in call.keywords:
            if kw.arg == "submodules":
                arg = kw.value
        if isinstance(arg, ast.Name):
            arg = assigned.get(arg.id)
        if isinstance(arg, ast.Call) and arg.args:  # frozenset({...}) / set([...])
            arg = arg.args[0]
        if not isinstance(arg, (ast.Set, ast.List, ast.Tuple)):
            continue
        for elt in arg.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                found.append((elt.lineno, elt.value, f"{package}.{elt.value}"))
    return found


def _check_lazy_exports(
    root: Path,
    rel: str,
    tree: ast.Module,
    listed: set[str],
    prefixes: tuple[str, ...],
) -> list[Problem]:
    problems = []
    for line, name, target in lazy_exports(
        tree, module_of(rel), rel.endswith("__init__.py")
    ):
        target_file = file_of(root, target)
        if target_file is None:
            problems.append(
                Problem(
                    rel,
                    line,
                    f"lazily exports {name} from {target}, which does not exist",
                )
            )
        elif target_file in listed or _is_worker_only(target, prefixes):
            continue  # shipped (in some group), or deliberately worker-only
        else:
            problems.append(
                Problem(
                    rel,
                    line,
                    f"lazily exports {name} from {target}, which is not in {LIST_FILE}: "
                    "seed it in packages/api/pyproject.toml, or, if it is worker-only, "
                    f"name the module in _WORKER_ONLY_PREFIXES ({INSTALL_FILE})",
                )
            )
    return problems


def check(root: Path) -> list[Problem]:
    listed = listed_files(root)
    listed_set = set(listed)
    declared = declared_requirements(root)
    optional = optional_requirements(root)
    extras = extra_requirements(root)
    import gen_api_files  # noqa: PLC0415 — sibling; imports this module

    groups = gen_api_files.file_groups(root)
    prefixes = worker_only_prefixes(root)
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
        problems.extend(_check_lazy_exports(root, rel, tree, listed_set, prefixes))
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
            reached_by = groups.get(rel, {"core"})
            if top and "core" not in reached_by:
                allowed = set().union(*(extras.get(g, set()) for g in reached_by))
                if provider in allowed:
                    continue
                problems.append(
                    Problem(
                        rel,
                        node.lineno,
                        f"imports {root_name} at module level; it is reached from seed "
                        f"group(s) {', '.join(sorted(reached_by))}, whose extra does not "
                        "declare it",
                    )
                )
                reported.add(id(node))
                continue
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
