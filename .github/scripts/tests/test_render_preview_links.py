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
