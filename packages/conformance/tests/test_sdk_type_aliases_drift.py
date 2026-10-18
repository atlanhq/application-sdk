"""Drift guard for the SDK type-alias table B005 reads.

The committed ``conformance/data/sdk_type_aliases.json`` is what lets B005
expand an alias an app imports from the SDK, inside a consumer app repo that
never has ``application_sdk/`` source installed.  For that to stay true the
committed file must equal a fresh read of the SDK's type aliases.

This test makes adding, removing or changing an SDK type alias fail CI until the
table is regenerated in the same PR:

    uv run atlan-application-sdk-conformance gen-sdk-type-aliases
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conformance.suite.checks.deprecation._sdk_type_aliases import (
    DATA_PATH,
    SDK_PACKAGE,
    build_sdk_type_aliases,
    collect_sdk_imported_aliases,
    load_sdk_type_aliases,
    serialize,
)


def _find_sdk_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if parent.joinpath(SDK_PACKAGE, "__init__.py").is_file():
            return parent
    return None


def test_committed_table_matches_sdk_source() -> None:
    sdk_root = _find_sdk_root()
    if sdk_root is None:
        pytest.skip("SDK source not on disk — table drift cannot be checked here.")

    expected = serialize(build_sdk_type_aliases(sdk_root))

    assert DATA_PATH.read_text(encoding="utf-8") == expected, (
        "sdk_type_aliases.json is stale — run "
        "`uv run atlan-application-sdk-conformance gen-sdk-type-aliases`."
    )


def test_table_holds_filter_map_under_both_import_paths() -> None:
    table = load_sdk_type_aliases()

    for path in (
        "application_sdk.templates.contracts.FilterMap",
        "application_sdk.templates.contracts.sql_metadata.FilterMap",
    ):
        assert path in table, path
        assert ast.unparse(table[path].value).startswith("Annotated[dict[str,")


def test_table_holds_lazily_exported_credential_types() -> None:
    # application_sdk.credentials exports the routing types through a module
    # __getattr__ (an import cycle rules out an eager import) and declares them
    # under ``if TYPE_CHECKING:``; the package path is the one apps import.
    table = load_sdk_type_aliases()

    for name in ("CredentialValue", "CredentialMap", "InlineCredentials"):
        assert f"application_sdk.credentials.{name}" in table, name
        assert f"application_sdk.credentials.routing.{name}" in table, name


def test_type_checking_reexport_is_followed(tmp_path: Path) -> None:
    pkg = tmp_path / SDK_PACKAGE / "pkg"
    pkg.mkdir(parents=True)
    (tmp_path / SDK_PACKAGE / "__init__.py").write_text("")
    (pkg / "impl.py").write_text("Alias = dict[str, int]\n")
    (pkg / "__init__.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from application_sdk.pkg.impl import Alias\n"
    )

    (pkg / "user.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from application_sdk.pkg.impl import Alias\n"
    )

    table = build_sdk_type_aliases(tmp_path)

    assert ast.unparse(table["application_sdk.pkg.Alias"].value) == "dict[str, int]"
    # A plain module's TYPE_CHECKING import is for its own annotations only.
    assert "application_sdk.pkg.user.Alias" not in table


def test_only_sdk_imports_are_bound() -> None:
    tree = ast.parse(
        "from application_sdk.templates.contracts import FilterMap as F\n"
        "from elsewhere.contracts import FilterMap\n"
        "from application_sdk.templates.contracts import NotAnAlias\n"
    )

    assert set(collect_sdk_imported_aliases(tree)) == {"F"}
