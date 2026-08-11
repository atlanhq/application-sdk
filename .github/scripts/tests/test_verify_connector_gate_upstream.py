"""Tests for .github/scripts/verify_connector_gate_upstream.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import verify_connector_gate_upstream as mod


def test_evaluate_fails_when_changes_did_not_succeed():
    error, dispatched = mod.evaluate(
        "failure", "success", "success", "success", "success"
    )
    assert error is not None
    assert "Detect Changes" in error
    assert dispatched is False


def test_evaluate_fails_when_matrix_did_not_succeed():
    error, _ = mod.evaluate("success", "failure", "success", "success", "success")
    assert error is not None
    assert "Build Test Matrix" in error


def test_evaluate_fails_when_base_image_failed():
    error, _ = mod.evaluate("success", "success", "failure", "success", "skipped")
    assert error is not None
    assert "Build SDK base image" in error


def test_evaluate_allows_base_image_skipped():
    error, dispatched = mod.evaluate(
        "success", "success", "skipped", "success", "skipped"
    )
    assert error is None
    assert dispatched is True


def test_evaluate_fails_when_connector_tests_failed():
    error, _ = mod.evaluate("success", "success", "success", "failure", "success")
    assert error is not None
    assert "Connector Tests dispatch" in error


def test_evaluate_connector_skipped_is_ok_but_not_dispatched():
    error, dispatched = mod.evaluate(
        "success", "success", "success", "skipped", "success"
    )
    assert error is None
    assert dispatched is False


def test_evaluate_connector_success_is_dispatched():
    error, dispatched = mod.evaluate(
        "success", "success", "success", "success", "success"
    )
    assert error is None
    assert dispatched is True


def test_main_exits_1_on_upstream_failure(capsys):
    rc = mod.main(
        [
            "--changes-result",
            "failure",
            "--matrix-result",
            "success",
            "--base-image-result",
            "success",
            "--merge-base-image-result",
            "success",
            "--connector-result",
            "success",
        ]
    )
    assert rc == 1
    assert "::error::" in capsys.readouterr().out


def test_main_writes_dispatched_true(tmp_path, monkeypatch):
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    rc = mod.main(
        [
            "--changes-result",
            "success",
            "--matrix-result",
            "success",
            "--base-image-result",
            "success",
            "--merge-base-image-result",
            "success",
            "--connector-result",
            "success",
        ]
    )

    assert rc == 0
    assert output_file.read_text() == "dispatched=true\n"


def test_main_writes_dispatched_false_for_docs_only_skip(tmp_path, monkeypatch):
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    rc = mod.main(
        [
            "--changes-result",
            "success",
            "--matrix-result",
            "success",
            "--base-image-result",
            "skipped",
            "--merge-base-image-result",
            "skipped",
            "--connector-result",
            "skipped",
        ]
    )

    assert rc == 0
    assert output_file.read_text() == "dispatched=false\n"


def test_main_prints_when_no_github_output(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    rc = mod.main(
        [
            "--changes-result",
            "success",
            "--matrix-result",
            "success",
            "--base-image-result",
            "success",
            "--merge-base-image-result",
            "success",
            "--connector-result",
            "success",
        ]
    )

    assert rc == 0
    assert "dispatched=true" in capsys.readouterr().out


# --- the manifest-merge job ------------------------------------------------
# The PR base image is built by a per-arch matrix and combined by a separate
# merge job. A failed arch leg leaves that merge SKIPPED rather than failed, and
# "skipped" is the benign value for both jobs — so neither result on its own
# separates "no base image was requested" (merge_group) from "the base image
# never got built". The gate is fail-closed; this is where that has to hold.


def test_evaluate_fails_when_the_manifest_merge_failed():
    error, _ = mod.evaluate("success", "success", "success", "success", "failure")
    assert error is not None
    assert "Merge SDK base image manifest" in error


def test_evaluate_fails_when_the_legs_passed_but_the_merge_was_skipped():
    """The pair neither single check catches.

    Both arch legs green ⇒ the merge should have run. If it did not, the
    connectors were dispatched with `base_image_ref` pointing at a manifest tag
    that was never created, and every connector build fails on a missing image
    one repo away from the cause.
    """
    error, dispatched = mod.evaluate(
        "success", "success", "success", "success", "skipped"
    )
    assert error is not None
    assert "never created" in error
    assert dispatched is False


def test_evaluate_allows_both_base_image_jobs_skipped():
    # The merge_group path: no PR base image is built at all, and connectors
    # build FROM the released base. Still a legitimate dispatch.
    error, dispatched = mod.evaluate(
        "success", "success", "skipped", "success", "skipped"
    )
    assert error is None
    assert dispatched is True


def test_main_requires_the_merge_result(capsys):
    """Omitting it must be a hard argparse error, not a defaulted pass.

    A default of "skipped" would make a forgotten flag indistinguishable from
    the merge_group path — the exact silent-pass this gate exists to refuse.
    """
    with pytest.raises(SystemExit):
        mod.main(
            [
                "--changes-result",
                "success",
                "--matrix-result",
                "success",
                "--base-image-result",
                "success",
                "--connector-result",
                "success",
            ]
        )
