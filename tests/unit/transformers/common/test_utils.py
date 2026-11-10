"""Deprecation of ``application_sdk.transformers.common.utils`` and ``.errors`` (FND-4690)."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from application_sdk.transformers.atlas import sql as atlas_sql
    from application_sdk.transformers.common import utils
    from application_sdk.transformers.errors import TransformerNotImplementedError
    from application_sdk.transformers.query import QueryBasedTransformer

from application_sdk.common import yaml_templates
from application_sdk.errors import UnimplementedError

_CASES: list[tuple[str, Callable[[], Any], Any, str]] = [
    (
        "process_text",
        lambda: utils.process_text("<b>a</b>   b"),
        "a b",
        "asset-mapper",
    ),
    (
        "build_phoenix_uri",
        lambda: utils.build_phoenix_uri("c", "sql", "db", "schema"),
        "/c/sql/db/schema",
        "f-string",
    ),
    (
        "build_atlas_qualified_name",
        lambda: utils.build_atlas_qualified_name("t/c/1", "db", "schema"),
        "t/c/1/db/schema",
        "creator(",
    ),
    (
        "flatten_yaml_columns",
        lambda: utils.flatten_yaml_columns({"a": {"b": {"source_query": "x"}}}),
        [{"source_query": "x", "name": "a.b"}],
        "application_sdk.common.yaml_templates.flatten_yaml_columns",
    ),
]


@pytest.mark.parametrize(("name", "call", "expected", "target"), _CASES)
def test_public_helpers_warn_and_still_work(
    name: str, call: Callable[[], Any], expected: Any, target: str
):
    with pytest.warns(DeprecationWarning) as record:
        assert call() == expected

    [warning] = record.list
    message = str(warning.message)
    assert f"transformers.common.utils.{name} is deprecated" in message
    assert target in message
    assert "removed in v4.0" in message
    assert warning.filename == __file__


def test_get_yaml_query_template_path_mappings_keeps_its_old_contract(tmp_path: Path):
    """The deprecated helper still merges the SDK default templates; only its
    replacement drops them."""
    (tmp_path / "table.yaml").write_text("columns: {}\n", encoding="utf-8")

    with pytest.warns(DeprecationWarning, match="yaml_template_paths"):
        mapping = utils.get_yaml_query_template_path_mappings(str(tmp_path))

    assert mapping["TABLE"] == str(tmp_path / "table.yaml")  # app overrides SDK
    assert "COLUMN" in mapping  # SDK default still merged in
    assert yaml_templates.yaml_template_paths(str(tmp_path)) == {
        "TABLE": str(tmp_path / "table.yaml")
    }


def test_sdk_transformers_bind_the_private_implementations():
    """The deprecated transformers must not warn per row about a helper the app
    never called: they are bound to the un-decorated implementations."""
    assert atlas_sql.build_atlas_qualified_name is utils._build_atlas_qualified_name
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert atlas_sql.build_atlas_qualified_name("c", "d") == "c/d"


def test_query_transformer_resolves_templates_without_helper_warnings():
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        QueryBasedTransformer(connector_name="c", tenant_id="t")

    helper_warnings = [
        w for w in record if "transformers.common.utils" in str(w.message)
    ]
    assert helper_warnings == []


def test_transformer_not_implemented_error_warns_and_keeps_its_identity():
    with pytest.warns(
        DeprecationWarning, match="application_sdk.errors.UnimplementedError"
    ):
        error = TransformerNotImplementedError()

    assert isinstance(error, UnimplementedError)
    assert error.code == "UNIMPLEMENTED_TRANSFORMER_BASE"
    assert error.operation == "transform"
