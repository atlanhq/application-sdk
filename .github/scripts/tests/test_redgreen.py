"""Red-green: did this PR's tests ever fail?

The verifier's whole value is that its answer is deterministic, so the tests
here are about the classification rules rather than about pytest. Two of them
guard inversions that would make the result actively misleading — reading a
collection error as doubt, and keying on the subprocess exit code when a
non-zero exit is the *success* condition.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_redgreen as rg  # noqa: E402
from sdk_loop_pack import parse_diff  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[3]
HYPOTHESES = REPO / ".mothership/pr-loop/HYPOTHESES.md"


def _ref(name: str, path: str = "tests/unit/test_x.py") -> rg.TestRef:
    return rg.TestRef(path=path, name=name)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_failing_test_captured_the_change() -> None:
    status, _ = rg.classify("FAILED")
    assert status == rg.CAPTURED


def test_a_collection_error_counts_as_captured_not_as_doubt() -> None:
    """A test that cannot import against the old source references something
    this PR introduced — red in the strongest sense available. Reading it as
    inconclusive would mark every test of a new module as suspect, which is
    exactly backwards and would make the metric useless on the PRs that add the
    most new code."""
    status, detail = rg.classify("ERROR")
    assert status == rg.CAPTURED
    assert "references new code" in detail


def test_a_passing_test_did_not_capture_the_change() -> None:
    status, detail = rg.classify("PASSED")
    assert status == rg.NOT_CAPTURED
    assert "does not capture" in detail


@pytest.mark.parametrize("status", ["SKIPPED", None])
def test_unresolved_statuses_are_inconclusive_not_counted(status) -> None:
    """Counting a skip as either outcome would move the headline number for a
    reason that has nothing to do with test quality."""
    assert rg.classify(status)[0] == rg.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Parsing pytest, without keying on the exit code
# ---------------------------------------------------------------------------


def test_the_report_is_parsed_from_output_not_the_exit_code() -> None:
    """The run is EXPECTED to fail — a non-zero exit is the success condition
    here. A caller keying on `returncode` would invert the entire result."""
    stdout = textwrap.dedent(
        """\
        tests/unit/test_x.py::test_a PASSED
        tests/unit/test_x.py::test_b FAILED
        tests/unit/test_x.py::test_c ERROR
        """
    )
    parsed = rg.parse_pytest_report(stdout)
    assert parsed == {
        "tests/unit/test_x.py::test_a": "PASSED",
        "tests/unit/test_x.py::test_b": "FAILED",
        "tests/unit/test_x.py::test_c": "ERROR",
    }


# ---------------------------------------------------------------------------
# Which tests get examined
# ---------------------------------------------------------------------------


def test_only_test_functions_this_pr_touched_are_examined(tmp_path) -> None:
    """A PR appending one test to a 900-line file changed one test. Re-running
    the other forty against base would report a pile of green-on-base results
    that say nothing about this change, burying the row that matters."""
    tests_dir = tmp_path / "tests" / "unit"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_x.py").write_text(
        textwrap.dedent(
            """\
            def test_old():
                assert True


            def test_new():
                assert True


            def helper_not_a_test():
                return 1
            """
        ),
        encoding="utf-8",
    )
    changed = parse_diff(
        "diff --git a/tests/unit/test_x.py b/tests/unit/test_x.py\n"
        "--- a/tests/unit/test_x.py\n+++ b/tests/unit/test_x.py\n"
        "@@ -4,0 +5,2 @@\n+def test_new():\n+    assert True\n"
    )
    refs = rg.changed_test_functions(tmp_path, changed)
    names = {r.name for r in refs}
    assert names == {"test_new"}, f"examined the wrong set: {names}"


def test_non_test_files_are_ignored(tmp_path) -> None:
    (tmp_path / "application_sdk").mkdir()
    (tmp_path / "application_sdk" / "x.py").write_text(
        "def test_lookalike():\n    pass\n"
    )
    changed = parse_diff(
        "diff --git a/application_sdk/x.py b/application_sdk/x.py\n"
        "--- a/application_sdk/x.py\n+++ b/application_sdk/x.py\n"
        "@@ -0,0 +1,2 @@\n+def test_lookalike():\n+    pass\n"
    )
    assert rg.changed_test_functions(tmp_path, changed) == ()


# ---------------------------------------------------------------------------
# The rate, and the distinction that matters
# ---------------------------------------------------------------------------


def test_no_gradeable_tests_reports_none_not_zero() -> None:
    """0.0 means every test examined passed on the old source. None means none
    were examined. Collapsing them would report a PR with no test changes as
    having the worst possible score."""
    empty = rg.Report()
    assert empty.rate is None

    all_green = rg.Report(outcomes=[rg.Outcome(_ref("test_a"), rg.NOT_CAPTURED)])
    assert all_green.rate == 0.0


def test_inconclusive_outcomes_do_not_move_the_rate() -> None:
    report = rg.Report(
        outcomes=[
            rg.Outcome(_ref("test_a"), rg.CAPTURED),
            rg.Outcome(_ref("test_b"), rg.INCONCLUSIVE),
        ]
    )
    assert report.rate == 1.0


# ---------------------------------------------------------------------------
# Running against base
# ---------------------------------------------------------------------------


def test_verify_overlays_only_the_test_files(tmp_path) -> None:
    """Keeping the old source and taking the new tests is exactly the state the
    author was in before they wrote the fix. Overlaying anything else would be
    running the new code against itself."""
    calls: list[list[str]] = []
    copied: list[tuple[str, str]] = []

    def runner(args, cwd):
        calls.append(list(args))
        if args[:3] == ["git", "worktree", "add"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "uv":
            return subprocess.CompletedProcess(
                args, 1, "tests/unit/test_x.py::test_a FAILED\n", ""
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    def copy_file(src, dst):
        copied.append((str(src), str(dst)))

    report = rg.verify(
        repo=tmp_path,
        base_ref="abc123",
        tests=[_ref("test_a")],
        workdir=tmp_path / "wt",
        runner=runner,
        copy_file=copy_file,
    )
    assert len(copied) == 1 and copied[0][0].endswith("tests/unit/test_x.py")
    assert report.rate == 1.0
    assert any(
        c[:3] == ["git", "worktree", "remove"] for c in calls
    ), "the throwaway worktree was not cleaned up"


def test_a_base_that_cannot_be_materialised_is_reported_not_guessed(tmp_path) -> None:
    """Silently reporting zero captured tests because the checkout failed would
    accuse the author of writing decorative tests on the strength of a git
    error."""

    def runner(args, cwd):
        if args[:3] == ["git", "worktree", "add"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
        return subprocess.CompletedProcess(args, 0, "", "")

    report = rg.verify(
        repo=tmp_path,
        base_ref="deadbeef",
        tests=[_ref("test_a")],
        workdir=tmp_path / "wt",
        runner=runner,
    )
    assert report.rate is None
    assert "could not materialise" in report.skipped_reason


def test_the_worktree_is_cleaned_up_even_when_the_run_raises(tmp_path) -> None:
    """The runner is shared with every other checkout on this machine; a leaked
    worktree outlives the review."""
    removed: list[str] = []

    def runner(args, cwd):
        if args[:3] == ["git", "worktree", "add"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["git", "worktree", "remove"]:
            removed.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise RuntimeError("pytest exploded")

    with pytest.raises(RuntimeError):
        rg.verify(
            repo=tmp_path,
            base_ref="abc",
            tests=[_ref("test_a")],
            workdir=tmp_path / "wt",
            runner=runner,
            copy_file=lambda s, d: None,
        )
    assert removed, "the worktree leaked when the run raised"


# ---------------------------------------------------------------------------
# Reporting — advisory, never a verdict
# ---------------------------------------------------------------------------


def test_the_report_asks_rather_than_accuses() -> None:
    """There are legitimate green-on-base tests: documenting behaviour the
    change must preserve, a refactor with no behavioural delta, backfilled
    coverage. A gate would punish all three."""
    report = rg.Report(
        outcomes=[
            rg.Outcome(_ref("test_a"), rg.CAPTURED),
            rg.Outcome(_ref("test_b"), rg.NOT_CAPTURED),
        ]
    )
    text = rg.render(report)
    assert "1/2" in text and "50%" in text
    assert "can be deliberate" in text
    assert "BLOCK" not in text.upper().replace("BLOCKING", "")


def test_a_pr_with_no_test_changes_says_so() -> None:
    assert "not run" in rg.render(rg.Report(skipped_reason="no test functions added"))


# ---------------------------------------------------------------------------
# The hypotheses brief
# ---------------------------------------------------------------------------


def test_the_hypotheses_brief_demands_the_file_before_the_tests() -> None:
    """Committing to the list first is what stops the exercise collapsing into
    tests for the paths already known to pass — the failure mode of every suite
    written after the code it tests."""
    text = HYPOTHESES.read_text(encoding="utf-8")
    assert "before you write any test" in text
    assert "hypotheses.md" in text


def test_the_hypotheses_brief_shows_a_bad_example() -> None:
    """ "Test that resolve() works" is what this produces without a counterexample
    to point at."""
    text = HYPOTHESES.read_text(encoding="utf-8")
    assert "Not this" in text


def test_the_hypotheses_brief_does_not_point_at_the_old_corpus() -> None:
    text = HYPOTHESES.read_text(encoding="utf-8")
    for forbidden in ("ORCHESTRATION.md", "pr-review/", "retro-log.md"):
        assert forbidden not in text
