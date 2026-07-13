"""Tests for mutation_diff.py — the diff-scoped mutation testing driver."""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from collections import Counter
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "mutation_diff.py"
spec = importlib.util.spec_from_file_location("mutation_diff", SCRIPT)
assert spec is not None and spec.loader is not None
mutation_diff = importlib.util.module_from_spec(spec)
sys.modules["mutation_diff"] = mutation_diff
spec.loader.exec_module(mutation_diff)


# ---------------------------------------------------------------- patterns


def test_module_patterns_maps_paths_to_mutant_globs():
    assert mutation_diff.module_patterns(
        ["application_sdk/services/objectstore.py"]
    ) == ["application_sdk.services.objectstore.*"]


def test_module_patterns_package_init_covers_whole_package():
    assert mutation_diff.module_patterns(["application_sdk/services/__init__.py"]) == [
        "application_sdk.services.*"
    ]


def test_module_patterns_deduplicates_and_sorts():
    patterns = mutation_diff.module_patterns(
        [
            "application_sdk/b.py",
            "application_sdk/a.py",
            "application_sdk/b.py",
        ]
    )
    assert patterns == ["application_sdk.a.*", "application_sdk.b.*"]


# ---------------------------------------------------- changed file filtering


def test_changed_python_files_drops_excluded_subtrees(monkeypatch):
    diff_output = "\n".join(
        [
            "application_sdk/services/objectstore.py",
            "application_sdk/test_utils/helpers.py",
            "application_sdk/testing/e2e/harness.py",
        ]
    )

    class FakeProc:
        stdout = diff_output

    monkeypatch.setattr(mutation_diff.subprocess, "run", lambda *a, **k: FakeProc())
    assert mutation_diff.changed_python_files("origin/main") == [
        "application_sdk/services/objectstore.py"
    ]


def test_excluded_prefixes_match_pyproject_do_not_mutate():
    """Guard against the script and [tool.mutmut] drifting apart."""
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)["tool"]["mutmut"]
    from_config = {p.removesuffix("*") for p in config["do_not_mutate"]}
    assert from_config == set(mutation_diff.EXCLUDED_PREFIXES)


# ------------------------------------------------------------ results parse


RESULTS_OUTPUT = """\
    application_sdk.services.objectstore.x_upload__mutmut_1: killed
    application_sdk.services.objectstore.x_upload__mutmut_2: survived
    application_sdk.services.objectstore.x_download__mutmut_1: no tests
    application_sdk.clients.rest.x_get__mutmut_9: survived
    application_sdk.services.objectstore.x_upload__mutmut_3: timeout
"""


def test_parse_results_keeps_only_matching_patterns():
    parsed = mutation_diff.parse_results(
        RESULTS_OUTPUT, ["application_sdk.services.objectstore.*"]
    )
    assert parsed == {
        "application_sdk.services.objectstore.x_upload__mutmut_1": "killed",
        "application_sdk.services.objectstore.x_upload__mutmut_2": "survived",
        "application_sdk.services.objectstore.x_download__mutmut_1": "no tests",
        "application_sdk.services.objectstore.x_upload__mutmut_3": "timeout",
    }


def test_parse_results_tolerates_blank_and_malformed_lines():
    assert mutation_diff.parse_results("\n   \nnot-a-result\n", ["*"]) == {}


# ------------------------------------------------------------------- score


def test_score_counts_detected_and_survived():
    status = {
        "a__mutmut_1": "killed",
        "a__mutmut_2": "survived",
        "a__mutmut_3": "timeout",
        "a__mutmut_4": "no tests",
    }
    mutation_score, counts = mutation_diff.score(status)
    assert mutation_score == pytest.approx(2 / 3)
    assert counts["no tests"] == 1


def test_score_none_when_nothing_decided():
    mutation_score, _ = mutation_diff.score({"a__mutmut_1": "no tests"})
    assert mutation_score is None


# ----------------------------------------------------------------- summary


def test_render_summary_no_changed_files():
    text = mutation_diff.render_summary([], {}, None, Counter())
    assert "nothing to mutate" in text


def test_render_summary_no_mutants_for_diff():
    text = mutation_diff.render_summary(["application_sdk/a.py"], {}, None, Counter())
    assert "no mutants" in text


def test_render_summary_scorecard_lists_survivors():
    status = {
        "application_sdk.a.x_f__mutmut_1": "killed",
        "application_sdk.a.x_f__mutmut_2": "survived",
        "application_sdk.a.x_g__mutmut_1": "no tests",
    }
    mutation_score, counts = mutation_diff.score(status)
    text = mutation_diff.render_summary(
        ["application_sdk/a.py"], status, mutation_score, counts
    )
    assert "**50%**" in text
    assert "application_sdk.a.x_f__mutmut_2" in text
    assert "application_sdk.a.x_g__mutmut_1" in text
    assert "mutmut show" in text


def test_render_summary_caps_gap_list():
    status = {
        f"application_sdk.a.x_f__mutmut_{i}": "survived"
        for i in range(mutation_diff.MAX_LISTED_GAPS + 10)
    }
    mutation_score, counts = mutation_diff.score(status)
    text = mutation_diff.render_summary(
        ["application_sdk/a.py"], status, mutation_score, counts
    )
    assert text.count("— survived") == mutation_diff.MAX_LISTED_GAPS
    assert "…and 10 more" in text


# ------------------------------------------------------------ run_mutmut


class FakeRunProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_mutmut_success(monkeypatch):
    monkeypatch.setattr(
        mutation_diff.subprocess,
        "run",
        lambda *a, **k: FakeRunProc(0, stdout="ok"),
    )
    assert mutation_diff.run_mutmut(["application_sdk.a.*"]) == (True, "ok")


def test_run_mutmut_nothing_matches_is_not_an_error(monkeypatch):
    stderr = f"AssertionError: {mutation_diff.NOTHING_MATCHES_MARKER}\n"
    monkeypatch.setattr(
        mutation_diff.subprocess,
        "run",
        lambda *a, **k: FakeRunProc(1, stderr=stderr),
    )
    measured, _ = mutation_diff.run_mutmut(["application_sdk.a.*"])
    assert measured is False


def test_run_mutmut_other_failure_raises(monkeypatch):
    monkeypatch.setattr(
        mutation_diff.subprocess,
        "run",
        lambda *a, **k: FakeRunProc(2, stderr="boom"),
    )
    with pytest.raises(SystemExit):
        mutation_diff.run_mutmut(["application_sdk.a.*"])


# -------------------------------------------------------------------- main


def test_main_no_changed_files_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(mutation_diff, "changed_python_files", lambda base_ref: [])
    assert mutation_diff.main([]) == 0
    assert "nothing to mutate" in capsys.readouterr().out


def test_main_advisory_never_fails_on_low_score(monkeypatch):
    monkeypatch.setattr(
        mutation_diff,
        "changed_python_files",
        lambda base_ref: ["application_sdk/a.py"],
    )
    monkeypatch.setattr(mutation_diff, "run_mutmut", lambda patterns: (True, ""))
    monkeypatch.setattr(mutation_diff, "collect_results", lambda: "")
    monkeypatch.setattr(
        mutation_diff,
        "parse_results",
        lambda output, patterns: {"application_sdk.a.x_f__mutmut_1": "survived"},
    )
    assert mutation_diff.main([]) == 0


def test_main_min_score_gates(monkeypatch):
    monkeypatch.setattr(
        mutation_diff,
        "changed_python_files",
        lambda base_ref: ["application_sdk/a.py"],
    )
    monkeypatch.setattr(mutation_diff, "run_mutmut", lambda patterns: (True, ""))
    monkeypatch.setattr(mutation_diff, "collect_results", lambda: "")
    monkeypatch.setattr(
        mutation_diff,
        "parse_results",
        lambda output, patterns: {
            "application_sdk.a.x_f__mutmut_1": "survived",
            "application_sdk.a.x_f__mutmut_2": "killed",
        },
    )
    assert mutation_diff.main(["--min-score", "0.6"]) == 1
    assert mutation_diff.main(["--min-score", "0.4"]) == 0


def test_main_writes_github_step_summary(monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(mutation_diff, "changed_python_files", lambda base_ref: [])
    mutation_diff.main([])
    assert "nothing to mutate" in summary.read_text()
