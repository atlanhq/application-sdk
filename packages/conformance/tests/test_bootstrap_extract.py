"""Tests for the shared bootstrap.extract primitives.

This is a leaf module both bootstrap/command.py (the writer) and
suite/checks/bootstrap_drift.py (the C002 checker) import at module level
without creating a cycle -- see bootstrap/extract.py's module docstring.
"""

from __future__ import annotations

from conformance.bootstrap.extract import (
    EXIT_ZERO_RE,
    extract_field,
    extract_renovate_automerge,
)


def test_extract_field_bare_value() -> None:
    assert extract_field("package_name: app\n", "package_name") == "app"


def test_extract_field_quoted_value() -> None:
    assert extract_field('package_name: "app"\n', "package_name") == "app"


def test_extract_field_indented() -> None:
    assert (
        extract_field(
            "  unit_tests_workflow_file: tests.yaml\n", "unit_tests_workflow_file"
        )
        == "tests.yaml"
    )


def test_extract_field_absent_returns_empty() -> None:
    assert extract_field("some: other\n", "package_name") == ""


def test_extract_field_matches_first_occurrence_only() -> None:
    text = "package_name: first\npackage_name: second\n"
    assert extract_field(text, "package_name") == "first"


def test_extract_renovate_automerge_soft_mode() -> None:
    text = '{"lockFileMaintenance": {"automerge": false}}'
    assert extract_renovate_automerge(text) == "false"


def test_extract_renovate_automerge_hard_mode() -> None:
    text = "{}"
    assert extract_renovate_automerge(text) == "true"


def test_extract_renovate_automerge_unparseable_defaults_hard() -> None:
    assert extract_renovate_automerge("not json") == "true"


def test_exit_zero_re_matches_rendered_expression() -> None:
    line = "exit-zero: ${{ github.event.inputs.exit_zero || true }}"
    m = EXIT_ZERO_RE.search(line)
    assert m is not None
    assert m.group(1) == "true"


def test_exit_zero_re_does_not_match_plain_field() -> None:
    assert EXIT_ZERO_RE.search("exit-zero: true\n") is None
