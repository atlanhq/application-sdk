"""Tests for conformance_pr_shape_gate.py.

This gate is the only mechanical thing standing between the remediation rover and
the gates it is graded by. Its instructions not to touch `tests/`, `.github/` or
`conformance/` are prose; these tests are what make the prohibition real, so they
lean on the failure cases rather than the happy path.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess

import pytest

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "conformance_pr_shape_gate.py"
_spec = importlib.util.spec_from_file_location("conformance_pr_shape_gate", _MOD_PATH)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ── the boundary: forbidden paths ─────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_foo.py",
        "tests/conftest.py",
        ".github/workflows/tests.yaml",
        ".github/scripts/build_conformance_args.py",
        "conformance/suite/rules/logging.py",
        "packages/conformance/conformance/suite/rules/logging.py",
        "./tests/test_foo.py",  # normalised leading ./
    ],
)
def test_rejects_edits_to_the_gates_it_is_graded_by(path: str) -> None:
    v = gate.evaluate(
        files=["app/main.py", path],
        title="fix(conformance): resolve L004 ExceptBlockMissingExcInfoLog",
        branch="conformance/l004",
        body="",
    )
    assert not v.ok
    assert path in v.reason


def test_accepts_an_ordinary_source_only_diff() -> None:
    v = gate.evaluate(
        files=["app/transformer.py", "app/client.py"],
        title="fix(conformance): resolve L004 ExceptBlockMissingExcInfoLog",
        branch="conformance/l004",
        body="Cleared 92 of 92 findings.",
    )
    assert v.ok, v.reason
    assert "L004" in v.reason


def test_accepts_a_dockerfile_diff_for_an_i_series_rule() -> None:
    v = gate.evaluate(
        files=["Dockerfile"],
        title="fix(conformance): resolve I005 DockerfileRootUser",
        branch="conformance/i005",
        body="",
    )
    assert v.ok, v.reason


# ── the subject-path exemption ────────────────────────────────────────────


def test_c_series_may_write_its_own_subject_prefix() -> None:
    """A C rule grades .github/; refusing it write access there makes the series
    permanently un-remediable, which is not the same risk the ban exists for."""
    v = gate.evaluate(
        files=[".github/workflows/conformance.yaml"],
        title="fix(conformance): resolve C002 ManagedFileDrift",
        branch="conformance/c002",
        body="",
    )
    assert v.ok, v.reason


def test_t_series_may_write_tests_but_not_github() -> None:
    ok = gate.evaluate(
        files=["tests/unit/test_client.py"],
        title="fix(conformance): resolve T013 TestFileOutsideTierDir",
        branch="conformance/t013",
        body="",
    )
    assert ok.ok, ok.reason

    # The exemption is per-prefix, not a blanket pass.
    bad = gate.evaluate(
        files=["tests/unit/test_client.py", ".github/workflows/tests.yaml"],
        title="fix(conformance): resolve T013 TestFileOutsideTierDir",
        branch="conformance/t013",
        body="",
    )
    assert not bad.ok
    assert ".github/workflows/tests.yaml" in bad.reason


def test_exemption_does_not_leak_to_other_series() -> None:
    """An L rule has no business in .github/ just because C rules do."""
    v = gate.evaluate(
        files=[".github/workflows/conformance.yaml"],
        title="fix(conformance): resolve L004 ExceptBlockMissingExcInfoLog",
        branch="conformance/l004",
        body="",
    )
    assert not v.ok
    assert "may write" not in v.reason  # no exemption mentioned for L


# ── one rule per PR ───────────────────────────────────────────────────────


def test_rejects_a_pr_claiming_two_rules_in_the_body() -> None:
    v = gate.evaluate(
        files=["app/main.py"],
        title="fix(conformance): resolve L004 ExceptBlockMissingExcInfoLog",
        branch="conformance/l004",
        body="Also fixed E002 while I was in there.",
    )
    assert not v.ok
    assert "E002" in v.reason


def test_rejects_a_title_naming_two_rules() -> None:
    v = gate.evaluate(
        files=["app/main.py"],
        title="fix(conformance): resolve L004 and L011",
        branch="",
        body="",
    )
    assert not v.ok
    assert "more than one rule" in v.reason


def test_branch_and_title_must_agree() -> None:
    v = gate.evaluate(
        files=["app/main.py"],
        title="fix(conformance): resolve E002 TypedExceptPass",
        branch="conformance/l004",
        body="",
    )
    assert not v.ok
    assert "disagrees with itself" in v.reason


def test_body_may_repeat_the_pr_s_own_rule() -> None:
    v = gate.evaluate(
        files=["app/main.py"],
        title="fix(conformance): resolve L004 ExceptBlockMissingExcInfoLog",
        branch="conformance/l004",
        body="L004: cleared 3 of 3. Suite 0.20.1. L004 detects clean after.",
    )
    assert v.ok, v.reason


# ── rule resolution ───────────────────────────────────────────────────────


def test_push_to_pr_branch_resolves_the_rule_from_the_title() -> None:
    """This delivery mode has no conformance/<rule> branch to key off."""
    v = gate.evaluate(
        files=["app/main.py"],
        title="fix(conformance): resolve L004 ExceptBlockMissingExcInfoLog",
        branch="feature/some-dev-branch",
        body="",
    )
    assert v.ok, v.reason
    assert "L004" in v.reason


def test_rejects_a_pr_that_names_no_rule_at_all() -> None:
    v = gate.evaluate(
        files=["app/main.py"],
        title="fix: tidy up logging",
        branch="feature/some-dev-branch",
        body="",
    )
    assert not v.ok
    assert "must say which rule" in v.reason


def test_branch_rule_is_case_insensitive() -> None:
    for branch in ("conformance/l004", "conformance/L004"):
        v = gate.evaluate(
            files=["app/main.py"],
            title="fix(conformance): resolve L004 X",
            branch=branch,
            body="",
        )
        assert v.ok, (branch, v.reason)


# ── degenerate input ──────────────────────────────────────────────────────


def test_rejects_an_empty_diff() -> None:
    v = gate.evaluate(
        files=[], title="fix(conformance): L004", branch="conformance/l004", body=""
    )
    assert not v.ok
    assert "changes no files" in v.reason


def test_rule_ids_in_ignores_lookalikes() -> None:
    assert gate.rule_ids_in("L004") == {"L004"}
    assert gate.rule_ids_in("no rules here") == set()
    # Not a rule ID: too many digits, lowercase, or embedded in a word.
    assert gate.rule_ids_in("L0041") == set()
    assert gate.rule_ids_in("l004") == set()
    assert gate.rule_ids_in("xL004") == set()


# ── the CLI surface ───────────────────────────────────────────────────────


def test_main_reads_the_env_override_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHANGED_FILES", "app/main.py\napp/client.py\n")
    monkeypatch.setenv("PR_TITLE", "fix(conformance): resolve L004 X")
    monkeypatch.setenv("PR_BRANCH", "conformance/l004")
    monkeypatch.setenv("PR_BODY", "")
    assert gate.main() == 0


def test_main_exits_one_and_annotates_on_rejection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CHANGED_FILES", "tests/test_foo.py\n")
    monkeypatch.setenv("PR_TITLE", "fix(conformance): resolve L004 X")
    monkeypatch.setenv("PR_BRANCH", "conformance/l004")
    monkeypatch.setenv("PR_BODY", "")
    assert gate.main() == 1
    out = capsys.readouterr().out
    assert out.startswith("::error title=Conformance remediation PR shape::")


def test_main_requires_repo_and_pr_without_the_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CHANGED_FILES", raising=False)
    monkeypatch.setenv("REPO", "")
    monkeypatch.setenv("PR_NUMBER", "")
    assert gate.main() == 1
    assert "REPO and PR_NUMBER are required" in capsys.readouterr().out


def test_main_surfaces_a_gh_failure_rather_than_passing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gate that cannot read the PR must fail, never pass by default."""
    monkeypatch.delenv("CHANGED_FILES", raising=False)
    monkeypatch.setenv("REPO", "atlanhq/atlan-netsuite-app")
    monkeypatch.setenv("PR_NUMBER", "1")

    def boom(args: list[str]) -> str:
        raise subprocess.CalledProcessError(1, args, stderr="not found")

    monkeypatch.setattr(gate, "_run", boom)
    monkeypatch.setattr(gate, "changed_files", lambda r, p, runner=boom: boom([]))
    assert gate.main() == 1
    assert "could not read PR" in capsys.readouterr().out


# ── helpers against a fake gh ─────────────────────────────────────────────


def test_changed_files_parses_paginated_output() -> None:
    def fake(args: list[str]) -> str:
        assert "--paginate" in args
        return "app/main.py\napp/client.py\n\n"

    assert gate.changed_files("o/r", "1", runner=fake) == [
        "app/main.py",
        "app/client.py",
    ]


def test_pr_meta_parses_json() -> None:
    def fake(args: list[str]) -> str:
        return '{"title":"T","headRefName":"conformance/l004","body":"B"}'

    assert gate.pr_meta("o/r", "1", runner=fake) == ("T", "conformance/l004", "B")


def test_pr_meta_tolerates_null_fields() -> None:
    def fake(args: list[str]) -> str:
        return '{"title":null,"headRefName":null,"body":null}'

    assert gate.pr_meta("o/r", "1", runner=fake) == ("", "", "")
