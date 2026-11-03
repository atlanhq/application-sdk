"""Submission-time structural regex check on SQL filters (FND-501).

Patterns run on the source's regex engine, not Python's, so only failures no
dialect accepts are refused; dialect-specific syntax passes through.
"""

import pytest

from application_sdk.common.sql_filters import validate_filter_regex_syntax


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("^db(", "unterminated group"),
        ("[a-", "unterminated character class"),
        ("[z-a]", "reversed character range"),
        ("abc\\", "trailing backslash"),
    ],
)
def test_structurally_broken_pattern_is_rejected(pattern: str, reason: str) -> None:
    with pytest.raises(ValueError, match="Invalid regex in filter value") as exc:
        validate_filter_regex_syntax(pattern)
    assert reason in str(exc.value)
    # The message reaches the caller's 422 body: it never quotes the pattern.
    assert pattern not in str(exc.value)
    assert "z-a" not in str(exc.value)


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "^db$",
        "^temp_.*$",
        "\\p{L}+",  # Java / ICU property class; Python's re rejects it
        "(?<name>x)",  # named group in Java / .NET syntax
        "[[:alpha:]]+",  # POSIX class; re only warns
        "a++",
        "*abc",  # undefined in POSIX ERE, literal in some engines
        "db)",  # a lone ")" is a literal in some ERE implementations
        '{"^qa$": ["^public$"]}',  # JSON-encoded filter map
        "{not json",  # malformed envelope is the injection check's call
    ],
)
def test_dialect_specific_or_valid_pattern_passes(pattern: str) -> None:
    assert validate_filter_regex_syntax(pattern) == pattern


def test_filter_map_key_is_checked() -> None:
    with pytest.raises(ValueError, match="filter key: unterminated group"):
        validate_filter_regex_syntax({"^db(": ["^public$"]})


def test_filter_map_value_is_checked() -> None:
    with pytest.raises(ValueError, match="filter value: unterminated character class"):
        validate_filter_regex_syntax({"^db$": ["^ok$", "[a-"]})


def test_apitree_nested_key_is_checked() -> None:
    with pytest.raises(ValueError, match="filter key: unterminated group"):
        validate_filter_regex_syntax({"^db$": {"^s(": {}}})


def test_json_encoded_filter_map_is_checked() -> None:
    with pytest.raises(ValueError, match="filter key: unterminated group"):
        validate_filter_regex_syntax('{"^db(": ["^public$"]}')


def test_non_string_shapes_pass_through() -> None:
    assert validate_filter_regex_syntax(None) is None
    assert validate_filter_regex_syntax(3) == 3
