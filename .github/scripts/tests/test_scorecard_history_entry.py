"""Tests for .github/scripts/scorecard_history_entry.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scorecard_history_entry as she

_SCORECARD = {
    "repo": "atlanhq/atlan-mysql-app",
    "aggregate": {
        "score": 74,
        "grade": "B",
        "maturity": "silver",
        "cappedBy": ["e2e-present"],
    },
    "tiers": [],
}


def test_history_entry_projects_aggregate():
    entry = she.history_entry(_SCORECARD, "2026-07-21")
    assert entry == {
        "date": "2026-07-21",
        "repo": "atlanhq/atlan-mysql-app",
        "score": 74,
        "grade": "B",
        "maturity": "silver",
        "cappedBy": ["e2e-present"],
    }


def test_history_entry_tolerates_missing_aggregate():
    entry = she.history_entry({"repo": "r"}, "2026-07-21")
    assert entry["repo"] == "r"
    assert entry["score"] is None
    assert entry["cappedBy"] == []


def test_main_appends_jsonl(tmp_path, monkeypatch):
    sc = tmp_path / "test-readiness.json"
    sc.write_text(json.dumps(_SCORECARD), encoding="utf-8")
    out = tmp_path / "history.jsonl"

    rc = she.main(["--scorecard", str(sc), "--date", "2026-07-21", "--out", str(out)])
    assert rc == 0

    # append semantics: a second run adds a second line
    she.main(["--scorecard", str(sc), "--date", "2026-07-22", "--out", str(out)])
    lines = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["date"] == "2026-07-21"
    assert lines[1]["date"] == "2026-07-22"
    assert lines[0]["grade"] == "B"
