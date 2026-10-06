"""Tests for .github/scripts/fleet_dashboards_collect.py.

The collector replaces the per-repo ``update-dashboard.yml`` shim with one
central pull (FND-3337). What these guard:

* the doc shapes connector-pulse ingests are unchanged from the reusable's
  inline Python;
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

sys.path.insert(0, str(Path(__file__).parent.parent))

import fleet_dashboards_collect as fdc

REPO = "atlanhq/atlan-example-app"
SLUG = "atlanhq_atlan-example-app"

TRIVY = {
    "Results": [
        {
            "Type": "uv",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-1",
                    "Severity": "CRITICAL",
                    "PkgName": "requests",
                    "InstalledVersion": "1.0",
                    "FixedVersion": "1.1",
                },
                # Duplicate id: must be counted once.
                {"VulnerabilityID": "CVE-1", "Severity": "CRITICAL"},
            ],
        },
        {
            "Type": "debian",
            "Vulnerabilities": [
                {"VulnerabilityID": "CVE-2", "Severity": "HIGH", "PkgName": "libc"},
                {"VulnerabilityID": "CVE-3", "Severity": "LOW", "PkgName": "zlib"},
            ],
        },
    ]
}

SARIF = {
    "runs": [
        {
            "tool": {
                "driver": {
                    "version": "1.2.3",
                    "rules": [
                        {
                            "id": "L001",
                            "name": "pct-logging",
                            "properties": {"atlan/tier": "warn"},
                            "shortDescription": {"text": "short"},
                        }
                    ],
                }
            },
            "properties": {
                "atlan/summary": {"failing": 1, "warning": 2, "suppressing": 1},
                "atlan/excludedPaths": ["tests/"],
                "atlan/profileVersion": "v2",
            },
            "results": [
                {"ruleId": "L001"},
                {
                    "ruleId": "L001",
                    "suppressions": [{"justification": " legacy "}],
                },
                {"ruleId": "L001", "kind": "pass"},
            ],
        }
    ]
}

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
    """Answers the handful of gh calls the collector and fcs make."""

    def __init__(self) -> None:
        self.default_branch = "main"
        self.artifacts: dict[str, list[dict[str, Any]]] = {}
        self.files: dict[str, str] = {}
        # run id -> {artifact name: {filename: content}}
        self.downloads: dict[int, dict[str, dict[str, str]]] = {}
        self.conformance_runs: list[dict[str, Any]] = []
        self.run_artifacts: dict[int, list[dict[str, Any]]] = {}
        self.run_views: dict[int, dict[str, str]] = {}
        self.fail: set = set()
        self.calls: list[list[str]] = []

    def __call__(self, args: list) -> tuple:
        self.calls.append(list(args))
        key = " ".join(args)
        if any(f in key for f in self.fail):
            return 1, ""
        if args[:2] == ["api", f"repos/{REPO}"]:
            return 0, self.default_branch + "\n"
        if args[0] == "api" and "/actions/artifacts?name=" in args[1]:
            name = args[1].split("name=")[1].split("&")[0]
            return 0, json.dumps({"artifacts": self.artifacts.get(name, [])})
        if args[0] == "api" and "/actions/runs/" in args[1]:
            run_id = int(args[1].split("/runs/")[1].split("/")[0])
            return 0, json.dumps({"artifacts": self.run_artifacts.get(run_id, [])})
        if args[0] == "api" and args[1] == "-H":
            path = args[3].split("/contents/")[1].split("?")[0]
            if path in self.files:
                return 0, self.files[path]
            return 1, ""
        if args[:2] == ["run", "list"]:
            return 0, json.dumps(self.conformance_runs)
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
        if args[:2] == ["run", "view"]:
            return 0, json.dumps(self.run_views[int(args[2])])
        raise AssertionError(f"unexpected gh call: {args}")


def _full_fleet_gh() -> FakeGh:
    gh = FakeGh()
    gh.artifacts["trivy-results"] = [
        _artifact("trivy-results", 10, "2026-10-01T03:00:00Z"),
        # Newer, but from a PR branch: must be ignored.
        _artifact("trivy-results", 11, "2026-10-02T03:00:00Z", branch="feat/x"),
        # Newer, but expired: must be ignored.
        _artifact("trivy-results", 12, "2026-10-03T03:00:00Z", expired=True),
    ]
    gh.downloads[10] = {"trivy-results": {"trivy_results.json": json.dumps(TRIVY)}}
    gh.files["Dockerfile"] = (
        "FROM node:20-alpine AS ui\n"
        "FROM registry.atlan.com/public/app-runtime-base:2.4.0\n"
    )
    gh.files[".security/allowlist.json"] = json.dumps(
        {"_comment": "x", "CVE-2": {"expires": "2099-01-01"}}
    )

    gh.conformance_runs = [{"databaseId": 20, "conclusion": "failure"}]
    gh.run_artifacts[20] = [{"name": "conformance-logging-sarif", "expired": False}]
    gh.downloads[20] = {
        "conformance-logging-sarif": {"logging.sarif": json.dumps(SARIF)}
    }
    gh.run_views[20] = {
        "headSha": "def456",
        "headBranch": "main",
        "createdAt": "2026-10-04T05:06:07Z",
    }

    gh.artifacts["test-readiness-scorecard-retry"] = [
        _artifact("test-readiness-scorecard-retry", 30, "2026-10-05T01:00:00Z")
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
    base = {"CVE-3": {"expires": "2020-01-01"}}
    return fdc.collect_repo(REPO, base, "2026-10-06", out, gh)


def test_collects_all_three_dashboards(tmp_path: Path) -> None:
    outcome = _collect(_full_fleet_gh(), tmp_path)
    assert outcome == {p: "published" for p in fdc.PREFIXES}


def test_security_doc_matches_the_reusable_shape(tmp_path: Path) -> None:
    _collect(_full_fleet_gh(), tmp_path)
    doc, history = _read(tmp_path, fdc.SECURITY_PREFIX)

    assert doc["repo"] == REPO
    # SDK base FROM wins over the builder stage that comes first.
    assert doc["sdk_version"] == "2.4.0"
    assert doc["total"] == 3  # CVE-1 deduplicated
    assert doc["critical"] == 1 and doc["high"] == 1
    by_id = {v["id"]: v for v in doc["vulnerabilities"]}
    assert by_id["CVE-1"]["source_type"] == "app"
    assert by_id["CVE-1"]["status"] == "new"
    assert by_id["CVE-2"]["status"] == "allowlisted"
    # In the base allowlist: base_image regardless of type, and expired.
    assert by_id["CVE-3"]["source_type"] == "base_image"
    assert by_id["CVE-3"]["status"] == "expired"
    assert doc["app_count"] == 1 and doc["base_image_count"] == 2
    assert set(doc) == {
        "repo",
        "sdk_version",
        "vulnerabilities",
        "scanned_at",
        "total",
        "critical",
        "high",
        "base_image_count",
        "app_count",
        "allowlisted",
        "new",
    }
    assert history["ids"] == ["CVE-1", "CVE-2", "CVE-3"]
    assert history["ids_allowlisted"] == ["CVE-2"]


def test_security_reads_the_scanned_commit_not_head(tmp_path: Path) -> None:
    gh = _full_fleet_gh()
    _collect(gh, tmp_path)
    reads = [c[3] for c in gh.calls if c[:2] == ["api", "-H"]]
    assert reads and all(r.endswith("?ref=abc123") for r in reads)


def test_stamps_come_from_the_source_run_not_the_clock(tmp_path: Path) -> None:
    """A re-read of an unchanged artifact must produce the same history line."""
    _collect(_full_fleet_gh(), tmp_path)
    sec, sec_h = _read(tmp_path, fdc.SECURITY_PREFIX)
    conf, conf_h = _read(tmp_path, fdc.CONFORMANCE_PREFIX)
    _, tr_h = _read(tmp_path, fdc.TEST_READINESS_PREFIX)

    assert sec["scanned_at"] == "2026-10-01T03:00:00Z"
    assert sec_h["date"] == "2026-10-01"
    assert conf["collectedAt"] == "2026-10-04T05:06:07Z"
    assert conf_h["date"] == "2026-10-04"
    assert tr_h["date"] == "2026-10-05"

    first = (tmp_path / fdc.SECURITY_PREFIX / f"history_{SLUG}.jsonl").read_text()
    _collect(_full_fleet_gh(), tmp_path)
    again = (tmp_path / fdc.SECURITY_PREFIX / f"history_{SLUG}.jsonl").read_text()
    assert first == again


def test_conformance_doc_matches_the_reusable_shape(tmp_path: Path) -> None:
    _collect(_full_fleet_gh(), tmp_path)
    doc, history = _read(tmp_path, fdc.CONFORMANCE_PREFIX)

    assert doc["commit"] == "def456" and doc["branch"] == "main"
    assert doc["toolVersion"] == "1.2.3"
    assert doc["profileVersion"] == "v2"
    assert doc["summary"] == {"failing": 1, "warning": 2, "suppressing": 1}
    assert doc["byRule"] == {"L001": {"failing": 1, "suppressing": 1}}
    assert doc["suppressions"] == [{"ruleId": "L001", "justification": "legacy"}]
    assert doc["excludedPaths"] == ["tests/"]
    assert doc["ruleCatalog"]["L001"]["tier"] == "warn"
    assert history == {
        "date": "2026-10-04",
        "repo": REPO,
        "toolVersion": "1.2.3",
        "failing": 1,
        "warning": 2,
        "suppressing": 1,
    }


def test_conformance_tool_version_skew_is_surfaced() -> None:
    a = {"runs": [{"tool": {"driver": {"version": "1.0"}}}]}
    b = {"runs": [{"tool": {"driver": {"version": "2.0"}}}]}
    doc, _ = fdc.conformance_doc(REPO, [a, b], "s", "main", "2026-10-01T00:00:00Z")
    assert doc["toolVersion"] == "skew:1.0,2.0"


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
    gh.artifacts["trivy-results"] = [
        _artifact("trivy-results", 1, "2026-10-01T00:00:00Z")
    ]
    gh.artifacts["trivy-results-retry"] = [
        _artifact("trivy-results-retry", 2, "2026-10-02T00:00:00Z")
    ]
    got = fdc.latest_artifact(REPO, fdc.TRIVY_ARTIFACTS, "main", gh)
    assert got is not None and got["name"] == "trivy-results-retry"


def test_non_main_default_branch_is_honoured(tmp_path: Path) -> None:
    gh = _full_fleet_gh()
    gh.default_branch = "master"
    outcome = _collect(gh, tmp_path)
    # Every fixture artifact is on `main`, so nothing matches `master`.
    assert outcome[fdc.SECURITY_PREFIX] == "skipped"
    assert outcome[fdc.TEST_READINESS_PREFIX] == "skipped"
    assert any("--branch=master" in c for c in gh.calls if c[:2] == ["run", "list"])


def test_one_dashboard_failing_does_not_cost_the_others(tmp_path: Path) -> None:
    gh = _full_fleet_gh()
    gh.fail.add("test-readiness-scorecard")
    outcome = _collect(gh, tmp_path)
    assert outcome[fdc.TEST_READINESS_PREFIX] == "error"
    assert outcome[fdc.SECURITY_PREFIX] == "published"
    assert outcome[fdc.CONFORMANCE_PREFIX] == "published"


def test_conformance_discovery_error_is_an_error_not_a_skip(tmp_path: Path) -> None:
    gh = _full_fleet_gh()
    gh.fail.add("run list")
    outcome = _collect(gh, tmp_path)
    assert outcome[fdc.CONFORMANCE_PREFIX] == "error"


def test_sdk_version_falls_back_to_last_from() -> None:
    text = "FROM python:3.12 AS build\nFROM debian:bookworm-slim\n"
    assert fdc.sdk_version([text]) == "bookworm-slim"
    assert fdc.sdk_version(["FROM scratch\n"]) == "unknown"
    assert fdc.sdk_version([None]) == "unknown"


def test_main_fails_only_when_every_repo_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    base = tmp_path / "base.json"
    base.write_text("{}")
    argv = [
        "--repos",
        json.dumps([REPO]),
        "--out-dir",
        str(tmp_path / "out"),
        "--base-allowlist",
        str(base),
    ]

    broken = FakeGh()
    broken.fail.add(f"repos/{REPO}")
    assert fdc.main(argv, gh=broken) == 1

    # Nothing live is routine, not a failure.
    assert fdc.main(argv, gh=FakeGh()) == 0


def test_main_rejects_an_empty_roster(tmp_path: Path) -> None:
    argv = [
        "--repos",
        "[]",
        "--out-dir",
        str(tmp_path),
        "--base-allowlist",
        str(tmp_path / "missing.json"),
    ]
    assert fdc.main(argv, gh=FakeGh()) == 1
