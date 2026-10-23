"""Tests for .github/scripts/renovate_release_followup.py.

`gh` is stubbed through the module's RunFn seam with a fake org: repo listing,
renovate.json per repo, rules on main, and uv.lock per (repo, ref). The poller
is injected, so no case sleeps or touches the network. The scenarios are the
2026-10-22 rollout's: a repo whose framework PR carried only the SDK, and a
repo that got no PR at all.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import renovate_release_followup as mod

SDK = "atlan-application-sdk"
API = "atlan-application-sdk-api"
CONF = "atlan-application-sdk-conformance"
RELEASED = {SDK: "3.51.2", API: "3.51.2", CONF: "0.52.0"}

AUTO_CONFIG = json.dumps(
    {"extends": ["github>atlanhq/application-sdk//renovate-config/default.json"]}
)
SOFT_CONFIG = json.dumps(
    {
        "extends": ["github>atlanhq/application-sdk//renovate-config/default.json"],
        "packageRules": [{"matchPackageNames": ["*"], "automerge": False}],
    }
)

OLD = {SDK: "3.51.1", API: "3.51.1", CONF: "0.51.1"}
NEW = dict(RELEASED)
SDK_ONLY = {SDK: "3.51.2", API: "3.51.2", CONF: "0.51.1"}


def _lock(versions: dict[str, str]) -> str:
    return "version = 1\n" + "".join(
        f'\n[[package]]\nname = "{n}"\nversion = "{v}"\n' for n, v in versions.items()
    )


class FakeGh:
    def __init__(self, repos: dict[str, dict]):
        """repos: name -> {config, rules, locks: {ref: versions | None}}."""
        self.repos = repos
        self.dispatches: list[list[str] | None] = []
        self.dispatch_fails = False

    def __call__(self, args: list) -> tuple:
        if args[:2] == ["repo", "list"]:
            return 0, json.dumps([f"atlanhq/{r}" for r in self.repos]), ""
        if args[:3] == ["api", "--method", "POST"]:
            if self.dispatch_fails:
                return 1, "", "HTTP 403"
            fields = [a for a in args if a.startswith("inputs[repos]=")]
            self.dispatches.append(
                json.loads(fields[0].split("=", 1)[1]) if fields else None
            )
            return 0, json.dumps({"workflow_run_id": 100 + len(self.dispatches)}), ""
        path = args[-1]
        repo = path.split("/")[2]
        spec = self.repos[repo]
        if path.endswith("/contents/renovate.json"):
            return 0, spec.get("config", AUTO_CONFIG), ""
        if path.endswith("/rules/branches/main"):
            return 0, json.dumps(spec.get("rules", [{"type": "merge_queue"}])), ""
        if "/contents/uv.lock?ref=" in path:
            ref = path.split("?ref=")[1]
            versions = spec["locks"].get(ref)
            if versions is None:
                return 1, "", "gh: Not Found (HTTP 404)"
            if versions == "error":
                return 1, "", "HTTP 502"
            return 0, _lock(versions), ""
        raise AssertionError(f"unexpected gh call: {args}")


def _poll(conclusions: list[str]):
    seen: list[str] = []

    def poll(repo, run_id, timeout_seconds):
        seen.append(run_id)
        c = conclusions.pop(0) if conclusions else "success"
        return ("timeout" if c == "timeout" else "completed"), c

    return poll, seen


def _followup(gh, conclusions=None, **kwargs):
    poll, seen = _poll(list(conclusions or []))
    left = mod.followup(RELEASED, run=gh, poll=poll, sleep=lambda s: None, **kwargs)
    return left, seen


def test_all_current_dispatches_once():
    gh = FakeGh(
        {
            "atlan-a-app": {"locks": {"main": NEW}},
            # Behind on main, but its open framework PR has the release.
            "atlan-b-app": {"locks": {"main": OLD, mod.FRAMEWORK_BRANCH: NEW}},
        }
    )
    left, seen = _followup(gh)
    assert left == []
    assert gh.dispatches == [None]
    assert seen == ["101"]


def test_stragglers_get_one_scoped_rerun():
    gh = FakeGh(
        {
            "atlan-ok-app": {"locks": {"main": NEW}},
            # Framework PR merged carrying only the SDK.
            "atlan-sdkonly-app": {"locks": {"main": SDK_ONLY}},
            # No PR opened at all.
            "atlan-nopr-app": {"locks": {"main": OLD}},
        }
    )
    left, seen = _followup(gh)
    # Still behind in the fake after the re-run, so retries run out at 2.
    assert gh.dispatches == [
        None,
        ["atlanhq/atlan-nopr-app", "atlanhq/atlan-sdkonly-app"],
        ["atlanhq/atlan-nopr-app", "atlanhq/atlan-sdkonly-app"],
    ]
    assert seen == ["101", "102", "103"]
    assert left == ["atlanhq/atlan-nopr-app", "atlanhq/atlan-sdkonly-app"]


def test_stops_when_scoped_rerun_catches_up():
    repos = {"atlan-nopr-app": {"locks": {"main": OLD}}}
    gh = FakeGh(repos)

    def poll(repo, run_id, timeout_seconds):
        if run_id == "102":
            repos["atlan-nopr-app"]["locks"][mod.FRAMEWORK_BRANCH] = NEW
        return "completed", "success"

    left = mod.followup(RELEASED, run=gh, poll=poll, sleep=lambda s: None)
    assert left == []
    assert gh.dispatches == [None, ["atlanhq/atlan-nopr-app"]]


def test_soft_mode_and_unprotected_repos_are_not_monitored():
    gh = FakeGh(
        {
            "atlan-soft-app": {"config": SOFT_CONFIG, "locks": {"main": OLD}},
            "atlan-norules-app": {"rules": [], "locks": {"main": OLD}},
        }
    )
    left, _ = _followup(gh)
    assert left == []
    assert gh.dispatches == [None]


def test_cancelled_sweep_skips_verification():
    gh = FakeGh({"atlan-nopr-app": {"locks": {"main": OLD}}})
    left, _ = _followup(gh, conclusions=["cancelled"])
    assert left == []
    assert gh.dispatches == [None]


def test_failed_sweep_is_still_verified():
    # One repo's matrix job failing makes the run conclude `failure`; the rest
    # of the fleet still needs checking.
    gh = FakeGh({"atlan-nopr-app": {"locks": {"main": OLD}}})
    _followup(gh, conclusions=["failure"], max_redispatches=1)
    assert gh.dispatches == [None, ["atlanhq/atlan-nopr-app"]]


def test_poll_timeout_skips_verification():
    gh = FakeGh({"atlan-nopr-app": {"locks": {"main": OLD}}})
    left, _ = _followup(gh, conclusions=["timeout"])
    assert left == []
    assert gh.dispatches == [None]


def test_dispatch_failure_warns_with_the_existing_fallback(capsys):
    gh = FakeGh({})
    gh.dispatch_fails = True
    left, seen = _followup(gh)
    assert left == [] and seen == []
    assert mod.DISPATCH_FALLBACK in capsys.readouterr().out


def test_unreadable_lock_is_redispatched():
    gh = FakeGh({"atlan-flaky-app": {"locks": {"main": "error"}}})
    _followup(gh, max_redispatches=1)
    assert gh.dispatches == [None, ["atlanhq/atlan-flaky-app"]]


def test_repo_without_uv_lock_is_skipped():
    gh = FakeGh({"atlan-nolock-app": {"locks": {}}})
    left, _ = _followup(gh)
    assert left == []


def test_prerelease_only_release_skips_verification():
    gh = FakeGh({"atlan-nopr-app": {"locks": {"main": OLD}}})
    poll, seen = _poll([])
    left = mod.followup(
        {SDK: "3.52.0rc1", API: "3.52.0rc1"}, run=gh, poll=poll, sleep=lambda s: None
    )
    assert left == [] and seen == []
    assert gh.dispatches == [None]


@pytest.mark.parametrize(
    ("released", "main_lock", "expected"),
    [
        # A package the repo does not lock is not expected.
        ({CONF: "0.52.0"}, {SDK: "3.51.1"}, {}),
        # SDK majors are opt-in: a repo on 3.x is not behind a 4.0.0 release.
        ({SDK: "4.0.0", API: "4.0.0"}, {SDK: "3.51.1", API: "3.51.1"}, {}),
        # Conformance majors auto-merge, so they are expected.
        ({CONF: "1.0.0"}, {CONF: "0.52.0"}, {CONF: "1.0.0"}),
        # Pre-releases are never expected.
        ({SDK: "3.52.0rc1"}, {SDK: "3.51.2"}, {}),
    ],
)
def test_expected_versions(released, main_lock, expected):
    assert mod.expected_versions(released, main_lock) == expected


def test_main_with_bad_packages_still_dispatches():
    gh = FakeGh({})
    assert mod.main(["--packages", "not json"], run=gh) == 0
    assert gh.dispatches == [None]


@pytest.mark.parametrize("status", ["", "in_progress_unknown", "unreadable"])
def test_unfinished_status_skips_verification(status):
    gh = FakeGh({"atlan-nopr-app": {"locks": {"main": OLD}}})
    left = mod.followup(
        RELEASED,
        run=gh,
        poll=lambda repo, run_id, timeout_seconds: (status, ""),
        sleep=lambda s: None,
    )
    assert left == []
    assert gh.dispatches == [None]


def test_gh_timeout_is_an_ordinary_failure(monkeypatch, tmp_path):
    # A real hanging `gh` on PATH rather than a patched subprocess.run, which
    # would be process-wide.
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(mod, "GH_TIMEOUT_SECONDS", 0.2)
    code, out, err = mod._run_gh(["api", "x"])
    assert code == 1 and out == "" and "timed out" in err
