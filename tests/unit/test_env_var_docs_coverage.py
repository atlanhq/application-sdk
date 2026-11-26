"""Every runtime env var the SDK reads is documented, and ``.env.example`` sets
only names the SDK reads.

``docs/standards/env-vars.md`` names ``docs/configuration.md`` as the home of every
runtime variable. Nothing enforced it, so new knobs landed undocumented and
``.env.example`` kept setting names the SDK stopped reading years ago. This test
collects env-var names from the source by AST and checks both directions.

How a name is collected:

- **Readers.** ``os.getenv``, ``os.environ.get`` / ``setdefault`` / ``pop``,
  ``os.environ[...]`` and ``... in os.environ`` read a key. Any function that
  forwards one of its parameters into a reader's key slot is itself a reader
  (``env_bool``, ``env_int``, local ``_bool`` helpers, ...), found by fixpoint,
  and ``from x import f as g`` makes ``g`` a reader when ``f`` is one. A new
  helper is picked up without editing this file.
- **Keys.** A string literal; a name bound to one in the same module, or at
  module level anywhere in the package (``PREFLIGHT_GATE_MODE_ENV``); a ``for``
  or comprehension target looping over a literal tuple; or an f-string, which
  becomes a pattern (``ATLAN_SDR_<…>_START_TO_CLOSE_SECONDS``) that the docs
  must spell with a ``<PLACEHOLDER>``.

``application_sdk/testing/`` is excluded: its ``E2E_*`` variables configure the
test harness and are documented with it (see ``docs/standards/env-vars.md``).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from functools import cache
from pathlib import Path

from application_sdk.common.env_warnings import _REMOVED_ENV_VARS

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "application_sdk"
CONFIGURATION_DOC = REPO_ROOT / "docs" / "configuration.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: Names the SDK reads but does not own: set by a cloud SDK's own conventions or
#: by the OS. Their meaning is documented by their owners, not by this SDK.
THIRD_PARTY_PREFIXES: tuple[str, ...] = ("AWS_",)
THIRD_PARTY_NAMES: frozenset[str] = frozenset(
    {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "HOSTNAME",
    }
)

#: Stands in for an f-string placeholder the collector cannot resolve.
_HOLE = "\x00"
_ENV_NAME = re.compile(rf"^[A-Z][A-Z0-9_{_HOLE}]*[A-Z0-9{_HOLE}]$")
_ENVIRON_KEY_METHODS = frozenset({"get", "setdefault", "pop"})


def _is_environ(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _callee(call: ast.Call) -> str | None:
    """The reader-table key for *call*'s function, ``"<environ>"`` for stdlib."""
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr == "getenv" or (
            func.attr in _ENVIRON_KEY_METHODS and _is_environ(func.value)
        ):
            return "<environ>"
        return func.attr
    if isinstance(func, ast.Name):
        return "<environ>" if func.id == "getenv" else func.id
    return None


@cache
def _trees() -> dict[Path, ast.Module]:
    return {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(PACKAGE.rglob("*.py"))
        if "testing" not in path.relative_to(PACKAGE).parts
    }


@cache
def _readers() -> dict[str, frozenset[int]]:
    """Function name -> positional indexes that carry an env-var key."""
    readers: dict[str, set[int]] = {"<environ>": {0}}
    aliases = [
        (alias.name, alias.asname)
        for tree in _trees().values()
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname
    ]
    functions = [
        node
        for tree in _trees().values()
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    changed = True
    while changed:
        changed = False
        for source, alias in aliases:
            if source in readers and not readers[source] <= readers.get(alias, set()):
                readers.setdefault(alias, set()).update(readers[source])
                changed = True
        for fn in functions:
            params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
            if params[:1] in (["self"], ["cls"]):
                params = params[1:]
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                for arg in _key_args(node, readers):
                    if isinstance(arg, ast.Name) and arg.id in params:
                        slots = readers.setdefault(fn.name, set())
                        if params.index(arg.id) not in slots:
                            slots.add(params.index(arg.id))
                            changed = True
    return {name: frozenset(slots) for name, slots in readers.items()}


def _key_args(
    call: ast.Call, readers: dict[str, set[int]] | dict[str, frozenset[int]]
) -> list[ast.expr]:
    slots = readers.get(_callee(call) or "", ())
    return [call.args[i] for i in sorted(slots) if i < len(call.args)]


class _Bindings:
    """String values a name can hold within one module."""

    def __init__(self, tree: ast.Module, package_constants: dict[str, set[str]]):
        self._package = package_constants
        self._local: dict[str, set[str]] = {}
        self._tables: dict[str, ast.expr] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        self._tables[target.id] = node.value
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    self._bind(target, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self._bind(node.target, node.value)
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                self._bind_loop(node.target, node.iter)

    def _bind(self, target: ast.expr, value: ast.expr) -> None:
        if isinstance(target, ast.Name):
            values = self.resolve(value)
            if values:
                self._local.setdefault(target.id, set()).update(values)

    def _bind_loop(self, target: ast.expr, iterable: ast.expr) -> None:
        rows = self._literal_rows(iterable)
        for row in rows:
            if isinstance(target, ast.Name):
                self._bind(target, row)
            elif isinstance(target, ast.Tuple) and isinstance(row, ast.Tuple):
                for name, item in zip(target.elts, row.elts):
                    self._bind(name, item)

    def _literal_rows(self, iterable: ast.expr) -> list[ast.expr]:
        if isinstance(iterable, ast.Name) and iterable.id in self._tables:
            iterable = self._tables[iterable.id]
        if isinstance(iterable, (ast.Tuple, ast.List, ast.Set)):
            return list(iterable.elts)
        return []

    def resolve(self, node: ast.expr) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            return self._local.get(node.id) or self._package.get(node.id, set())
        if isinstance(node, ast.JoinedStr):
            return {"".join(self._fstring_parts(node))}
        return set()

    def _fstring_parts(self, node: ast.JoinedStr) -> Iterator[str]:
        for part in node.values:
            if isinstance(part, ast.Constant):
                yield str(part.value)
                continue
            assert isinstance(part, ast.FormattedValue)
            values = self.resolve(part.value)
            yield next(iter(values)) if len(values) == 1 else _HOLE


def _package_constants() -> dict[str, set[str]]:
    """Module-level ``NAME = "VALUE"`` bindings, kept only when unambiguous."""
    seen: dict[str, set[str]] = {}
    for tree in _trees().values():
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        seen.setdefault(target.id, set()).add(node.value.value)
    return {name: values for name, values in seen.items() if len(values) == 1}


def _keys_read(node: ast.AST, readers: dict[str, frozenset[int]]) -> list[ast.expr]:
    if isinstance(node, ast.Call):
        return _key_args(node, readers)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _is_environ(node.value)
    ):
        return [node.slice]
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and _is_environ(node.comparators[0])
    ):
        return [node.left]
    return []


@cache
def collected_env_vars() -> dict[str, str]:
    """Every env-var name (or ``_HOLE`` pattern) read, mapped to one call site."""
    readers = _readers()
    package_constants = _package_constants()
    found: dict[str, str] = {}
    for path, tree in _trees().items():
        bindings = _Bindings(tree, package_constants)
        for node in ast.walk(tree):
            for key in _keys_read(node, readers):
                for value in bindings.resolve(key):
                    # A pattern needs a literal ``X_`` stem: ``{prefix}{name}``
                    # forwards a name some other caller supplies.
                    stem = value.split(_HOLE, 1)[0]
                    if _ENV_NAME.match(value) and (_HOLE not in value or "_" in stem):
                        site = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                        found.setdefault(value, site)
    return found


def _display(name: str) -> str:
    return name.replace(_HOLE, "<…>")


def _doc_pattern(name: str) -> re.Pattern[str]:
    placeholder = r"(?:<[A-Za-z0-9_-]+>|\*)"
    body = placeholder.join(re.escape(part) for part in name.split(_HOLE))
    return re.compile(rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])")


def _is_third_party(name: str) -> bool:
    return name in THIRD_PARTY_NAMES or name.startswith(THIRD_PARTY_PREFIXES)


def _env_example_names() -> list[str]:
    names = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
        if match:
            names.append(match.group(1))
    return names


def _sorted_report(rows: Iterable[str]) -> str:
    return "\n  ".join(sorted(rows))


class TestCollector:
    """Guards the collector, so a broken one cannot pass the coverage test."""

    def test_finds_vars_through_every_read_shape(self) -> None:
        found = collected_env_vars()
        expected = {
            # os.getenv literal
            "ATLAN_APPLICATION_VERSION",
            # env_* helper with a literal
            "ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS",
            # aliased import (``env_bool as _env_bool``) in main.py
            "ATLAN_WORKFLOW_TLS_ENABLED",
            # cross-module constant (PREFLIGHT_GATE_MODE_ENV)
            "ATLAN_PREFLIGHT_GATE_MODE",
            # tuple-unpacking loop over a literal table in _bootstrap.py
            "ATLAN_ENABLE_STATESTORE",
            # local helper forwarding its parameter (execution/settings.py)
            "TEMPORAL_MAX_CONCURRENT_ACTIVITIES",
        }
        assert expected <= found.keys()
        assert f"ATLAN_SDR_{_HOLE}_START_TO_CLOSE_SECONDS" in found
        # The package reads ~220 names; a collapse means the collector broke.
        assert len(found) > 150

    def test_excludes_the_test_harness(self) -> None:
        assert not [name for name in collected_env_vars() if name.startswith("E2E_")]


def test_every_runtime_env_var_is_documented() -> None:
    doc = CONFIGURATION_DOC.read_text(encoding="utf-8")
    missing = [
        f"{_display(name)}  ({site})"
        for name, site in collected_env_vars().items()
        if not _is_third_party(name)
        and name not in _REMOVED_ENV_VARS
        and not _doc_pattern(name).search(doc)
    ]
    assert not missing, (
        "These env vars are read by application_sdk/ but not documented in "
        "docs/configuration.md. Add a row for each (spell an f-string part as "
        "<PLACEHOLDER>), or, for a variable the SDK does not own, add it to "
        f"THIRD_PARTY_NAMES in this test:\n  {_sorted_report(missing)}"
    )


def test_env_example_sets_only_names_the_sdk_reads() -> None:
    read = collected_env_vars().keys()
    dead = [
        name
        for name in _env_example_names()
        if name not in read and name not in _REMOVED_ENV_VARS
    ]
    assert not dead, (
        ".env.example sets env vars application_sdk/ does not read. Remove them, "
        "and if the SDK used to read one, add it to _REMOVED_ENV_VARS in "
        f"application_sdk/common/env_warnings.py:\n  {_sorted_report(dead)}"
    )


def test_env_example_does_not_set_removed_names() -> None:
    stale = sorted(set(_env_example_names()) & _REMOVED_ENV_VARS)
    assert not stale, (
        ".env.example sets env vars the SDK no longer reads (listed in "
        f"_REMOVED_ENV_VARS). Remove them: {stale}"
    )


def test_third_party_allowlist_entries_are_still_read() -> None:
    read = collected_env_vars().keys()
    stale = sorted(THIRD_PARTY_NAMES - read)
    assert not stale, f"Allowlisted names the SDK no longer reads: {stale}"
