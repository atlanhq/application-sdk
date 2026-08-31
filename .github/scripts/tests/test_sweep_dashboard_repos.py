"""Tests for .github/scripts/sweep_dashboard_repos.py.

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

import sweep_dashboard_repos as sweep_module  # noqa: E402
from sweep_dashboard_repos import (  # noqa: E402
    BUCKET,
    archived_state,
    list_slugs,
    load_roster,
    main,
    slug_to_repo,
    sweep,
)

PREFIX = "conformance-dashboard"


class FakeS3:
    def __init__(self, initial=None, rm_denies=()):
        self.objects = dict(initial or {})
        self.removed: list = []
        # Substrings of keys the bucket refuses to delete, mirroring an
        # AccessDenied / throttled `aws s3 rm` (non-zero, object survives).
        self.rm_denies = tuple(rm_denies)

    def __call__(self, args: list) -> tuple:
        if args[:2] == ["s3", "ls"]:
            listing = "".join(
                f"2026-08-27 12:00:00       1234 {key.rsplit('/', 1)[-1]}\n"
                for key in sorted(self.objects)
                if key.startswith(args[2])
            )
            return 0, listing, ""
        if args[:2] == ["s3", "rm"]:
            if any(d in args[2] for d in self.rm_denies):
                return 1, "", "An error occurred (AccessDenied)"
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


def test_a_refused_delete_keeps_the_slug_in_the_manifest(tmp_path):
    """The object survives, so its row must too — otherwise the panel drops a
    repo whose data is still in the bucket and no later run reconsiders it."""
    s3 = FakeS3(
        _bucket("atlanhq_atlan-live-app", "atlanhq_atlan-dead-app"),
        rm_denies=("atlanhq_atlan-dead-app",),
    )
    result = sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(archived=("atlanhq/atlan-dead-app",)),
    )
    assert result["swept"] == []
    assert result["refused"] == ["atlanhq_atlan-dead-app"]
    assert f"{BUCKET}/{PREFIX}/repos.json" not in s3.objects


def test_a_partial_delete_failure_still_sweeps_the_rest(tmp_path):
    """One refused prefix row must not strand the deletes that did land."""
    s3 = FakeS3(
        _bucket("atlanhq_atlan-live-app", "atlanhq_a-dead-app", "atlanhq_b-dead-app"),
        rm_denies=("atlanhq_b-dead-app",),
    )
    result = sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(archived=("atlanhq/a-dead-app", "atlanhq/b-dead-app")),
        max_fraction=1.0,
    )
    assert result["swept"] == ["atlanhq_a-dead-app"]
    assert result["refused"] == ["atlanhq_b-dead-app"]
    assert json.loads(s3.objects[f"{BUCKET}/{PREFIX}/repos.json"]) == [
        "atlanhq_atlan-live-app.json",
        "atlanhq_b-dead-app.json",
    ]


# ── roster mode ──────────────────────────────────────────────────────────────


def test_a_repo_off_the_roster_is_swept_even_though_it_is_alive(tmp_path):
    """The criterion the archived sweep cannot express.

    A repo like `AI-taskforce` is healthy and was never a consumer; the only
    thing wrong with it is that a publisher wrote it to a panel whose scope it
    was never in. Archived-ness has nothing to say about that.
    """
    s3 = FakeS3(_bucket("atlanhq_atlan-mysql-app", "atlanhq_AI-taskforce"))
    result = sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(),  # never consulted
        roster={"atlanhq/atlan-mysql-app"},
    )
    assert result["swept"] == ["atlanhq_AI-taskforce"]
    assert json.loads(s3.objects[f"{BUCKET}/{PREFIX}/repos.json"]) == [
        "atlanhq_atlan-mysql-app.json"
    ]


def test_roster_mode_never_asks_github(tmp_path):
    """Roster membership is the whole question, so a GitHub call is a bug: it
    would make the sweep's verdict depend on a token roster mode does not need."""

    def exploding_gh(repo: str) -> tuple:
        raise AssertionError(f"roster mode must not query GitHub (asked: {repo})")

    s3 = FakeS3(_bucket("atlanhq_atlan-mysql-app"))
    sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=exploding_gh,
        roster={"atlanhq/atlan-mysql-app"},
    )
    assert s3.removed == []


def test_an_unparsable_slug_is_reported_not_swept_in_roster_mode(tmp_path):
    """A slug that is not `<owner>_<name>` cannot be compared to the roster, so
    it is undetermined — not 'absent from the roster'."""
    s3 = FakeS3(_bucket("atlanhq_atlan-mysql-app", "stray-object"))
    result = sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(),
        roster={"atlanhq/atlan-mysql-app"},
    )
    assert result["unknown"] == ["stray-object"]
    assert s3.removed == []


def test_a_truncated_roster_trips_the_fraction_cap(tmp_path, capsys):
    """A roster that lost most of the fleet looks exactly like a mass prune.

    Same rail as a bad token in archived mode: the cleanup has to be asked for
    at the size it actually is, rather than arriving as a surprise.
    """
    names = [f"atlanhq_atlan-app-{i}" for i in range(5)]
    s3 = FakeS3(_bucket(*names))
    result = sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(),
        roster={"atlanhq/atlan-app-0"},
    )
    assert s3.removed == []
    assert len(result["refused"]) == 4
    err = capsys.readouterr().err
    assert "not on the roster" in err
    assert "sweeping none" in err


def test_a_large_cleanup_goes_through_once_the_cap_is_raised(tmp_path):
    """The renovate-dashboard shape: most of what is stored does not belong."""
    names = [f"atlanhq_atlan-app-{i}" for i in range(5)]
    s3 = FakeS3(_bucket(*names))
    result = sweep(
        PREFIX,
        tmp_path,
        run=s3,
        gh=_gh(),
        roster={"atlanhq/atlan-app-0"},
        max_fraction=0.8,
    )
    assert len(result["swept"]) == 4
    assert json.loads(s3.objects[f"{BUCKET}/{PREFIX}/repos.json"]) == [
        "atlanhq_atlan-app-0.json"
    ]


def test_an_empty_roster_is_refused_rather_than_read_as_delete_everything(tmp_path):
    """The roster is the deletion criterion, so an empty one is not a criterion.

    Discovery returning [] (a 401 on `gh repo list` looks identical to an empty
    fleet) would otherwise name every stored repo for deletion.
    """
    roster_file = tmp_path / "roster.json"
    roster_file.write_text("[]")
    assert load_roster(roster_file) is None


def test_an_unreadable_or_wrong_shaped_roster_is_refused(tmp_path):
    assert load_roster(tmp_path / "nope.json") is None

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json")
    assert load_roster(malformed) is None

    wrong_shape = tmp_path / "shape.json"
    wrong_shape.write_text('{"repos": ["atlanhq/atlan-mysql-app"]}')
    assert load_roster(wrong_shape) is None


def test_a_good_roster_loads_as_a_set(tmp_path):
    roster_file = tmp_path / "roster.json"
    roster_file.write_text('["atlanhq/atlan-mysql-app", " atlanhq/application-sdk "]')
    assert load_roster(roster_file) == {
        "atlanhq/atlan-mysql-app",
        "atlanhq/application-sdk",
    }


def test_a_roster_may_not_be_fanned_across_prefixes(tmp_path, capsys):
    """The panels do not share a publish scope.

    gate-enforcement-dashboard spans every repo its probe visits (~166) against
    renovate's ~80, so the Renovate roster would name about half of a healthy
    panel for deletion — a fraction well under any cap an operator would raise
    for a genuine cleanup. The fan-out itself has to be refused, before any
    listing happens.
    """
    roster_file = tmp_path / "roster.json"
    roster_file.write_text('["atlanhq/atlan-mysql-app"]')

    rc = main(
        [
            "--prefixes",
            "renovate-dashboard,gate-enforcement-dashboard",
            "--roster-file",
            str(roster_file),
        ]
    )
    assert rc == 2
    assert "exactly one --prefixes entry" in capsys.readouterr().err


def test_the_archived_sweep_still_fans_out_across_prefixes(monkeypatch):
    """Archived-ness is a property of the repo, not of a panel's scope, so the
    multi-prefix default stays valid for that criterion."""
    seen: list = []

    def fake_sweep(prefix, tmp_dir, **kwargs):
        seen.append(prefix)
        assert kwargs["roster"] is None
        return {
            "prefix": prefix,
            "stored": 0,
            "swept": [],
            "refused": [],
            "unknown": [],
        }

    monkeypatch.setattr(sweep_module, "sweep", fake_sweep)
    assert main(["--prefixes", "security-dashboard,renovate-dashboard"]) == 0
    assert seen == ["security-dashboard", "renovate-dashboard"]
