"""E022 — a SQL client's ``run_query`` override drops the SDK base's ``params``.

Each negative case is a shape a bare text match on
``async def run_query(self, query: str, batch_size: int = 100000)`` also hits,
or a compliant shape it would miss; every one is asserted to stay clean.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conformance.suite.checks.error_handling import discover, scan_path, scan_text
from conformance.suite.checks.error_handling._sql_client import (
    RUN_QUERY,
    RUN_QUERY_PARAM,
    RUN_QUERY_PARAMS_SDK_FLOOR,
    SDK_SQL_CLIENT_CLASSES,
    SDK_SQL_CLIENT_MODULES,
    sdk_binds_run_query_params,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

_IMPORT = "from application_sdk.clients.sql import BaseSQLClient\n\n\n"

#: The probe the fleet sweep matched, verbatim.
_DROPPED = "    async def run_query(self, query: str, batch_size: int = 100000):\n"

_BODY = "        yield []\n"

_KEPT = (
    "    async def run_query(\n"
    "        self,\n"
    "        query: str,\n"
    "        batch_size: int = 100000,\n"
    "        params: Mapping[str, SqlBindValue] | None = None,\n"
    "    ):\n"
)


def _e022(src: str, file: str = "app/clients.py") -> list[int]:
    return [f.line for f in scan_text(src, file) if f.rule_id == "E022"]


def _lock(sdk_version: str | None) -> str:
    lines = ["version = 1\n"]
    if sdk_version is not None:
        lines.append(
            f'\n[[package]]\nname = "atlan-application-sdk"\nversion = "{sdk_version}"\n'
        )
    lines.append('\n[[package]]\nname = "sqlalchemy"\nversion = "2.0.0"\n')
    return "".join(lines)


def _repo(
    tmp_path: Path, files: dict[str, str], sdk_version: str | None = "3.49.0"
) -> Path:
    if sdk_version != "<no lock>":
        (tmp_path / "uv.lock").write_text(_lock(sdk_version))
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def _scan_repo(root: Path, target: str) -> list[tuple[str, int]]:
    return [
        (f.file, f.line) for f in scan_path(root / target, root) if f.rule_id == "E022"
    ]


# ---------------------------------------------------------------------------
# Flagged
# ---------------------------------------------------------------------------


def test_flags_the_probe_shape() -> None:
    src = _IMPORT + "class SQLClient(BaseSQLClient):\n" + _DROPPED + _BODY
    assert _e022(src) == [5]


def test_flags_an_override_spelled_across_several_lines() -> None:
    """The removed multi-line form a single-line probe misses."""
    src = (
        _IMPORT
        + "class SQLClient(BaseSQLClient):\n"
        + "    async def run_query(\n"
        + "        self, query: str, batch_size: int = 100000\n"
        + "    ):\n"
        + _BODY
    )
    assert _e022(src) == [5]


@pytest.mark.parametrize(
    "header",
    [
        "from application_sdk.clients.sql import BaseSQLClient\n",
        "from application_sdk.clients import BaseSQLClient\n",
        "from application_sdk.clients.sql import AsyncBaseSQLClient as BaseSQLClient\n",
        (
            "from application_sdk.clients.sql import BaseSQLClient as _Base\n"
            "BaseSQLClient = None\n"
        ),
    ],
)
def test_flags_each_import_spelling(header: str) -> None:
    base = "_Base" if "as _Base" in header else "BaseSQLClient"
    src = header + f"\n\nclass C({base}):\n" + _DROPPED + _BODY
    assert len(_e022(src)) == 1


@pytest.mark.parametrize(
    "header,base",
    [
        ("import application_sdk.clients.sql as sql\n", "sql.BaseSQLClient"),
        ("from application_sdk.clients import sql\n", "sql.BaseSQLClient"),
        ("import application_sdk.clients\n", "application_sdk.clients.BaseSQLClient"),
        ("from application_sdk import clients\n", "clients.AsyncBaseSQLClient"),
    ],
)
def test_flags_a_base_reached_through_a_module(header: str, base: str) -> None:
    src = header + f"\n\nclass C({base}):\n" + _DROPPED + _BODY
    assert len(_e022(src)) == 1


def test_flags_a_sync_def_override() -> None:
    src = (
        _IMPORT
        + "class C(BaseSQLClient):\n"
        + "    def run_query(self, query, batch_size=100000):\n"
        + "        return []\n"
    )
    assert _e022(src) == [5]


def test_flags_a_divergent_override_under_a_type_ignore() -> None:
    """A deliberate divergent signature still breaks a caller that binds."""
    src = (
        _IMPORT
        + "class ImpalaClient(BaseSQLClient):\n"
        + "    async def run_query(self, query: str) -> list[tuple]:  # type: ignore[override]\n"
        + "        return []\n"
    )
    assert _e022(src) == [5]


def test_flags_only_star_args() -> None:
    """``*args`` alone still rejects the ``params=`` keyword callers pass."""
    src = (
        _IMPORT
        + "class C(BaseSQLClient):\n"
        + "    async def run_query(self, *args):\n"
        + _BODY
    )
    assert _e022(src) == [5]


def test_flags_a_similarly_named_parameter() -> None:
    src = (
        _IMPORT
        + "class C(BaseSQLClient):\n"
        + "    async def run_query(self, query, batch_size=100000, param=None):\n"
        + _BODY
    )
    assert _e022(src) == [5]


def test_flags_a_same_module_intermediate() -> None:
    src = (
        _IMPORT
        + "class _Shared(BaseSQLClient):\n    pass\n\n\n"
        + "class C(_Shared):\n"
        + _DROPPED
        + _BODY
    )
    assert _e022(src) == [9]


def test_flags_the_last_definition_when_redefined() -> None:
    src = (
        _IMPORT + "class C(BaseSQLClient):\n" + _KEPT + _BODY + "\n" + _DROPPED + _BODY
    )
    assert _e022(src) == [13]


def test_flags_on_the_def_line_of_a_decorated_override() -> None:
    src = _IMPORT + "class C(BaseSQLClient):\n" + "    @traced\n" + _DROPPED + _BODY
    assert _e022(src) == [6]


def test_message_names_the_class_and_the_fix() -> None:
    src = _IMPORT + "class RedshiftClient(BaseSQLClient):\n" + _DROPPED + _BODY
    (finding,) = [f for f in scan_text(src, "app/clients.py") if f.rule_id == "E022"]
    assert "RedshiftClient.run_query" in finding.message
    assert "params: Mapping[str, SqlBindValue] | None = None" in finding.message
    assert "SqlBindParameterError" in finding.message


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signature",
    [
        _KEPT,
        "    async def run_query(self, query, batch_size=100000, params=None):\n",
        '    async def run_query(self, query, batch_size=100000, params: "Mapping[str, SqlBindValue] | None" = None):\n',
        "    async def run_query(self, query, batch_size=100000, *, params=None):\n",
        "    async def run_query(self, *args: Any, **kwargs: Any):\n",
        "    async def run_query(self, query, **kwargs):\n",
    ],
)
def test_silent_when_the_override_accepts_params(signature: str) -> None:
    src = _IMPORT + "class C(BaseSQLClient):\n" + signature + _BODY
    assert _e022(src) == []


def test_silent_on_a_test_double_that_subclasses_nothing() -> None:
    """The fleet's test fakes: same signature, no BaseSQLClient base."""
    src = (
        "from typing import Any\n\n\n"
        "class _FakeClient:\n"
        "    async def run_query(self, query: str, batch_size: int = 100000) -> Any:\n"
        "        for b in self._batches:\n"
        "            yield b\n"
    )
    assert _e022(src, "tests/unit/test_fake.py") == []


def test_silent_on_a_client_interface_implementation() -> None:
    """A client that implements ClientInterface directly has no params to keep."""
    src = (
        "from application_sdk.clients import ClientInterface\n\n\n"
        "class SapAseClient(ClientInterface):\n" + _DROPPED + _BODY
    )
    assert _e022(src) == []


@pytest.mark.parametrize(
    "src",
    [
        # A module-level function, not a method.
        "async def run_query(query: str, batch_size: int = 100000):\n" + _BODY,
        # A nested helper inside a method of an SQL client.
        _IMPORT
        + "class C(BaseSQLClient):\n"
        + "    async def fetch(self):\n"
        + "        async def run_query(query, batch_size=100000):\n"
        + "            yield []\n"
        + "        return run_query\n",
        # A run_query in the class body of a nested class.
        _IMPORT
        + "class C(BaseSQLClient):\n"
        + "    class Inner:\n"
        + "        async def run_query(self, query, batch_size=100000):\n"
        + "            yield []\n",
        # A same-named method on an unrelated class in the same file.
        _IMPORT + "class Looker:\n" + _DROPPED + _BODY,
        # A subclass that does not override run_query at all.
        _IMPORT + "class C(BaseSQLClient):\n    DB_CONFIG = None\n",
        # A run_query_async helper on an SQL client.
        _IMPORT
        + "class C(BaseSQLClient):\n"
        + "    async def run_query_async(self, conn, sql, batch_size=100):\n"
        + _BODY,
    ],
)
def test_silent_on_shapes_a_bare_text_match_also_hits(src: str) -> None:
    assert _e022(src) == []


@pytest.mark.parametrize(
    "header,base",
    [
        # A same-named class from a third-party package.
        ("from other_sdk.clients import BaseSQLClient\n", "BaseSQLClient"),
        # A non-client name from the SDK's own clients module.
        ("from application_sdk.clients import ClientInterface\n", "ClientInterface"),
        # An unresolvable base.
        ("", "BaseSQLClient"),
        # A different package's module attribute.
        ("import other_sdk.clients.sql as sql\n", "sql.BaseSQLClient"),
    ],
)
def test_silent_when_the_base_is_not_the_sdk_client(header: str, base: str) -> None:
    src = header + f"\n\nclass C({base}):\n" + _DROPPED + _BODY
    assert _e022(src) == []


def test_silent_on_the_sdk_declaring_its_own_subclass() -> None:
    """The SDK's sql.py defines BaseSQLClient locally; its AsyncBaseSQLClient
    derives from that local class and must not resolve as an app subclass."""
    src = (
        "class BaseSQLClient:\n" + _KEPT + _BODY + "\n\n"
        "class AsyncBaseSQLClient(BaseSQLClient):\n" + _DROPPED + _BODY
    )
    assert _e022(src, "application_sdk/clients/sql.py") == []


def test_rule_is_app_scoped_so_the_sdk_is_never_graded() -> None:
    rule = get_rule("E022")
    assert rule.scope is RuleScope.APP


# ---------------------------------------------------------------------------
# Suppression is reported, not silenced
# ---------------------------------------------------------------------------


def test_justified_suppression_is_still_emitted_and_marked() -> None:
    src = (
        _IMPORT
        + "class C(BaseSQLClient):\n"
        + "    # conformance: ignore[E022] driver cannot bind; nothing calls with params\n"
        + _DROPPED
        + _BODY
    )
    findings = [f for f in scan_text(src, "app/clients.py") if f.rule_id == "E022"]
    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert findings[0].suppression_justification


def test_unrelated_suppression_does_not_hide_the_finding() -> None:
    src = (
        _IMPORT
        + "class C(BaseSQLClient):\n"
        + "    # conformance: ignore[E021] unrelated\n"
        + _DROPPED
        + _BODY
    )
    findings = [f for f in scan_text(src, "app/clients.py") if f.rule_id == "E022"]
    assert [f.suppressed for f in findings] == [False]


# ---------------------------------------------------------------------------
# Repo-level: cross-file bases and the SDK-version gate
# ---------------------------------------------------------------------------


_CLIENT = (
    "from application_sdk.clients.sql import BaseSQLClient\n\n\nclass SQLClient(BaseSQLClient):\n"
    + _DROPPED
    + _BODY
)


def test_scan_path_fires_at_or_above_the_floor(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"app/clients.py": _CLIENT}, sdk_version="3.46.0")
    assert _scan_repo(root, "app/clients.py") == [("app/clients.py", 5)]


@pytest.mark.parametrize("version", ["3.39.0", "3.45.9", None, "<no lock>", "garbage"])
def test_scan_path_silent_below_the_floor_or_when_unreadable(
    tmp_path: Path, version: str | None
) -> None:
    """Below 3.46.0 the base has no params (the fleet's 3.39.0 carrier); an
    absent, SDK-less or unparseable lock is not evidence it does."""
    root = _repo(tmp_path, {"app/clients.py": _CLIENT}, sdk_version=version)
    assert _scan_repo(root, "app/clients.py") == []


def test_scan_path_silent_on_an_unparseable_lock(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"app/clients.py": _CLIENT}, sdk_version="<no lock>")
    (root / "uv.lock").write_text("this is [not toml")
    assert _scan_repo(root, "app/clients.py") == []


def test_the_gate_rereads_a_changed_lock(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"app/clients.py": _CLIENT}, sdk_version="3.39.0")
    assert not sdk_binds_run_query_params(root)
    lock = root / "uv.lock"
    lock.write_text(_lock("3.46.0"))
    stat = lock.stat()
    import os

    os.utime(lock, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert sdk_binds_run_query_params(root)


@pytest.mark.parametrize(
    "importer",
    [
        "from app.base import SharedClient\n",
        "from .base import SharedClient\n",
    ],
)
def test_follows_an_intermediate_imported_from_the_app(
    tmp_path: Path, importer: str
) -> None:
    root = _repo(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/base.py": _IMPORT + "class SharedClient(BaseSQLClient):\n    pass\n",
            "app/clients.py": importer
            + "\n\nclass C(SharedClient):\n"
            + _DROPPED
            + _BODY,
        },
    )
    assert _scan_repo(root, "app/clients.py") == [("app/clients.py", 5)]


def test_an_app_intermediate_on_a_non_client_base_stays_clean(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/base.py": "from application_sdk.clients import ClientInterface\n\n\n"
            "class SharedClient(ClientInterface):\n    pass\n",
            "app/clients.py": "from app.base import SharedClient\n\n\nclass C(SharedClient):\n"
            + _DROPPED
            + _BODY,
        },
    )
    assert _scan_repo(root, "app/clients.py") == []


def test_tests_directory_is_not_discovered(tmp_path: Path) -> None:
    """The fleet's carriers in tests/ are fakes; discovery never reaches them."""
    root = _repo(
        tmp_path,
        {
            "app/clients.py": _CLIENT,
            "tests/unit/test_handler_activities.py": _CLIENT,
            "tests/unit/helpers.py": _CLIENT,
        },
    )
    rels = sorted(str(p.relative_to(root)) for p in discover(root))
    assert rels == ["app/clients.py"]


def test_a_compliant_repo_is_clean(tmp_path: Path) -> None:
    src = (
        "from collections.abc import Mapping\n"
        "from application_sdk.clients.sql import BaseSQLClient, SqlBindValue\n\n\n"
        "class SQLClient(BaseSQLClient):\n"
        + _KEPT
        + "        async for b in super().run_query(query, batch_size, params):\n            yield b\n"
    )
    root = _repo(tmp_path, {"app/clients.py": src})
    assert _scan_repo(root, "app/clients.py") == []


# ---------------------------------------------------------------------------
# Catalog entry
# ---------------------------------------------------------------------------


def test_rule_is_app_scoped_warn_and_not_autofixable() -> None:
    rule = get_rule("E022")
    assert rule.name == "SqlClientRunQueryOverrideDropsParams"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert rule.autofixable is False
    assert rule.help_uri and rule.help_uri.endswith("error-handling.md#e022")


# ---------------------------------------------------------------------------
# Drift: the declared shape matches the installed SDK
# ---------------------------------------------------------------------------


def _find_sdk_sql() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "application_sdk" / "clients" / "sql.py"
        if candidate.is_file():
            return candidate
    return None


def _sdk_sql_tree() -> ast.Module:
    path = _find_sdk_sql()
    if path is None:
        pytest.skip("application_sdk source not on disk (installed wheel run)")
    return ast.parse(path.read_text(encoding="utf-8"))


def test_declared_client_classes_match_the_sdk_source() -> None:
    """Every public class in clients/sql.py that is or derives from
    BaseSQLClient is the set the checker resolves, and no other."""
    tree = _sdk_sql_tree()
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    derived = {"BaseSQLClient"} if "BaseSQLClient" in classes else set()
    changed = True
    while changed:
        changed = False
        for name, node in classes.items():
            if name in derived or name.startswith("_"):
                continue
            if any(isinstance(b, ast.Name) and b.id in derived for b in node.bases):
                derived.add(name)
                changed = True
    assert derived == SDK_SQL_CLIENT_CLASSES


def test_declared_modules_export_the_sdk_client_classes() -> None:
    path = _find_sdk_sql()
    if path is None:
        pytest.skip("application_sdk source not on disk (installed wheel run)")
    package = ast.parse((path.parent / "__init__.py").read_text(encoding="utf-8"))
    reexported = {
        alias.asname or alias.name
        for node in package.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "application_sdk.clients.sql"
        for alias in node.names
    }
    assert SDK_SQL_CLIENT_CLASSES <= reexported
    assert {"application_sdk.clients", "application_sdk.clients.sql"} == set(
        SDK_SQL_CLIENT_MODULES
    )


def test_every_sdk_run_query_declares_the_param_the_rule_requires() -> None:
    tree = _sdk_sql_tree()
    seen = 0
    for node in tree.body:
        if (
            not isinstance(node, ast.ClassDef)
            or node.name not in SDK_SQL_CLIENT_CLASSES
        ):
            continue
        for stmt in node.body:
            if (
                isinstance(stmt, ast.AsyncFunctionDef | ast.FunctionDef)
                and stmt.name == RUN_QUERY
            ):
                names = [a.arg for a in (*stmt.args.args, *stmt.args.kwonlyargs)]
                assert RUN_QUERY_PARAM in names, node.name
                seen += 1
    assert seen >= 1


def test_the_floor_is_not_above_the_sdk_that_ships_params() -> None:
    """The floor must never exceed the SDK this checkout is, which declares params."""
    path = _find_sdk_sql()
    if path is None:
        pytest.skip("application_sdk source not on disk (installed wheel run)")
    import tomllib

    from conformance.suite.checks._version import parse_version, version_reached

    pyproject = path.parents[2] / "pyproject.toml"
    version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    parsed = parse_version(version)
    assert parsed is not None
    assert version_reached(RUN_QUERY_PARAMS_SDK_FLOOR, parsed)
