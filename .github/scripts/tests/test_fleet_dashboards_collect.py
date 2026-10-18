"""Tests for .github/scripts/fleet_dashboards_collect.py.

The collector replaces the per-repo ``update-dashboard.yml`` shim with one
central pull (FND-3337). What these guard:

* the test-readiness doc connector-pulse ingests is the scorecard verbatim;
* only live, default-branch artifacts are read, newest first, retry names
  included;
* a repo with nothing live is SKIPPED, never written as an empty "clean" row;
* timestamps come from the source run, so re-reading an unchanged artifact
  on the next tick produces the same history line;
* one repo's failure does not stop the fleet, and only an all-failed run
  turns red.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import fleet_dashboards_collect as fdc

REPO = "atlanhq/atlan-example-app"
SLUG = "atlanhq_atlan-example-app"
OTHER = "atlanhq/atlan-other-app"

SCORECARD = {
    "repo": REPO,
    "aggregate": {"score": 71, "grade": "B", "maturity": "m2", "cappedBy": []},
}


def _artifact(name: str, run_id: int, created: str, **kw: Any) -> dict[str, Any]:
    return {
        "name": name,
        "expired": kw.get("expired", False),
        "created_at": created,
        "workflow_run": {
            "id": run_id,
            "head_branch": kw.get("branch", "main"),
            "head_sha": kw.get("sha", "abc123"),
        },
    }


class FakeGh:
    """Answers the handful of gh calls the collector makes."""

    def __init__(self) -> None:
        self.default_branch = "main"
        self.artifacts: dict[str, list[dict[str, Any]]] = {}
        # run id -> {artifact name: {filename: content}}
        self.downloads: dict[int, dict[str, dict[str, str]]] = {}
        self.fail: set = set()
        self.calls: list[list[str]] = []

    def __call__(self, args: list) -> tuple:
        self.calls.append(list(args))
        key = " ".join(args)
        if any(f in key for f in self.fail):
            return 1, ""
        if args[:2] in (["api", f"repos/{REPO}"], ["api", f"repos/{OTHER}"]):
            return 0, self.default_branch + "\n"
        if args[0] == "api" and "/actions/artifacts?name=" in args[1]:
            name = args[1].split("name=")[1].split("&")[0]
            query = dict(p.split("=", 1) for p in args[1].split("?", 1)[1].split("&"))
            size, page = int(query["per_page"]), int(query.get("page", 1))
            listed = self.artifacts.get(name, [])[(page - 1) * size : page * size]
            return 0, json.dumps({"artifacts": listed})
        if args[:2] == ["run", "download"]:
            run_id = int(args[2])
            name = args[args.index("--name") + 1]
            dest = Path(args[args.index("--dir") + 1])
            files = self.downloads.get(run_id, {}).get(name)
            if files is None:
                return 1, ""
            dest.mkdir(parents=True, exist_ok=True)
            for fname, content in files.items():
                (dest / fname).write_text(content)
            return 0, ""
        raise AssertionError(f"unexpected gh call: {args}")


def _full_fleet_gh() -> FakeGh:
    gh = FakeGh()
    gh.artifacts["test-readiness-scorecard-retry"] = [
        _artifact("test-readiness-scorecard-retry", 30, "2026-10-05T01:00:00Z"),
        # Newer, but from a PR branch: must be ignored.
        _artifact(
            "test-readiness-scorecard-retry",
            31,
            "2026-10-06T01:00:00Z",
            branch="feat/x",
        ),
        # Newer, but expired: must be ignored.
        _artifact(
            "test-readiness-scorecard-retry",
            32,
            "2026-10-06T02:00:00Z",
            expired=True,
        ),
    ]
    gh.downloads[30] = {
        "test-readiness-scorecard-retry": {"test-readiness.json": json.dumps(SCORECARD)}
    }
    return gh


def _read(out: Path, prefix: str) -> tuple[dict[str, Any], dict[str, Any]]:
    doc = json.loads((out / prefix / "repos" / f"{SLUG}.json").read_text())
    history = json.loads((out / prefix / f"history_{SLUG}.jsonl").read_text())
    return doc, history


def _collect(gh: FakeGh, out: Path) -> dict[str, str]:
    return fdc.collect_repo(REPO, out, gh)


def test_collects_every_dashboard(tmp_path: Path) -> None:
    outcome = _collect(_full_fleet_gh(), tmp_path)
    assert outcome == {p: "published" for p in fdc.PREFIXES}


def test_stamps_come_from_the_source_run_not_the_clock(tmp_path: Path) -> None:
    """A re-read of an unchanged artifact must produce the same history line."""
    _collect(_full_fleet_gh(), tmp_path)
    _, tr_h = _read(tmp_path, fdc.TEST_READINESS_PREFIX)
    assert tr_h["date"] == "2026-10-05"

    histories = [tmp_path / prefix / f"history_{SLUG}.jsonl" for prefix in fdc.PREFIXES]
    first = [h.read_text() for h in histories]
    _collect(_full_fleet_gh(), tmp_path)
    assert [h.read_text() for h in histories] == first


def test_test_readiness_doc_is_the_scorecard_verbatim(tmp_path: Path) -> None:
    _collect(_full_fleet_gh(), tmp_path)
    doc, history = _read(tmp_path, fdc.TEST_READINESS_PREFIX)
    assert doc == SCORECARD
    assert history["score"] == 71 and history["grade"] == "B"


def test_quiet_repo_is_skipped_not_written_empty(tmp_path: Path) -> None:
    """No live artifacts must leave the stored row alone — zeros read as clean."""
    gh = FakeGh()
    outcome = _collect(gh, tmp_path)
    assert outcome == {p: "skipped" for p in fdc.PREFIXES}
    assert not list(tmp_path.rglob("*.json"))


def test_newest_live_artifact_across_plain_and_retry_names() -> None:
    gh = FakeGh()
    plain, retry = fdc.SCORECARD_ARTIFACTS
    gh.artifacts[plain] = [_artifact(plain, 1, "2026-10-01T00:00:00Z")]
    gh.artifacts[retry] = [_artifact(retry, 2, "2026-10-02T00:00:00Z")]
    got = fdc.latest_artifact(REPO, fdc.SCORECARD_ARTIFACTS, "main", gh)
    assert got is not None and got["name"] == retry


def test_non_main_default_branch_is_honoured(tmp_path: Path) -> None:
    gh = _full_fleet_gh()
    gh.default_branch = "master"
    # Every `main` fixture artifact is ignored; only the `master` one counts.
    assert _collect(gh, tmp_path)[fdc.TEST_READINESS_PREFIX] == "skipped"

    name = fdc.SCORECARD_ARTIFACTS[0]
    gh.artifacts[name] = [_artifact(name, 40, "2026-10-01T00:00:00Z", branch="master")]
    gh.downloads[40] = {name: {"test-readiness.json": json.dumps(SCORECARD)}}
    assert _collect(gh, tmp_path)[fdc.TEST_READINESS_PREFIX] == "published"
    assert any(c[:3] == ["run", "download", "40"] for c in gh.calls)


def test_one_repo_failing_does_not_cost_the_others(tmp_path: Path) -> None:
    gh = _full_fleet_gh()
    gh.fail.add(f"repos/{OTHER}")
    results = fdc.collect_fleet([REPO, OTHER], tmp_path, gh)
    assert results[REPO] == {p: "published" for p in fdc.PREFIXES}
    assert results[OTHER] == {p: "error" for p in fdc.PREFIXES}


def test_unreadable_scorecard_is_an_error_not_a_skip(tmp_path: Path) -> None:
    gh = _full_fleet_gh()
    gh.fail.add("test-readiness-scorecard")
    outcome = _collect(gh, tmp_path)
    assert outcome[fdc.TEST_READINESS_PREFIX] == "error"
    assert not (tmp_path / fdc.TEST_READINESS_PREFIX).exists()


def test_main_fails_only_when_every_repo_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    argv = ["--repos", json.dumps([REPO]), "--out-dir", str(tmp_path / "out")]

    broken = FakeGh()
    broken.fail.add(f"repos/{REPO}")
    assert fdc.main(argv, gh=broken) == 1

    # Nothing live is routine, not a failure.
    assert fdc.main(argv, gh=FakeGh()) == 0


def test_main_rejects_an_empty_roster(tmp_path: Path) -> None:
    argv = ["--repos", "[]", "--out-dir", str(tmp_path)]
    assert fdc.main(argv, gh=FakeGh()) == 1


def test_main_succeeds_when_only_some_repos_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run fails only when EVERY dashboard of every repo errored. One
    repo erroring beside a published one is a partial collection, not a token
    or API fault, and must not turn the job red."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    argv = ["--repos", json.dumps([REPO, OTHER]), "--out-dir", str(tmp_path / "out")]
    gh = _full_fleet_gh()
    gh.fail.add(f"repos/{OTHER}")
    assert fdc.main(argv, gh=gh) == 0
    assert (
        tmp_path / "out" / fdc.TEST_READINESS_PREFIX / "repos" / f"{SLUG}.json"
    ).exists()


def test_artifact_listing_is_paged_past_other_branches() -> None:
    """A full first page of PR-branch artifacts must not hide an older, still
    live default-branch one on the next page."""
    gh = FakeGh()
    name = "test-readiness-scorecard"
    gh.artifacts[name] = [
        _artifact(name, 100 + i, "2026-10-05T00:00:00Z", branch=f"pr/{i}")
        for i in range(fdc.ARTIFACT_PAGE_SIZE)
    ] + [_artifact(name, 7, "2026-10-01T00:00:00Z")]
    got = fdc.latest_artifact(REPO, (name,), "main", gh)
    assert got is not None and got["workflow_run"]["id"] == 7
    pages = [c[1] for c in gh.calls if f"name={name}&" in c[1]]
    assert len(pages) == 2 and pages[1].endswith("&page=2")


def test_artifact_paging_stops_at_the_first_page_with_a_match() -> None:
    """The listing is newest first, so a page holding a match ends the walk:
    later pages are only older, and each one costs REST budget."""
    gh = FakeGh()
    name = "test-readiness-scorecard"
    gh.artifacts[name] = [
        _artifact(name, i, "2026-10-01T00:00:00Z")
        for i in range(1, 2 * fdc.ARTIFACT_PAGE_SIZE + 1)
    ]
    fdc.latest_artifact(REPO, (name,), "main", gh)
    assert len([c for c in gh.calls if f"name={name}&" in c[1]]) == 1


def test_a_stalled_gh_call_fails_instead_of_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production default bounds every gh call, so one stalled request
    marks that repo failed and the sequential fleet scan moves on."""
    seen: dict[str, Any] = {}

    def stalled(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise fdc.subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(fdc.subprocess, "run", stalled)
    rc, out = fdc.run_gh_bounded(["api", f"repos/{REPO}"])
    assert (rc, out) == (fdc.GH_TIMEOUT_RC, "")
    assert seen["timeout"] == fdc.GH_CALL_TIMEOUT_SECONDS
    # And it is what the fleet scan and main() use when no gh is injected.
    assert fdc.collect_fleet.__defaults__ == (fdc.run_gh_bounded,)
    assert fdc.main.__defaults__ == (None, fdc.run_gh_bounded)


def test_the_workflow_runs_on_schedule_only() -> None:
    """The job holds fleet-wide credentials, so it runs on schedule only: a
    scheduled run always executes the default branch."""
    workflow = (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / ("update-fleet-dashboards.yaml")
    )
    doc = yaml.safe_load(workflow.read_text())
    # PyYAML reads the bare `on:` key as the boolean True.
    triggers = doc.get("on", doc.get(True))
    assert set(triggers) == {"schedule"}


def test_the_workflow_publishes_exactly_the_collected_prefixes() -> None:
    """Every collected dashboard has a publish step, and no step publishes a
    prefix the collector no longer writes (the security and conformance
    dashboards were dropped in FND-3462)."""
    workflow = (
        Path(__file__).resolve().parents[2]
        / "workflows"
        / ("update-fleet-dashboards.yaml")
    )
    steps = yaml.safe_load(workflow.read_text())["jobs"]["fleet-dashboards"]["steps"]
    published = {
        step["run"].split("--prefix ")[1].split()[0]
        for step in steps
        if "publish_fleet_dashboard.py" in step.get("run", "")
    }
    assert published == set(fdc.PREFIXES)
    for dropped in ("security-dashboard", "conformance-dashboard"):
        assert not any(dropped in step.get("run", "") for step in steps)


def test_a_stalled_repo_does_not_stop_the_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stalled(cmd: list[str], **kwargs: Any) -> Any:
        raise fdc.subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(fdc.subprocess, "run", stalled)
    results = fdc.collect_fleet([REPO, OTHER], tmp_path)
    assert set(results) == {REPO, OTHER}
    assert all(s == "error" for o in results.values() for s in o.values())
