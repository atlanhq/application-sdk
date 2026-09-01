"""Tests for the Renovate dashboard staging step (FND-909).

The behaviour being preserved is what the old shell loops did, minus the network:
merge each repo's history with what is already published, deduplicate, and keep
fleet aggregates off single-repo runs. Getting the merge wrong loses published
history, which is unrecoverable — hence the emphasis on the merge cases.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))

import renovate_dashboard_stage as stage  # noqa: E402


def _scanner_output(tmp_path, repos=(), histories=None, fleet=True):
    """Lay out a directory shaped like the renovate-scan CLI's --out."""
    out = tmp_path / "output"
    (out / "repos").mkdir(parents=True)
    for slug in repos:
        (out / "repos" / f"{slug}.json").write_text(json.dumps([{"number": 1}]))
    for slug, rows in (histories or {}).items():
        (out / f"history_{slug}.jsonl").write_text("".join(f"{r}\n" for r in rows))
    if fleet:
        (out / "fleet.json").write_text(json.dumps({"totalOpenPRs": 3}))
    return out


def _existing(tmp_path, histories=None):
    existing = tmp_path / "existing"
    existing.mkdir()
    for slug, rows in (histories or {}).items():
        (existing / f"{slug}.jsonl").write_text("".join(f"{r}\n" for r in rows))
    return existing


def test_every_repo_summary_is_staged(tmp_path):
    out = _scanner_output(tmp_path, repos=["atlanhq_a", "atlanhq_b"])
    counts = stage.stage(out, _existing(tmp_path), tmp_path / "stage")

    staged = sorted(p.name for p in (tmp_path / "stage" / "repos").glob("*.json"))
    assert staged == ["atlanhq_a.json", "atlanhq_b.json"]
    assert counts["repos"] == 2


def test_history_merges_existing_with_new(tmp_path):
    out = _scanner_output(
        tmp_path, repos=["atlanhq_a"], histories={"atlanhq_a": ['{"d":"2026-08-29"}']}
    )
    existing = _existing(tmp_path, {"atlanhq_a": ['{"d":"2026-08-28"}']})

    stage.stage(out, existing, tmp_path / "stage")

    lines = (tmp_path / "stage" / "history" / "atlanhq_a.jsonl").read_text().split()
    assert lines == ['{"d":"2026-08-28"}', '{"d":"2026-08-29"}']


def test_history_is_idempotent_across_reruns(tmp_path):
    # Two runs the same day emit an identical row; it must not accumulate.
    row = '{"d":"2026-08-29","open":5}'
    out = _scanner_output(tmp_path, repos=["atlanhq_a"], histories={"atlanhq_a": [row]})
    existing = _existing(tmp_path, {"atlanhq_a": [row]})

    stage.stage(out, existing, tmp_path / "stage")

    assert (
        tmp_path / "stage" / "history" / "atlanhq_a.jsonl"
    ).read_text() == f"{row}\n"


def test_history_survives_a_repo_with_no_published_history(tmp_path):
    # First time a repo is seen: no existing file, and that is not an error.
    out = _scanner_output(
        tmp_path,
        repos=["atlanhq_new"],
        histories={"atlanhq_new": ['{"d":"2026-08-29"}']},
    )

    stage.stage(out, _existing(tmp_path), tmp_path / "stage")

    assert (tmp_path / "stage" / "history" / "atlanhq_new.jsonl").read_text() == (
        '{"d":"2026-08-29"}\n'
    )


def test_full_fleet_run_stages_fleet_aggregates(tmp_path):
    out = _scanner_output(
        tmp_path, repos=["atlanhq_a"], histories={"fleet": ['{"d":"2026-08-29"}']}
    )
    existing = _existing(tmp_path, {"fleet": ['{"d":"2026-08-28"}']})

    counts = stage.stage(out, existing, tmp_path / "stage", single_repo="")

    assert (tmp_path / "stage" / "fleet.json").is_file()
    assert counts["fleet_json"] == 1
    fleet_history = tmp_path / "stage" / "history" / "fleet.jsonl"
    assert fleet_history.read_text().split() == [
        '{"d":"2026-08-28"}',
        '{"d":"2026-08-29"}',
    ]


def test_single_repo_run_never_stages_fleet_aggregates(tmp_path):
    # The scanner only has one repo's data; publishing either would overwrite the
    # real fleet view with a partial one.
    out = _scanner_output(
        tmp_path, repos=["atlanhq_a"], histories={"fleet": ['{"d":"2026-08-29"}']}
    )

    counts = stage.stage(
        out, _existing(tmp_path), tmp_path / "stage", single_repo="atlanhq/a"
    )

    assert not (tmp_path / "stage" / "fleet.json").exists()
    assert not (tmp_path / "stage" / "history" / "fleet.jsonl").exists()
    assert counts["fleet_json"] == 0


def test_fleet_history_is_never_staged_as_a_repo(tmp_path):
    # history_fleet.jsonl sits beside the per-repo files and must not be treated
    # as a repo named "fleet" — the old shell loop skipped it explicitly.
    out = _scanner_output(
        tmp_path, histories={"fleet": ['{"d":"1"}'], "atlanhq_a": ['{"d":"2"}']}
    )

    counts = stage.stage(out, _existing(tmp_path), tmp_path / "stage", single_repo="x")

    assert counts["histories"] == 1  # atlanhq_a only


def test_blank_lines_are_dropped(tmp_path):
    out = _scanner_output(
        tmp_path, repos=["atlanhq_a"], histories={"atlanhq_a": ['{"d":"1"}', "", "  "]}
    )

    stage.stage(out, _existing(tmp_path), tmp_path / "stage")

    assert (tmp_path / "stage" / "history" / "atlanhq_a.jsonl").read_text() == (
        '{"d":"1"}\n'
    )


def test_full_fleet_run_refuses_to_stage_without_the_aggregate(tmp_path):
    """Fail closed: per-repo files without fleet.json publish stale fleet numbers.

    The `aws s3 cp` this replaced got that for free under `set -euo pipefail`.
    Skipping a missing file quietly would leave the dashboard showing the
    previous run's aggregate beside this run's per-repo data, with nothing red.
    """
    out = _scanner_output(tmp_path, repos=["atlanhq_a"], fleet=False)

    try:
        stage.stage(out, _existing(tmp_path), tmp_path / "stage", single_repo="")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "refusing to stage" in str(exc)


def test_single_repo_run_tolerates_a_missing_aggregate(tmp_path):
    # Single-repo mode never publishes fleet.json, so its absence is expected
    # rather than a fault — the guard above must not fire here.
    out = _scanner_output(tmp_path, repos=["atlanhq_a"], fleet=False)

    counts = stage.stage(
        out, _existing(tmp_path), tmp_path / "stage", single_repo="atlanhq/a"
    )

    assert counts["repos"] == 1
    assert counts["fleet_json"] == 0


def test_main_exits_nonzero_when_the_aggregate_is_missing(tmp_path, capsys):
    out = _scanner_output(tmp_path, repos=["atlanhq_a"], fleet=False)

    rc = stage.main(
        [
            "--output-dir",
            str(out),
            "--existing-history-dir",
            str(_existing(tmp_path)),
            "--stage-dir",
            str(tmp_path / "stage"),
        ]
    )

    assert rc == 1
    assert "refusing to stage" in capsys.readouterr().err


def test_main_reports_missing_scanner_output(tmp_path, capsys):
    rc = stage.main(
        [
            "--output-dir",
            str(tmp_path / "nope"),
            "--existing-history-dir",
            str(tmp_path),
            "--stage-dir",
            str(tmp_path / "stage"),
        ]
    )

    assert rc == 1
    assert "scanner output not found" in capsys.readouterr().err


def test_main_stages_end_to_end(tmp_path, capsys):
    out = _scanner_output(
        tmp_path, repos=["atlanhq_a"], histories={"atlanhq_a": ['{"d":"1"}']}
    )

    rc = stage.main(
        [
            "--output-dir",
            str(out),
            "--existing-history-dir",
            str(_existing(tmp_path)),
            "--stage-dir",
            str(tmp_path / "stage"),
        ]
    )

    assert rc == 0
    assert "staged 1 repo files, 1 histories, fleet.json=yes" in capsys.readouterr().out


# ── the manifest (FND-960) ───────────────────────────────────────────────────


def test_full_fleet_run_stages_a_manifest_of_what_it_scanned(tmp_path):
    out = _scanner_output(tmp_path, repos=["atlanhq_a", "atlanhq_b"])
    counts = stage.stage(out, _existing(tmp_path), tmp_path / "stage")

    manifest = json.loads((tmp_path / "stage" / "repos.json").read_text())
    assert manifest == ["atlanhq_a.json", "atlanhq_b.json"]
    assert counts["manifest"] == 1


def test_the_manifest_names_only_this_run_not_what_the_bucket_holds(tmp_path):
    """The prefix is additive, so every repo that ever published stays stored.

    Building the manifest from an `aws s3 ls` therefore listed 619 repos against
    a fleet of 80 — 452 of them never consumers, written while discovery was
    broken and frozen since. A manifest built from the scan stops naming them
    without deleting anything.
    """
    out = _scanner_output(tmp_path, repos=["atlanhq_still-a-consumer"])
    stage_dir = tmp_path / "stage"
    (stage_dir / "repos").mkdir(parents=True)
    # A leftover from an earlier, broken run, already staged/stored.
    (stage_dir / "repos" / "atlanhq_CARAT.json").write_text("[]")

    stage.stage(out, _existing(tmp_path), stage_dir)

    manifest = json.loads((stage_dir / "repos.json").read_text())
    assert manifest == ["atlanhq_still-a-consumer.json"]
    # Not deleted — the sync is additive; it is simply no longer named.
    assert (stage_dir / "repos" / "atlanhq_CARAT.json").is_file()


def test_single_repo_run_never_stages_a_manifest(tmp_path):
    """One repo must not be allowed to redefine fleet membership."""
    out = _scanner_output(tmp_path, repos=["atlanhq_a"], fleet=False)
    counts = stage.stage(
        out, _existing(tmp_path), tmp_path / "stage", single_repo="atlanhq/a"
    )
    assert not (tmp_path / "stage" / "repos.json").exists()
    assert counts["manifest"] == 0


def test_full_fleet_run_refuses_to_stage_an_empty_manifest(tmp_path):
    """A scan that produced nothing would empty every panel at once."""
    out = _scanner_output(tmp_path, repos=[])
    with pytest.raises(RuntimeError, match="refusing to stage a manifest"):
        stage.stage(out, _existing(tmp_path), tmp_path / "stage")


def test_no_fractional_shrink_rail_blocks_the_first_corrected_run(tmp_path):
    """619 -> 80 is the fix working, not a fault to guard against.

    Any percentage-based cap would refuse exactly this run. #3516 removed that
    class of heuristic from this scan for the same reason.
    """
    out = _scanner_output(tmp_path, repos=[f"atlanhq_app-{i}" for i in range(80)])
    stage_dir = tmp_path / "stage"
    (stage_dir / "repos").mkdir(parents=True)
    for i in range(619):
        (stage_dir / "repos" / f"atlanhq_stale-{i}.json").write_text("[]")

    stage.stage(out, _existing(tmp_path), stage_dir)

    assert len(json.loads((stage_dir / "repos.json").read_text())) == 80
