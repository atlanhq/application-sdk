"""Tests for .github/scripts/flag_job_timeouts.py."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import flag_job_timeouts as mod

REPO_ROOT = Path(__file__).resolve().parents[3]
START = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

CALLER = """
jobs:
  changes:
    name: Detect Changes
    runs-on: ubuntu-latest
    timeout-minutes: 10
  tests:
    name: SDK Tests
    uses: ./callee.yaml
  gate:
    name: Gate
    runs-on: ubuntu-latest
"""

CALLEE = """
on:
  workflow_call: {}
jobs:
  unit:
    name: Unit Tests
    timeout-minutes: 15
    strategy:
      matrix:
        os: [a, b]
  bats:
    name: Script Tests (bats)
    timeout-minutes: 5
  dynamic:
    name: Lint ${{ matrix.tool }}
    timeout-minutes: 7
  computed:
    name: Computed cap
    timeout-minutes: ${{ inputs.minutes }}
"""


def _workflows(tmp_path: Path) -> Path:
    (tmp_path / "caller.yaml").write_text(CALLER)
    (tmp_path / "callee.yaml").write_text(CALLEE)
    return tmp_path / "caller.yaml"


def _caps(tmp_path: Path) -> list[mod.JobCap]:
    return mod.collect_caps(_workflows(tmp_path), tmp_path)


def _cap_for(caps: list[mod.JobCap], name: str) -> int | None:
    match = next((c for c in caps if c.pattern.fullmatch(name)), None)
    return match.minutes if match else None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(name: str, seconds: float, conclusion: str = "cancelled", **over) -> dict:
    job = {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": _iso(START),
        "completed_at": _iso(START + timedelta(seconds=seconds)),
    }
    job.update(over)
    return job


# ---------------------------------------------------------------------------
# collect_caps
# ---------------------------------------------------------------------------


def test_caps_resolve_top_level_reusable_and_matrix_names(tmp_path):
    caps = _caps(tmp_path)
    assert _cap_for(caps, "Detect Changes") == 10
    assert _cap_for(caps, "SDK Tests / Unit Tests (a)") == 15
    assert _cap_for(caps, "SDK Tests / Unit Tests") == 15
    assert _cap_for(caps, "SDK Tests / Script Tests (bats)") == 5
    assert _cap_for(caps, "SDK Tests / Lint ruff") == 7


def test_job_without_timeout_gets_the_github_default(tmp_path):
    assert _cap_for(_caps(tmp_path), "Gate") == mod.DEFAULT_TIMEOUT_MINUTES


def test_expression_cap_is_unknown_and_skipped(tmp_path):
    assert _cap_for(_caps(tmp_path), "SDK Tests / Computed cap") is None


def test_skipped_reusable_caller_has_no_cap(tmp_path):
    # A skipped reusable call appears in the API as one job named after the
    # caller; it never ran, so there is nothing to flag.
    assert _cap_for(_caps(tmp_path), "SDK Tests") is None


def test_real_sdk_gate_resolves_its_reusable_jobs():
    caps = mod.collect_caps(REPO_ROOT / ".github/workflows/sdk-gate.yaml", REPO_ROOT)
    assert _cap_for(caps, "SDK Tests / Unit Tests (3.12, windows-latest)") is not None
    assert _cap_for(caps, "SDK Tests / Storage Emulator Tests") is not None


# ---------------------------------------------------------------------------
# find_timeouts
# ---------------------------------------------------------------------------


def test_flags_a_job_that_ran_to_its_cap(tmp_path):
    flagged = mod.find_timeouts(
        [_job("SDK Tests / Unit Tests (b)", 15 * 60 + 3)], _caps(tmp_path), 30
    )
    assert flagged == [("SDK Tests / Unit Tests (b)", 903.0, 15, "cancelled")]


def test_flags_a_job_inside_the_margin(tmp_path):
    flagged = mod.find_timeouts(
        [_job("SDK Tests / Unit Tests (b)", 15 * 60 - 30)], _caps(tmp_path), 30
    )
    assert len(flagged) == 1


def test_does_not_flag_a_superseded_cancellation(tmp_path):
    flagged = mod.find_timeouts(
        [_job("SDK Tests / Unit Tests (b)", 15 * 60 - 31)], _caps(tmp_path), 30
    )
    assert flagged == []


def test_ignores_unfinished_unknown_and_untimed_jobs(tmp_path):
    caps = _caps(tmp_path)
    jobs = [
        _job("SDK Tests / Unit Tests (a)", 10_000, status="in_progress"),
        _job("Some Other Workflow Job", 10_000),
        _job("SDK Tests / Unit Tests (a)", 10_000, started_at=None),
    ]
    assert mod.find_timeouts(jobs, caps, 30) == []


# ---------------------------------------------------------------------------
# list_jobs / main
# ---------------------------------------------------------------------------


def _ndjson(items: list[dict]) -> subprocess.CompletedProcess:
    out = "\n".join(json.dumps(i) for i in items) + ("\n" if items else "")
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=out, stderr="")


def test_list_jobs_reads_the_attempt_and_paginates(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _ndjson([{"name": "x"}])

    monkeypatch.setattr(mod, "run", fake_run)
    assert mod.list_jobs("o/r", "123", "2") == [{"name": "x"}]
    assert "--paginate" in seen["cmd"]
    assert any("actions/runs/123/attempts/2/jobs" in c for c in seen["cmd"])


def test_main_warns_and_writes_summary_but_exits_zero(tmp_path, monkeypatch, capsys):
    workflow = _workflows(tmp_path)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(
        mod,
        "run",
        lambda cmd, **kw: _ndjson(
            [
                _job("SDK Tests / Unit Tests (a)", 15 * 60),
                _job("SDK Tests / Unit Tests (b)", 60),
            ]
        ),
    )
    rc = mod.main(
        [
            "--repo",
            "o/r",
            "--run-id",
            "1",
            "--run-attempt",
            "1",
            "--workflow",
            str(workflow),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("::warning title=Job timeout::") == 1
    assert "SDK Tests / Unit Tests (a) ran 900s" in out
    assert "Unit Tests (a)" in summary.read_text()


def test_main_quiet_when_nothing_timed_out(tmp_path, monkeypatch, capsys):
    workflow = _workflows(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(mod, "run", lambda cmd, **kw: _ndjson([_job("Gate", 5)]))
    rc = mod.main(
        [
            "--repo",
            "o/r",
            "--run-id",
            "1",
            "--run-attempt",
            "1",
            "--workflow",
            str(workflow),
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    assert "::warning" not in capsys.readouterr().out
