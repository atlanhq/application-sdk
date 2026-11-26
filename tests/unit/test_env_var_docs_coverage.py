"""Every runtime env var the SDK reads is documented, and ``.env.example`` sets
only names the SDK reads.

``docs/standards/env-vars.md`` names ``docs/configuration.md`` as the home of every
runtime variable. Nothing enforced it, so new knobs landed undocumented and
``.env.example`` kept setting names the SDK stopped reading years ago. This test
collects env-var names from the source by AST and checks both directions.

How a name is collected:

- **Readers.** ``os.getenv``, ``os.environ.get`` / ``setdefault`` / ``pop``,
  ``os.environ[...]`` and ``... in os.environ`` read a key, passed by position
  or as ``key=``. Any function that forwards one of its parameters into a
  reader's key slot is itself a reader (``env_bool``, ``env_int``, local
  ``_bool`` helpers, ...), found by fixpoint, and its key is then matched by
  position or by the parameter's name. ``from x import f as g`` makes ``g`` a
  reader when ``f`` is one, ``getenv`` and ``environ`` included. A new helper
  is picked up without editing this file. A key passed through ``*args`` /
  ``**kwargs``, or a reader reached through a bare alias assignment
  (``read = os.getenv``), is not followed.
- **Keys.** A string literal; a name bound to one in the same module, or at
  module level anywhere in the package (``PREFLIGHT_GATE_MODE_ENV``); a ``for``
  or comprehension target looping over a literal tuple; or an f-string, which
  becomes a pattern (``ATLAN_SDR_<…>_START_TO_CLOSE_SECONDS``) that the docs
  must spell with a ``<PLACEHOLDER>``.

A name counts as documented when it appears in a **table row** of
``docs/configuration.md``: its own row, or a fallback named in another row.
A mention in prose does not count.

``application_sdk/testing/`` is excluded: its ``E2E_*`` variables configure the
test harness and are documented with it (see ``docs/standards/env-vars.md``).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from functools import cache
from pathlib import Path

import pytest

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


#: A reader's key slot: its position in the call (``-1`` when keyword-only) and
#: its keyword name.
Slot = tuple[int, str]
#: ``os.getenv(key, ...)`` and ``os.environ.get(key, ...)`` both name it ``key``.
_STDLIB_SLOT: Slot = (0, "key")


def _package_trees() -> dict[str, ast.Module]:
    """``application_sdk/`` outside ``testing/``, keyed by repo-relative path."""
    return {
        str(path.relative_to(REPO_ROOT)): ast.parse(
            path.read_text(encoding="utf-8"), filename=str(path)
        )
        for path in sorted(PACKAGE.rglob("*.py"))
        if "testing" not in path.relative_to(PACKAGE).parts
    }


class _Collector:
    """Collects the env-var names a set of modules reads."""

    def __init__(self, trees: dict[str, ast.Module]):
        self._trees = trees
        self._import_aliases = [
            (alias.name, alias.asname)
            for tree in trees.values()
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.asname
        ]
        self._environ_names = {"environ"} | {
            alias for source, alias in self._import_aliases if source == "environ"
        }
        self._readers = self._find_readers()

    def _is_environ(self, node: ast.expr) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
            isinstance(node, ast.Name) and node.id in self._environ_names
        )

    def _callee(self, call: ast.Call) -> str | None:
        """The reader-table key for *call*'s function, ``"<environ>"`` for stdlib."""
        func = call.func
        if isinstance(func, ast.Attribute):
            if func.attr == "getenv" or (
                func.attr in _ENVIRON_KEY_METHODS and self._is_environ(func.value)
            ):
                return "<environ>"
            return func.attr
        if isinstance(func, ast.Name):
            return "<environ>" if func.id == "getenv" else func.id
        return None

    def _key_args(
        self, call: ast.Call, readers: Mapping[str, Collection[Slot]]
    ) -> list[ast.expr]:
        """The arguments of *call* in a reader's key slot, by position or keyword."""
        keys: list[ast.expr] = []
        for index, name in readers.get(self._callee(call) or "", ()):
            if 0 <= index < len(call.args):
                keys.append(call.args[index])
            keys.extend(kw.value for kw in call.keywords if kw.arg == name)
        return keys

    def _find_readers(self) -> dict[str, frozenset[Slot]]:
        """Function name -> the argument slots that carry an env-var key."""
        # ``getenv`` is seeded so ``from os import getenv as read_env`` propagates.
        readers: dict[str, set[Slot]] = {
            "<environ>": {_STDLIB_SLOT},
            "getenv": {_STDLIB_SLOT},
        }
        functions = [
            node
            for tree in self._trees.values()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        changed = True
        while changed:
            changed = False
            for source, alias in self._import_aliases:
                if source in readers and not readers[source] <= readers.get(
                    alias, set()
                ):
                    readers.setdefault(alias, set()).update(readers[source])
                    changed = True
            for fn in functions:
                params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
                if params[:1] in (["self"], ["cls"]):
                    params = params[1:]
                keyword_only = [a.arg for a in fn.args.kwonlyargs]
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    for arg in self._key_args(node, readers):
                        if not isinstance(arg, ast.Name):
                            continue
                        if arg.id in params:
                            slot: Slot = (params.index(arg.id), arg.id)
                        elif arg.id in keyword_only:
                            slot = (-1, arg.id)
                        else:
                            continue
                        slots = readers.setdefault(fn.name, set())
                        if slot not in slots:
                            slots.add(slot)
                            changed = True
        return {name: frozenset(slots) for name, slots in readers.items()}

    def _package_constants(self) -> dict[str, set[str]]:
        """Module-level ``NAME = "VALUE"`` bindings, kept only when unambiguous."""
        seen: dict[str, set[str]] = {}
        for tree in self._trees.values():
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

    def _keys_read(self, node: ast.AST) -> list[ast.expr]:
        if isinstance(node, ast.Call):
            return self._key_args(node, self._readers)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and self._is_environ(node.value)
        ):
            return [node.slice]
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and self._is_environ(node.comparators[0])
        ):
            return [node.left]
        return []

    def collect(self) -> dict[str, str]:
        """Every env-var name (or ``_HOLE`` pattern) read, mapped to one call site."""
        package_constants = self._package_constants()
        found: dict[str, str] = {}
        for path, tree in self._trees.items():
            bindings = _Bindings(tree, package_constants)
            for node in ast.walk(tree):
                for key in self._keys_read(node):
                    for value in bindings.resolve(key):
                        # A pattern needs a literal ``X_`` stem: ``{prefix}{name}``
                        # forwards a name some other caller supplies.
                        stem = value.split(_HOLE, 1)[0]
                        if _ENV_NAME.match(value) and (
                            _HOLE not in value or "_" in stem
                        ):
                            found.setdefault(value, f"{path}:{node.lineno}")
        return found


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


@cache
def collected_env_vars() -> dict[str, str]:
    """Every env-var name the package reads, mapped to one call site."""
    return _Collector(_package_trees()).collect()


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

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param('import os\nos.getenv(key="ATLAN_X")', id="getenv-keyword"),
            pytest.param(
                'import os\nos.environ.get(key="ATLAN_X")', id="environ-get-keyword"
            ),
            pytest.param(
                'from os import getenv as read_env\nread_env("ATLAN_X")',
                id="aliased-getenv",
            ),
            pytest.param(
                'from os import environ as env\nenv.get("ATLAN_X")\nenv["ATLAN_Y"]',
                id="aliased-environ",
            ),
            pytest.param(
                "import os\n"
                "def read(name, default=''):\n"
                "    return os.environ.get(key=name)\n"
                'read(name="ATLAN_X")\n'
                'read("ATLAN_Y")',
                id="helper-forwarding-by-keyword",
            ),
            pytest.param(
                "import os\n"
                "def read(*, name):\n"
                "    return os.getenv(name)\n"
                'read(name="ATLAN_X")',
                id="helper-keyword-only-param",
            ),
        ],
    )
    def test_finds_keyword_and_aliased_reads(self, source: str) -> None:
        found = _Collector({"m.py": ast.parse(source)}).collect()
        assert "ATLAN_X" in found or "ATLAN_Y" in found
        expected = {n for n in ("ATLAN_X", "ATLAN_Y") if f'"{n}"' in source}
        assert found.keys() == expected


def _table_rows(markdown: str) -> str:
    """Only the table rows of *markdown*: a name mentioned in prose is not a row."""
    return "\n".join(
        line for line in markdown.splitlines() if line.lstrip().startswith("|")
    )


def test_table_rows_ignore_prose() -> None:
    doc = "Set `ATLAN_X` to tune it.\n\n| `ATLAN_Y` | `1` | Uses `ATLAN_Z` as a fallback. |"
    rows = _table_rows(doc)
    assert not _doc_pattern("ATLAN_X").search(rows)
    assert _doc_pattern("ATLAN_Y").search(rows)
    assert _doc_pattern("ATLAN_Z").search(rows)


def test_every_runtime_env_var_is_documented() -> None:
    doc = _table_rows(CONFIGURATION_DOC.read_text(encoding="utf-8"))
    missing = [
        f"{_display(name)}  ({site})"
        for name, site in collected_env_vars().items()
        if not _is_third_party(name)
        and name not in _REMOVED_ENV_VARS
        and not _doc_pattern(name).search(doc)
    ]
    assert not missing, (
        "These env vars are read by application_sdk/ but appear in no table row "
        "of docs/configuration.md. Add a row for each (spell an f-string part as "
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
