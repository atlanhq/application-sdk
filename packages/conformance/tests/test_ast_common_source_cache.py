"""The run-scoped source cache: one read, one parse and one walk per file.

Covers :mod:`conformance.suite.checks._ast_common._source_cache` and the call
sites that motivated it: the E-series import resolvers used to re-parse every
module a file imports once per importing file, and every K-series ``scan_all``
re-read and re-parsed the tree its siblings had just parsed.
"""

from __future__ import annotations

import ast
import importlib
import shutil
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

from conformance.suite import runner
from conformance.suite.checks import error_handling
from conformance.suite.checks._ast_common import (
    _source_cache,
    discover,
    load_source,
    parse_module,
    safe_read_text,
    source_cache,
)
from tests._helpers import write_py


@pytest.fixture
def parse_counts(monkeypatch: pytest.MonkeyPatch) -> Iterator[Counter[str]]:
    """Count ``ast.parse`` calls per source text for the length of one test."""
    counts: Counter[str] = Counter()
    real_parse = ast.parse

    def counting_parse(source, *args, **kwargs):
        if isinstance(source, str):
            counts[source] += 1
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting_parse)
    yield counts


# --- outside a cache: nothing is remembered --------------------------------


def test_without_a_cache_a_rewritten_file_is_read_again(tmp_path: Path) -> None:
    (path,) = write_py(tmp_path, {"m.py": "x = 1\n"})
    first = load_source(path)
    path.write_text("y = 2\n", encoding="utf-8")
    second = load_source(path)

    assert first is not None and second is not None
    assert first.text == "x = 1\n"
    assert second.text == "y = 2\n"


def test_without_a_cache_discover_walks_again(tmp_path: Path) -> None:
    write_py(tmp_path, {"app/a.py": ""})
    assert [p.name for p in discover(tmp_path)] == ["a.py"]
    write_py(tmp_path, {"app/b.py": ""})
    assert [p.name for p in discover(tmp_path)] == ["a.py", "b.py"]


# --- inside a cache: shared for the block, dropped after it -----------------


def test_inside_a_cache_a_file_is_read_and_parsed_once(
    tmp_path: Path, parse_counts: Counter[str]
) -> None:
    (path,) = write_py(tmp_path, {"m.py": "x = 1\n"})
    with source_cache():
        first = load_source(path)
        path.write_text("y = 2\n", encoding="utf-8")
        second = load_source(path)
        assert safe_read_text(path) == "x = 1\n"
    after = load_source(path)

    assert first is not None and second is not None and after is not None
    assert second.tree is first.tree
    assert parse_counts["x = 1\n"] == 1
    # The cache closed with the block: the rewrite is visible again.
    assert after.text == "y = 2\n"


def test_parse_module_shares_one_tree_per_text() -> None:
    with source_cache():
        assert parse_module("a = 1\n") is parse_module("a = 1\n", filename="other.py")
    assert parse_module("a = 1\n") is not parse_module("a = 1\n")


def test_a_nested_cache_joins_the_outer_one(tmp_path: Path) -> None:
    (path,) = write_py(tmp_path, {"m.py": "x = 1\n"})
    with source_cache():
        outer = load_source(path)
        with source_cache():
            inner = load_source(path)
        # Leaving the inner block must not close the outer cache.
        again = load_source(path)

    assert outer is not None and inner is not None and again is not None
    assert inner.tree is outer.tree
    assert again.tree is outer.tree


def test_discover_walks_once_per_cache_and_returns_a_fresh_list(
    tmp_path: Path,
) -> None:
    write_py(tmp_path, {"app/a.py": ""})
    with source_cache():
        first = discover(tmp_path)
        first.clear()
        write_py(tmp_path, {"app/b.py": ""})
        second = discover(tmp_path)
    assert [p.name for p in second] == ["a.py"]
    assert [p.name for p in discover(tmp_path)] == ["a.py", "b.py"]


# --- failures --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("syntax.py", b"def broken(:\n"),
        ("latin1.py", b"x = '\xe9'\n"),
        ("nul.py", b"x = 1\x00\n"),
    ],
)
def test_load_source_returns_none_for_a_file_it_cannot_use(
    tmp_path: Path, name: str, content: bytes
) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    with source_cache():
        assert load_source(path) is None
        assert load_source(path) is None
    assert load_source(path) is None


def test_load_source_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    with source_cache():
        assert load_source(tmp_path / "missing.py") is None


def test_a_syntax_error_is_raised_each_time_with_its_own_filename() -> None:
    with source_cache():
        for filename in ("a.py", "b.py"):
            with pytest.raises(SyntaxError) as excinfo:
                parse_module("def broken(:\n", filename=filename)
            assert excinfo.value.filename == filename


# --- the call sites --------------------------------------------------------


_ERRORS_PY = (
    "from application_sdk.errors import AppError\n"
    "\n"
    "\n"
    "class SourceError(AppError):\n"
    "    pass\n"
)


def _importer(i: int) -> str:
    return (
        "from app.errors import SourceError\n"
        "\n"
        "\n"
        f"def f{i}(exc):\n"
        "    raise SourceError('x', cause=exc) from None\n"
    )


def test_e_series_parses_an_imported_module_once_per_run(
    tmp_path: Path, parse_counts: Counter[str]
) -> None:
    # FND-4944: each scanned file built its own import resolver, so a module
    # imported by N files was re-parsed N times (7,217 of a 32,177-parse SDK
    # run). The resolvers now load through the run's source cache.
    files = {"app/errors.py": _ERRORS_PY}
    files.update({f"app/m{i}.py": _importer(i) for i in range(5)})
    paths = write_py(tmp_path, files)

    error_handling.scan_all(paths, tmp_path)

    assert parse_counts[_ERRORS_PY] == 1


#: Modules that read and parse whole files through the source cache. A direct
#: ``ast.parse`` of a module from one of them would bypass it again.
_CACHED_MODULES = frozenset(
    {
        "conformance.suite.checks.artifact_schema_declared._check",
        "conformance.suite.checks.artifact_schema_writer._check",
        "conformance.suite.checks.error_handling",
        "conformance.suite.checks.error_handling._sdk_errors",
        "conformance.suite.checks.error_handling._dependency_category",
        "conformance.suite.checks.error_handling.exception_chaining",
        "conformance.suite.checks.manifest_contract._check",
        "conformance.suite.checks.manifest_contract._entrypoint_class_names",
        "conformance.suite.checks.manifest_contract._filter_string_acceptance",
        "conformance.suite.checks.manifest_contract._input_fields",
        "conformance.suite.checks.preflight._common",
        "conformance.suite.checks.prescriptions",
        "conformance.suite.checks.prescriptions._unbounded_fields",
        "conformance.suite.checks.text_io_encoding",
    }
)


def _module_parses(module_name: str) -> list[int]:
    """Lines in *module_name* that call ``ast.parse`` on a whole module."""
    path = Path(importlib.import_module(module_name).__file__ or "")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "parse"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ast"
        and not any(
            kw.arg == "mode"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "eval"
            for kw in node.keywords
        )
    ]


@pytest.mark.parametrize("module_name", sorted(_CACHED_MODULES))
def test_converted_checks_parse_only_through_the_cache(module_name: str) -> None:
    # Statically, because most of these only reach their parse on a full app
    # shape (generated manifests, a contract): a direct ast.parse here would
    # re-parse what the run has already parsed. Use load_source/parse_module.
    assert _module_parses(module_name) == []


def test_no_check_mutates_a_shared_tree(tmp_path: Path) -> None:
    # Trees are shared between checks for the whole run, so a check that
    # edited one would change what every later check sees. Run every check
    # over real, construct-rich source (this package's own checks) and
    # compare each cached tree with a fresh parse of the same text.
    checks_dir = Path(error_handling.__file__).resolve().parent.parent
    shutil.copytree(
        checks_dir,
        tmp_path / "app" / "checks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    with source_cache():
        runner._collect(
            tmp_path,
            runner._CHECKS,
            active_scope=None,
            excluded_prefixes=(),
            rule_ids=None,
            series=None,
        )
        cache = _source_cache._ACTIVE.get()
        assert cache is not None
        trees = dict(cache.trees)

    assert len(trees) > 50
    changed = [
        text.splitlines()[0] if text else "<empty>"
        for text, tree in trees.items()
        if ast.dump(tree, include_attributes=True)
        != ast.dump(ast.parse(text), include_attributes=True)
    ]
    assert changed == []
