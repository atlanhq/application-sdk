"""Tests for the shared name helpers in ``_ast_common._names`` (FND-4946).

These replace eleven private copies of "last name of a dotted reference",
six of "a class's base names" and two of the SDK's class-name-to-app-name
transform.
"""

from __future__ import annotations

import ast

import pytest

from conformance.suite.checks._ast_common import base_names, leaf_name, pascal_to_kebab


def _expr(src: str) -> ast.expr:
    return ast.parse(src, mode="eval").body


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("foo", "foo"),
        ("a.b.foo", "foo"),
        ("foo()", None),
        ("foo[int]", None),
        ("'literal'", None),
    ],
)
def test_leaf_name(src: str, expected: str | None) -> None:
    assert leaf_name(_expr(src)) == expected


def test_leaf_name_of_none_is_none() -> None:
    assert leaf_name(None) is None


def test_base_names_keeps_order_and_drops_non_simple_bases() -> None:
    cls = ast.parse("class A(Base, mod.Other, Generic[T], make()): pass").body[0]
    assert isinstance(cls, ast.ClassDef)
    assert base_names(cls) == ["Base", "Other"]


_KEBAB_CASES = [
    "Greeter",
    "CsvPipeline",
    "MyAwesomeApp",
    "HTTPHandler",
    "S3Loader",
    "MSSQLMetadataExtractor",
    "myapp",
    "A",
    "OAuth2Client",
    "SAPHanaApp",
]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Greeter", "greeter"),
        ("MyAwesomeApp", "my-awesome-app"),
        ("HTTPHandler", "http-handler"),
        ("MSSQLMetadataExtractor", "mssql-metadata-extractor"),
    ],
)
def test_pascal_to_kebab(name: str, expected: str) -> None:
    assert pascal_to_kebab(name) == expected


@pytest.mark.parametrize("name", _KEBAB_CASES)
def test_pascal_to_kebab_matches_the_sdk(name: str) -> None:
    """The result is compared with the app name the SDK derives at runtime, so
    the two must never differ."""
    base = pytest.importorskip("application_sdk.app.base")
    assert pascal_to_kebab(name) == base._pascal_to_kebab(name)
