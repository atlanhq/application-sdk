"""Tests for .github/scripts/publish_fleet_dashboard.py.

`aws` is stubbed through the module's single `run` seam, with a fake bucket
backing it, so the download → merge → re-upload sequence is exercised end to end
without network or credentials. The sequence is the point: its failure mode in
the shell original was a step that uploaded the per-repo document and then died
before ever writing a history line, leaving a dashboard with data and no trend.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from publish_fleet_dashboard import (  # noqa: E402
    BUCKET,
    merge_history,
    publish,
    refresh_manifest,
)

PREFIX = "gate-enforcement-dashboard"


class FakeS3:
    """A dict-backed S3 double driving the module's `run` seam."""

    def __init__(self, initial=None):
        self.objects = dict(initial or {})

    def __call__(self, args: list) -> tuple:
        if args[:2] == ["s3", "cp"]:
            src, dst = args[2], args[3]
            if src.startswith(BUCKET):
                if src not in self.objects:
                    return 1, ""  # a missing object: first publish
                Path(dst).write_text(self.objects[src])
                return 0, ""
            self.objects[dst] = Path(src).read_text()
            return 0, ""
        if args[:2] == ["s3", "ls"]:
            listing = "".join(
                f"2026-08-14 12:00:00       1234 {key.rsplit('/', 1)[-1]}\n"
                for key in sorted(self.objects)
                if key.startswith(args[2])
            )
            return 0, listing
        raise AssertionError(f"unexpected aws call: {args}")


def _scan_dir(
    tmp_path: Path, repos=("atlanhq_atlan-mysql-app",), with_fleet=True
) -> Path:
    scan = tmp_path / "scan"
    (scan / "repos").mkdir(parents=True)
    for slug in repos:
        (scan / "repos" / f"{slug}.json").write_text(
            json.dumps({"repo": slug.replace("_", "/", 1), "gated": True})
        )
        (scan / f"history_{slug}.jsonl").write_text(
            json.dumps({"date": "2026-08-14", "gated": True}) + "\n"
        )
    if with_fleet:
        (scan / "fleet.json").write_text(
            json.dumps({"fleetSize": len(repos), "gated": 1})
        )
        (scan / "history_fleet.jsonl").write_text(
            json.dumps({"date": "2026-08-14", "gated": 1}) + "\n"
        )
    return scan


def test_full_fleet_publish_writes_every_object(tmp_path):
    s3 = FakeS3()
    summary = publish(
        _scan_dir(tmp_path), PREFIX, single_repo=False, tmp_dir=tmp_path, run=s3
    )

    assert f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-mysql-app.json" in s3.objects
    assert f"{BUCKET}/{PREFIX}/fleet.json" in s3.objects
    assert f"{BUCKET}/{PREFIX}/history/atlanhq_atlan-mysql-app.jsonl" in s3.objects
    assert f"{BUCKET}/{PREFIX}/history/fleet.jsonl" in s3.objects
    assert summary["fleetPublished"] is True


def test_first_publish_still_writes_history(tmp_path):
    """The regression this script exists for: with no stored history object yet,
    the merge must still produce one rather than aborting the step."""
    s3 = FakeS3()
    publish(_scan_dir(tmp_path), PREFIX, single_repo=False, tmp_dir=tmp_path, run=s3)
    stored = s3.objects[f"{BUCKET}/{PREFIX}/history/atlanhq_atlan-mysql-app.jsonl"]
    assert json.loads(stored.strip())["date"] == "2026-08-14"


def test_history_merge_is_append_only_and_deduplicated(tmp_path):
    key = f"{BUCKET}/{PREFIX}/history/slug.jsonl"
    s3 = FakeS3({key: json.dumps({"date": "2026-08-13", "gated": False}) + "\n"})
    local = tmp_path / "history_slug.jsonl"
    local.write_text(json.dumps({"date": "2026-08-14", "gated": True}) + "\n")

    merge_history(local, PREFIX, "slug", tmp_path, run=s3)
    merge_history(local, PREFIX, "slug", tmp_path, run=s3)

    lines = [ln for ln in s3.objects[key].splitlines() if ln.strip()]
    assert len(lines) == 2  # yesterday preserved, today added once
    assert {json.loads(ln)["date"] for ln in lines} == {"2026-08-13", "2026-08-14"}


def test_single_repo_mode_never_touches_the_fleet_aggregate(tmp_path):
    """A one-repo scan has no fleet view; publishing one would read as the
    fleet collapsing to a single repo."""
    fleet_key = f"{BUCKET}/{PREFIX}/fleet.json"
    s3 = FakeS3({fleet_key: json.dumps({"fleetSize": 166, "gated": 5})})
    summary = publish(
        _scan_dir(tmp_path), PREFIX, single_repo=True, tmp_dir=tmp_path, run=s3
    )

    assert json.loads(s3.objects[fleet_key])["fleetSize"] == 166
    assert f"{BUCKET}/{PREFIX}/history/fleet.jsonl" not in s3.objects
    assert summary["fleetPublished"] is False


def test_manifest_is_built_from_the_bucket_not_the_local_scan(tmp_path):
    """In single-repo mode the local directory holds one file; a manifest built
    from it would hide every other repo from the dashboard."""
    s3 = FakeS3(
        {
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-openapi-app.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-metabase-app.json": "{}",
        }
    )
    publish(_scan_dir(tmp_path), PREFIX, single_repo=True, tmp_dir=tmp_path, run=s3)
    manifest = json.loads(s3.objects[f"{BUCKET}/{PREFIX}/repos.json"])
    assert manifest == [
        "atlanhq_atlan-metabase-app.json",
        "atlanhq_atlan-mysql-app.json",
        "atlanhq_atlan-openapi-app.json",
    ]


def test_refresh_manifest_ignores_non_json_keys(tmp_path):
    s3 = FakeS3(
        {
            f"{BUCKET}/{PREFIX}/repos/a.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/README.txt": "x",
        }
    )
    assert refresh_manifest(PREFIX, tmp_path, run=s3) == ["a.json"]


def test_empty_scan_is_refused(tmp_path):
    """Publishing nothing would leave a stale dashboard reading as current."""
    scan = tmp_path / "scan"
    (scan / "repos").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="no per-repo documents"):
        publish(scan, PREFIX, single_repo=False, tmp_dir=tmp_path, run=FakeS3())


def test_missing_fleet_aggregate_on_a_full_run_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="fleet.json"):
        publish(
            _scan_dir(tmp_path, with_fleet=False),
            PREFIX,
            single_repo=False,
            tmp_dir=tmp_path,
            run=FakeS3(),
        )


def test_upload_failure_is_not_swallowed(tmp_path):
    def failing(args: list) -> tuple:
        if args[:2] == ["s3", "cp"] and not args[2].startswith(BUCKET):
            return 1, ""
        return 0, ""

    with pytest.raises(RuntimeError, match="failed to upload"):
        publish(
            _scan_dir(tmp_path),
            PREFIX,
            single_repo=False,
            tmp_dir=tmp_path,
            run=failing,
        )
