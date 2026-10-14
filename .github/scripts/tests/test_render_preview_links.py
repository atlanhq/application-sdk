"""Tests for .github/scripts/render_preview_links.py.

The preview files come from a leg that ran the pull request's own code, so the
renderer treats them as untrusted: only the exact link shape the harness writes
reaches the PR comment. Synthetic identifiers only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import preview_baseline_run as baseline
import render_preview_links as mod

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REUSABLE = _REPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml"
_GUID = "0f0e0d0c-0b0a-4908-8706-050403020100"
_LINK = f"https://example-tenant.atlan.com/assets/{_GUID}/overview"
_LEG = "e2e-preview-atlan-example-app-example-e2e-aws"


def _write(root: Path, body: object, leg: str = _LEG) -> None:
    out = root / leg / "ExampleE2E.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps(body))


def test_a_leg_with_links_renders_one_row_per_type(tmp_path: Path) -> None:
    _write(tmp_path, {"suite": "ExampleE2E", "links": {"Table": _LINK}})
    md = mod.render(mod.collect(tmp_path))
    assert (
        f"| atlan-example-app-example-e2e-aws | ExampleE2E | Table | [open]({_LINK}) |"
        in md
    )


def test_no_preview_files_renders_nothing(tmp_path: Path) -> None:
    assert mod.render(mod.collect(tmp_path)) == ""


def test_a_link_of_any_other_shape_is_dropped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "suite": "ExampleE2E",
            "links": {
                "Table": "javascript:alert(1)",
                "View": "https://example-tenant.atlan.com/assets/x)[evil](https://e/overview",
                "Column": _LINK,
            },
        },
    )
    md = mod.render(mod.collect(tmp_path))
    assert "javascript" not in md and "evil" not in md
    assert "| Column |" in md


def test_a_type_name_carrying_markdown_is_dropped(tmp_path: Path) -> None:
    _write(tmp_path, {"suite": "ExampleE2E", "links": {"Table | x": _LINK}})
    assert mod.render(mod.collect(tmp_path)) == ""


def test_an_unparseable_file_is_skipped(tmp_path: Path) -> None:
    out = tmp_path / _LEG / "ExampleE2E.json"
    out.parent.mkdir(parents=True)
    out.write_text("{not json")
    assert mod.render(mod.collect(tmp_path)) == ""


def test_a_hold_window_is_stated_when_the_harness_records_one(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "suite": "ExampleE2E",
            "links": {"Table": _LINK},
            "expires_at": "2026-01-02T03:04:05Z",
        },
    )
    assert "Kept until 2026-01-02T03:04:05Z" in mod.render(mod.collect(tmp_path))


def test_the_tests_gate_comment_carries_the_preview_section() -> None:
    jobs = yaml.safe_load(_REUSABLE.read_text())["jobs"]
    steps = jobs["tests-passed"]["steps"]
    preview = next(s for s in steps if s.get("id") == "preview")
    assert "render_preview_links.py" in preview["run"]
    comment = next(
        s for s in steps if s.get("name") == "Post unified test-summary comment on PR"
    )
    assert "steps.preview.outputs.markdown" in comment["with"]["message"]


def test_the_e2e_command_verdict_carries_the_preview_section() -> None:
    """A `/e2e` comment starts a dispatched run that never posts the Tests
    Summary, so its verdict comment is where the links have to land."""
    steps = yaml.safe_load(_REUSABLE.read_text())["jobs"]["tests-passed"]["steps"]
    preview = next(s for s in steps if s.get("id") == "preview")
    assert "pull_request" not in preview["if"]
    assert "$RUNNER_TEMP/e2e-preview.md" in preview["run"]
    verdict = next(
        s for s in steps if s.get("name") == "Post the /e2e verdict on the PR"
    )
    assert '--extra-file "$RUNNER_TEMP/e2e-preview.md"' in verdict["run"]


def test_the_hold_reads_the_label_from_the_api_not_the_event() -> None:
    """A dispatched `/e2e` run has no pull_request payload to read labels from."""
    jobs = yaml.safe_load(_REUSABLE.read_text())["jobs"]
    hold = next(
        s for s in jobs["discover-e2e"]["steps"] if s.get("id") == "preview-hold"
    )
    assert "issues/${PR_NUMBER}/labels" in hold["run"]
    assert '"e2e-preview"' in hold["run"]
    assert "inputs.pr-number" in hold["env"]["PR_NUMBER"]
    assert jobs["e2e"]["env"]["E2E_PREVIEW_HOLD_HOURS"] == (
        "${{ needs.discover-e2e.outputs.preview-hold-hours }}"
    )


# ---------------------------------------------------------------------------
# What changed vs the release baseline
# ---------------------------------------------------------------------------


def _counts(root: Path, counts: dict[str, object]) -> mod.Counts:
    _write(root, {"suite": "ExampleE2E", "links": {}, "asset_counts": counts})
    return mod.collect_counts(root)


def test_a_changed_count_is_listed_with_its_delta(tmp_path: Path) -> None:
    now = _counts(tmp_path / "pr", {"Table": 5, "ColumnProcess": 3})
    before = _counts(tmp_path / "base", {"Table": 5})
    md = mod.render_diff(now, before)
    assert "| ColumnProcess | 0 | 3 | +3 |" in md
    assert "| Table |" not in md


def test_counts_identical_to_the_baseline_are_called_out(tmp_path: Path) -> None:
    """A change whose e2e lands exactly what the default branch lands passed
    without showing the change: the comment must say so, not stay silent."""
    now = _counts(tmp_path / "pr", {"Table": 5})
    before = _counts(tmp_path / "base", {"Table": 5})
    assert "match the release baseline exactly" in mod.render_diff(now, before)


def test_no_baseline_is_stated(tmp_path: Path) -> None:
    now = _counts(tmp_path / "pr", {"Table": 5})
    assert "No baseline" in mod.render_diff(now, None)


def test_a_non_integer_count_is_ignored(tmp_path: Path) -> None:
    got = _counts(tmp_path, {"Table": "5", "View": True, "Column": -1, "Schema": 2})
    assert got == {("atlan-example-app-example-e2e-aws", "ExampleE2E"): {"Schema": 2}}


def test_the_baseline_is_the_newest_passing_release_run_but_not_this_one() -> None:
    runs = [
        {"id": 9, "conclusion": "success", "head_branch": "bump-version-main"},
        {"id": 8, "conclusion": "success", "head_branch": "feature/x"},
        {"id": 7, "conclusion": "failure", "head_branch": "bump-version-main"},
        {"id": 6, "conclusion": "success", "head_branch": "bump-version-main"},
    ]
    assert baseline.pick(runs, current_run_id=1) == 9
    assert baseline.pick(runs, current_run_id=9) == 6
    assert baseline.pick(runs[1:3], current_run_id=1) is None
