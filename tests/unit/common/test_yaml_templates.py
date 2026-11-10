"""Tests for ``application_sdk.common.yaml_templates`` (FND-4690)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from application_sdk.common.yaml_templates import (
    flatten_yaml_columns,
    yaml_template_paths,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("columns: {}\n", encoding="utf-8")
    return path


def test_yaml_template_paths_keys_by_upper_basename_recursively(tmp_path: Path):
    table = _touch(tmp_path / "table.yaml")
    column = _touch(tmp_path / "nested" / "deeper" / "column.yaml")
    _touch(tmp_path / "notes.yml")  # only ``.yaml`` is a template

    assert yaml_template_paths(str(tmp_path)) == {
        "TABLE": str(table),
        "COLUMN": str(column),
    }


def test_yaml_template_paths_filters_to_assets(tmp_path: Path):
    _touch(tmp_path / "table.yaml")
    column = _touch(tmp_path / "column.yaml")

    assert yaml_template_paths(str(tmp_path), assets=["COLUMN", "ABSENT"]) == {
        "COLUMN": str(column)
    }


def test_yaml_template_paths_empty_assets_selects_nothing(tmp_path: Path):
    """``assets=[]`` is an explicit empty selection, not "no filter"."""
    _touch(tmp_path / "table.yaml")

    assert yaml_template_paths(str(tmp_path), assets=[]) == {}


def test_yaml_template_paths_does_not_merge_sdk_default_templates(tmp_path: Path):
    """The old helper merged in ``QueryBasedTransformer``'s bundled SQL templates.

    Those leave with ``application_sdk.transformers`` in v4.0, so the
    re-homed helper must resolve only the directory it is given — otherwise an
    app would depend on them again through a non-deprecated name.
    """
    assert yaml_template_paths(str(tmp_path)) == {}


def test_yaml_template_paths_duplicate_basename_is_deterministic(tmp_path: Path):
    _touch(tmp_path / "a" / "table.yaml")
    last = _touch(tmp_path / "b" / "table.yaml")

    assert yaml_template_paths(str(tmp_path)) == {"TABLE": str(last)}


def test_yaml_template_paths_missing_directory_is_empty(tmp_path: Path):
    assert yaml_template_paths(str(tmp_path / "absent")) == {}


def test_flatten_yaml_columns_dots_only_under_non_leaf_parents():
    nested = {
        "typeName": "'Table'",
        "attributes": {
            "name": {"source_query": "table_name"},
            "description": {"source_query": "remarks", "source_columns": ["remarks"]},
        },
    }

    assert flatten_yaml_columns(nested) == [
        {"source_query": "'Table'", "name": "typeName"},
        {"source_query": "table_name", "name": "attributes.name"},
        {
            "source_query": "remarks",
            "source_columns": ["remarks"],
            "name": "attributes.description",
        },
    ]


def test_flatten_yaml_columns_does_not_mutate_input():
    leaf = {"source_query": "x"}
    flatten_yaml_columns({"a": {"b": leaf}})
    assert leaf == {"source_query": "x"}


def test_flatten_yaml_columns_custom_separator():
    assert flatten_yaml_columns({"a": {"b": {"source_query": "x"}}}, sep="__") == [
        {"source_query": "x", "name": "a__b"}
    ]


def test_yaml_templates_imports_without_the_deprecated_transformers():
    """The re-homed module must not drag in the deprecated transformers package.

    Run in a fresh interpreter: this process has long since imported
    ``application_sdk.transformers`` through other tests.
    """
    probe = (
        "import sys, application_sdk.common.yaml_templates; "
        "sys.exit(any(m.startswith('application_sdk.transformers') "
        "for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
