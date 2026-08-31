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
    orphans,
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
                    # a missing object: first publish (the real aws CLI signature)
                    return (
                        1,
                        "",
                        "fatal error: An error occurred (404) when calling the "
                        'HeadObject operation: Key "..." does not exist',
                    )
                Path(dst).write_text(self.objects[src])
                return 0, "", ""
            self.objects[dst] = Path(src).read_text()
            return 0, "", ""
        if args[:2] == ["s3", "ls"]:
            listing = "".join(
                f"2026-08-14 12:00:00       1234 {key.rsplit('/', 1)[-1]}\n"
                for key in sorted(self.objects)
                if key.startswith(args[2])
            )
            return 0, listing, ""
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


def test_a_changed_entry_replaces_that_day_rather_than_doubling_it(tmp_path):
    """The date is the key, not the line.

    Deduplicating on whole-line identity only collapses a byte-identical rerun.
    A repo that gets gated at noon, or an entry whose shape changed between
    runs, would keep BOTH lines for the day — two contradicting points on the
    same x, with no rule saying which wins. Concretely: the first published
    history carried a `gated-unbypassable` status that the schema no longer
    has, and it must not survive alongside the corrected line for that date.
    """
    key = f"{BUCKET}/{PREFIX}/history/slug.jsonl"
    stale = json.dumps(
        {"date": "2026-08-14", "status": "gated-unbypassable", "unbypassable": True}
    )
    s3 = FakeS3({key: stale + "\n"})
    local = tmp_path / "history_slug.jsonl"
    local.write_text(json.dumps({"date": "2026-08-14", "status": "gated"}) + "\n")

    merge_history(local, PREFIX, "slug", tmp_path, run=s3)

    lines = [ln for ln in s3.objects[key].splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "gated"  # the fresh line wins


def test_unparseable_history_lines_are_carried_not_dropped(tmp_path):
    """Deleting data we cannot interpret is worse than carrying it."""
    key = f"{BUCKET}/{PREFIX}/history/slug.jsonl"
    s3 = FakeS3({key: "not json at all\n" + json.dumps({"date": "2026-08-13"}) + "\n"})
    local = tmp_path / "history_slug.jsonl"
    local.write_text(json.dumps({"date": "2026-08-14"}) + "\n")

    merge_history(local, PREFIX, "slug", tmp_path, run=s3)

    lines = [ln for ln in s3.objects[key].splitlines() if ln.strip()]
    assert lines[0] == "not json at all"
    assert [json.loads(ln)["date"] for ln in lines[1:]] == ["2026-08-13", "2026-08-14"]


def test_history_stays_ordered_by_date(tmp_path):
    """Entries arrive newest-last but must be stored oldest-first, so a consumer
    can read the series without sorting it."""
    key = f"{BUCKET}/{PREFIX}/history/slug.jsonl"
    s3 = FakeS3({key: json.dumps({"date": "2026-08-20"}) + "\n"})
    local = tmp_path / "history_slug.jsonl"
    local.write_text(json.dumps({"date": "2026-08-14"}) + "\n")

    merge_history(local, PREFIX, "slug", tmp_path, run=s3)

    dates = [
        json.loads(ln)["date"] for ln in s3.objects[key].splitlines() if ln.strip()
    ]
    assert dates == ["2026-08-14", "2026-08-20"]


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
            return 1, "", "fatal error: upload failed"
        return 0, "", ""

    with pytest.raises(RuntimeError, match="failed to upload"):
        publish(
            _scan_dir(tmp_path),
            PREFIX,
            single_repo=False,
            tmp_dir=tmp_path,
            run=failing,
        )


def test_history_download_failure_aborts_before_upload(tmp_path):
    """A non-NotFound download failure (AccessDenied, throttling, timeout) must
    NOT be treated as "no history yet": merging only today's line and uploading
    it over the stored history would silently truncate the trend line."""
    key = f"{BUCKET}/{PREFIX}/history/slug.jsonl"
    stored = json.dumps({"date": "2026-08-13", "gated": False}) + "\n"
    s3 = FakeS3({key: stored})

    def denied(args: list) -> tuple:
        if args[:2] == ["s3", "cp"] and args[2].startswith(BUCKET):
            return 1, "", "fatal error: An error occurred (AccessDenied)"
        return s3(args)

    local = tmp_path / "history_slug.jsonl"
    local.write_text(json.dumps({"date": "2026-08-14", "gated": True}) + "\n")

    with pytest.raises(RuntimeError, match="failed to download"):
        merge_history(local, PREFIX, "slug", tmp_path, run=denied)

    assert s3.objects[key] == stored  # history untouched


def test_history_download_non_404_with_not_found_phrase_is_not_first_publish(tmp_path):
    """A non-404 error whose stderr happens to embed "does not exist" must NOT
    be misclassified as a first publish — only the (404)+HeadObject signature
    is. This guards the tightened NotFound predicate."""
    key = f"{BUCKET}/{PREFIX}/history/slug.jsonl"
    stored = json.dumps({"date": "2026-08-13", "gated": False}) + "\n"
    s3 = FakeS3({key: stored})

    def throttled(args: list) -> tuple:
        if args[:2] == ["s3", "cp"] and args[2].startswith(BUCKET):
            # a 503 SlowDown that mentions a key that "does not exist" — not a 404
            return (
                1,
                "",
                "fatal error: An error occurred (503) SlowDown: the requested key does not exist",
            )
        return s3(args)

    local = tmp_path / "history_slug.jsonl"
    local.write_text(json.dumps({"date": "2026-08-14", "gated": True}) + "\n")

    with pytest.raises(RuntimeError, match="failed to download"):
        merge_history(local, PREFIX, "slug", tmp_path, run=throttled)

    assert s3.objects[key] == stored  # history untouched


# ── departed repos (FND-960) ─────────────────────────────────────────────────


def test_orphans_are_the_stored_objects_a_scan_did_not_produce():
    assert orphans(["a.json", "b.json"], {"a.json"}) == ["b.json"]
    assert orphans(["a.json"], {"a.json", "b.json"}) == []


def test_a_full_fleet_run_retires_a_repo_that_left_the_fleet(tmp_path):
    """The scanners exclude archived repos, so the scan stops covering one.

    Because the manifest is rebuilt by listing the bucket, its object used to
    keep it enumerated on every dashboard forever.
    """
    s3 = FakeS3(
        {
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-mysql-app.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-dead-app.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-hive-app.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-adf-app.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-gcs-app.json": "{}",
        }
    )
    publish(
        _scan_dir(
            tmp_path,
            repos=(
                "atlanhq_atlan-mysql-app",
                "atlanhq_atlan-hive-app",
                "atlanhq_atlan-adf-app",
                "atlanhq_atlan-gcs-app",
            ),
        ),
        PREFIX,
        single_repo=False,
        tmp_dir=tmp_path,
        run=s3,
    )
    manifest = json.loads(s3.objects[f"{BUCKET}/{PREFIX}/repos.json"])
    assert "atlanhq_atlan-dead-app.json" not in manifest
    assert "atlanhq_atlan-mysql-app.json" in manifest


def test_a_retired_repo_is_omitted_but_never_deleted(tmp_path):
    """Excluding is idempotent and reversible; deleting dashboard history is not."""
    dead = f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-dead-app.json"
    s3 = FakeS3(
        {
            dead: "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-mysql-app.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-hive-app.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-adf-app.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-gcs-app.json": "{}",
            f"{BUCKET}/{PREFIX}/history/atlanhq_atlan-dead-app.jsonl": "{}\n",
        }
    )
    publish(
        _scan_dir(
            tmp_path,
            repos=(
                "atlanhq_atlan-mysql-app",
                "atlanhq_atlan-hive-app",
                "atlanhq_atlan-adf-app",
                "atlanhq_atlan-gcs-app",
            ),
        ),
        PREFIX,
        single_repo=False,
        tmp_dir=tmp_path,
        run=s3,
    )
    assert dead in s3.objects
    assert f"{BUCKET}/{PREFIX}/history/atlanhq_atlan-dead-app.jsonl" in s3.objects


def test_a_single_repo_run_never_retires_anything(tmp_path):
    """Its one document says nothing about the rest of the fleet."""
    s3 = FakeS3(
        {
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-mysql-app.json": "{}",
            f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-metabase-app.json": "{}",
        }
    )
    publish(_scan_dir(tmp_path), PREFIX, single_repo=True, tmp_dir=tmp_path, run=s3)
    manifest = json.loads(s3.objects[f"{BUCKET}/{PREFIX}/repos.json"])
    assert "atlanhq_atlan-metabase-app.json" in manifest


def test_a_partial_scan_retires_nothing(tmp_path, capsys):
    """A crashed or rate-limited scan makes most of the fleet look departed.

    Dropping them would read as a catastrophic regression on every panel, so
    above the cap the manifest keeps everything and the run says why.
    """
    stored = {
        f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-app-{i}.json": "{}" for i in range(10)
    }
    stored[f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-mysql-app.json"] = "{}"
    s3 = FakeS3(stored)
    publish(_scan_dir(tmp_path), PREFIX, single_repo=False, tmp_dir=tmp_path, run=s3)
    manifest = json.loads(s3.objects[f"{BUCKET}/{PREFIX}/repos.json"])
    assert len(manifest) == 11
    assert "absent from this scan" in capsys.readouterr().err
