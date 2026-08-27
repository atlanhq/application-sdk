"""Tests for .github/scripts/sweep_archived_dashboard_repos.py.

The sweep DELETES dashboard objects, so the cases here are weighted towards what
it must refuse: an unreadable repo state, an unexpected object in the prefix, and
a token failure that makes the whole fleet look archived. Deleting a live repo's
history is unrecoverable; leaving a dead row on a panel is cosmetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sweep_archived_dashboard_repos import (  # noqa: E402
    BUCKET,
    archived_state,
    list_slugs,
    slug_to_repo,
    sweep,
)

PREFIX = "conformance-dashboard"


class FakeS3:
    def __init__(self, initial=None):
        self.objects = dict(initial or {})
        self.removed: list = []

    def __call__(self, args: list) -> tuple:
        if args[:2] == ["s3", "ls"]:
            listing = "".join(
                f"2026-08-27 12:00:00       1234 {key.rsplit('/', 1)[-1]}\n"
                for key in sorted(self.objects)
                if key.startswith(args[2])
            )
            return 0, listing, ""
        if args[:2] == ["s3", "rm"]:
            self.removed.append(args[2])
            self.objects.pop(args[2], None)
            return 0, "", ""
        if args[:2] == ["s3", "cp"]:
            self.objects[args[3]] = Path(args[2]).read_text()
            return 0, "", ""
        raise AssertionError(f"unexpected aws call: {args}")


def _gh(archived=(), fails=()):
    def gh(repo: str) -> tuple:
        if repo in fails:
            return 1, "", "gh: Not Found (HTTP 404)"
        return 0, "true\n" if repo in archived else "false\n", ""

    return gh


def _bucket(*names):
    objects = {}
    for n in names:
        objects[f"{BUCKET}/{PREFIX}/repos/{n}.json"] = "{}"
        objects[f"{BUCKET}/{PREFIX}/history/{n}.jsonl"] = "{}\n"
    return objects


# ── slug handling ────────────────────────────────────────────────────────────


def test_slug_maps_to_owner_and_repo():
    assert slug_to_repo("atlanhq_atlan-mysql-app") == "atlanhq/atlan-mysql-app"


def test_a_slug_without_an_owner_is_not_a_repo():
    """An unexpected object must not become a repo name we query and then delete."""
    assert slug_to_repo("stray-object") is None


def test_listing_ignores_non_json_objects(tmp_path):
    s3 = FakeS3({f"{BUCKET}/{PREFIX}/repos/README.txt": "x", **_bucket("atlanhq_a")})
    assert list_slugs(PREFIX, run=s3) == ["atlanhq_a"]


# ── archived state ───────────────────────────────────────────────────────────


def test_state_is_unknown_when_github_cannot_answer():
    """A 404/403 says something about the token, not about fleet membership."""
    assert archived_state("atlanhq/x", _gh(fails=("atlanhq/x",))) is None


def test_state_reads_the_flag():
    assert archived_state("atlanhq/x", _gh(archived=("atlanhq/x",))) is True
    assert archived_state("atlanhq/x", _gh()) is False


# ── sweeping ─────────────────────────────────────────────────────────────────


def test_an_archived_repo_loses_its_document_history_and_manifest_row(tmp_path):
    s3 = FakeS3(_bucket("atlanhq_atlan-live-app", "atlanhq_atlan-dead-app"))
    result = sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(archived=("atlanhq/atlan-dead-app",)),
    )
    assert result["swept"] == ["atlanhq_atlan-dead-app"]
    assert f"{BUCKET}/{PREFIX}/repos/atlanhq_atlan-dead-app.json" in s3.removed
    assert f"{BUCKET}/{PREFIX}/history/atlanhq_atlan-dead-app.jsonl" in s3.removed
    assert json.loads(s3.objects[f"{BUCKET}/{PREFIX}/repos.json"]) == [
        "atlanhq_atlan-live-app.json"
    ]


def test_a_live_repo_is_untouched(tmp_path):
    s3 = FakeS3(_bucket("atlanhq_atlan-live-app"))
    sweep(PREFIX, tmp_path, run=s3, gh=_gh())
    assert s3.removed == []


def test_an_unreadable_repo_is_reported_not_swept(tmp_path):
    """Deleted / renamed / invisible need different follow-ups than archived."""
    s3 = FakeS3(_bucket("atlanhq_atlan-live-app", "atlanhq_atlan-mystery-app"))
    result = sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(fails=("atlanhq/atlan-mystery-app",)),
    )
    assert result["unknown"] == ["atlanhq_atlan-mystery-app"]
    assert s3.removed == []


def test_a_dry_run_deletes_nothing_and_rewrites_no_manifest(tmp_path):
    s3 = FakeS3(_bucket("atlanhq_atlan-live-app", "atlanhq_atlan-dead-app"))
    sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(archived=("atlanhq/atlan-dead-app",)),
        dry_run=True,
    )
    assert s3.removed == []
    assert f"{BUCKET}/{PREFIX}/repos.json" not in s3.objects


def test_everything_reading_as_archived_sweeps_nothing(tmp_path, capsys):
    """A broken token must not be able to empty a dashboard."""
    names = [f"atlanhq_atlan-app-{i}" for i in range(5)]
    s3 = FakeS3(_bucket(*names))
    result = sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(archived=tuple(f"atlanhq/atlan-app-{i}" for i in range(5))),
    )
    assert s3.removed == []
    assert len(result["refused"]) == 5
    assert "sweeping none" in capsys.readouterr().err
