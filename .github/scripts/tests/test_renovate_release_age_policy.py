"""Guard: Renovate's release-age windows and the action-major review lane (FND-4651).

The windows are policy, recorded in docs/standards/ci.md under "Renovate
release-age windows": 7 days on GitHub Actions (the org baseline's full window),
and a recorded 3-day exception on the PyPI, pre-commit, pkl and lock-refresh
lanes. They are set in five places across four files, so this test resolves the
layered packageRules for representative dependencies and checks each lane.

The resolver mirrors Renovate's packageRules semantics for the matchers these
configs use: rules apply in order, preset first, and a later matching rule
overrides only the options it sets. A rule using any other matcher fails the
test rather than being read as matching, so a new matcher cannot silently turn a
rule into a no-op here.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PRESET = _REPO_ROOT / "renovate-config" / "default.json"
_REPO_CONFIG = _REPO_ROOT / "renovate.json"
_SELF_HOSTED = _REPO_ROOT / "renovate-config" / "self-hosted.js"
_LOCK_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "renovate-lock-cooldown.yaml"
_CI_STANDARD = _REPO_ROOT / "docs" / "standards" / "ci.md"

ACTIONS_WINDOW = "7 days"
EXCEPTION_WINDOW = "3 days"
EXCEPTION_SPAN = "P3D"

_KNOWN_MATCHERS = {
    "matchManagers",
    "matchDatasources",
    "matchPackageNames",
    "matchDepNames",
    "matchUpdateTypes",
    "matchFileNames",
}

Dep = dict[str, str]

THIRD_PARTY_ACTION: Dep = {
    "manager": "github-actions",
    "datasource": "github-tags",
    "depName": "actions/checkout",
    "packageName": "actions/checkout",
}
FIRST_PARTY_ACTION: Dep = {
    "manager": "github-actions",
    "datasource": "github-tags",
    "depName": "atlanhq/application-sdk",
    "packageName": "atlanhq/application-sdk",
}
# astral-sh/setup-uv's `version:` input, as the github-actions manager extracts it.
SETUP_UV_INPUT: Dep = {
    "manager": "github-actions",
    "datasource": "github-releases",
    "depName": "astral-sh/uv",
    "packageName": "astral-sh/uv",
}
# The uv binary cache key in .github/actions/setup-deps/action.yaml.
UV_CACHE_KEY: Dep = {
    "manager": "custom.regex",
    "datasource": "github-releases",
    "depName": "astral-sh/uv",
    "packageName": "astral-sh/uv",
}
# `container:` / `services:` images the github-actions manager also extracts.
ACTION_DOCKER_IMAGE: Dep = {
    "manager": "github-actions",
    "datasource": "docker",
    "depName": "postgres",
    "packageName": "postgres",
}
# `runs-on:` labels: no release timestamp, so a hold would suppress them outright.
RUNNER_LABEL: Dep = {
    "manager": "github-actions",
    "datasource": "github-runners",
    "depName": "ubuntu",
    "packageName": "ubuntu",
}
PYPI_DEP: Dep = {
    "manager": "pep621",
    "datasource": "pypi",
    "depName": "httpx",
    "packageName": "httpx",
}
PKL: Dep = {
    "manager": "custom.regex",
    "datasource": "github-tags",
    "depName": "pkl",
    "packageName": "apple/pkl",
}
PRE_COMMIT_HOOK: Dep = {
    "manager": "pre-commit",
    "datasource": "github-tags",
    "depName": "pycqa/isort",
    "packageName": "pycqa/isort",
}


def _rules() -> list[dict[str, Any]]:
    preset = json.loads(_PRESET.read_text())
    repo = json.loads(_REPO_CONFIG.read_text())
    assert "github>atlanhq/application-sdk//renovate-config/default.json" in repo.get(
        "extends", []
    ), "renovate.json no longer extends the preset; the layering below is wrong"
    return preset["packageRules"] + repo["packageRules"]


def _names_match(patterns: list[str], name: str) -> bool:
    positive = [p for p in patterns if not p.startswith("!")]
    negative = [p[1:] for p in patterns if p.startswith("!")]
    if any(fnmatch.fnmatchcase(name.lower(), p.lower()) for p in negative):
        return False
    return not positive or any(
        fnmatch.fnmatchcase(name.lower(), p.lower()) for p in positive
    )


def _matches(rule: dict[str, Any], dep: Dep, update_type: str) -> bool:
    unknown = {k for k in rule if k.startswith("match")} - _KNOWN_MATCHERS
    assert not unknown, f"resolver does not model {sorted(unknown)}: {rule}"
    file_name = dep.get("packageFile", ".github/workflows/ci.yaml")
    checks = [
        ("matchManagers", dep["manager"]),
        ("matchDatasources", dep["datasource"]),
        ("matchUpdateTypes", update_type),
    ]
    for key, value in checks:
        if key in rule and value not in rule[key]:
            return False
    if "matchPackageNames" in rule and not _names_match(
        rule["matchPackageNames"], dep["packageName"]
    ):
        return False
    if "matchDepNames" in rule and not _names_match(
        rule["matchDepNames"], dep["depName"]
    ):
        return False
    if "matchFileNames" in rule and not any(
        fnmatch.fnmatchcase(file_name, p) for p in rule["matchFileNames"]
    ):
        return False
    return True


def resolve(dep: Dep, update_type: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for rule in _rules():
        if _matches(rule, dep, update_type):
            merged.update(
                {
                    k: v
                    for k, v in rule.items()
                    if not k.startswith("match") and k != "description"
                }
            )
    return merged


class TestActionsHoldSevenDays:
    @pytest.mark.parametrize("update_type", ["major", "minor", "patch"])
    @pytest.mark.parametrize(
        "dep",
        [THIRD_PARTY_ACTION, SETUP_UV_INPUT, UV_CACHE_KEY],
        ids=lambda d: d["manager"] + ":" + d["datasource"],
    )
    def test_third_party_action_lanes_wait_seven_days(
        self, dep: Dep, update_type: str
    ) -> None:
        if dep is UV_CACHE_KEY and update_type == "major":
            pytest.skip("the preset disables custom.regex majors")
        assert resolve(dep, update_type).get("minimumReleaseAge") == ACTIONS_WINDOW

    @pytest.mark.parametrize("update_type", ["minor", "patch"])
    def test_uv_cache_key_moves_with_the_setup_uv_input(self, update_type: str) -> None:
        """Both halves of one uv bump must age alike and share a group, or the
        key lands alone and test_setup_deps_uv_cache_key.py fails on the PR."""
        key = resolve(UV_CACHE_KEY, update_type)
        install = resolve(SETUP_UV_INPUT, update_type)
        assert key.get("minimumReleaseAge") == install.get("minimumReleaseAge")
        assert key.get("groupName") == install.get("groupName") == "github-actions"

    @pytest.mark.parametrize("update_type", ["major", "minor", "patch"])
    def test_first_party_actions_are_not_held(self, update_type: str) -> None:
        assert resolve(FIRST_PARTY_ACTION, update_type).get("minimumReleaseAge") is None

    @pytest.mark.parametrize(
        ("dep", "update_type"),
        [
            (THIRD_PARTY_ACTION, "digest"),
            (THIRD_PARTY_ACTION, "pinDigest"),
            (ACTION_DOCKER_IMAGE, "minor"),
            (RUNNER_LABEL, "minor"),
        ],
        ids=["digest", "pinDigest", "docker", "runner"],
    )
    def test_lanes_without_a_reliable_timestamp_are_not_held(
        self, dep: Dep, update_type: str
    ) -> None:
        """minimumReleaseAgeBehaviour defaults to timestamp-required: a hold on
        an update with no release timestamp suppresses it for good."""
        assert resolve(dep, update_type).get("minimumReleaseAge") is None


class TestActionMajorsAreReviewed:
    def test_action_major_is_not_auto_merged(self) -> None:
        config = resolve(THIRD_PARTY_ACTION, "major")
        assert config.get("automerge") is False
        assert config.get("platformAutomerge") is False
        # separateMajorMinor gives it renovate/major-github-actions, the slug the
        # fleet dashboard classifier reads as a human-review lane.
        assert config.get("groupName") == "github-actions"

    @pytest.mark.parametrize("update_type", ["minor", "patch", "digest"])
    def test_action_non_majors_still_auto_merge(self, update_type: str) -> None:
        config = resolve(THIRD_PARTY_ACTION, update_type)
        assert config.get("automerge") is True
        assert config.get("platformAutomerge") is True


class TestThreeDayException:
    @pytest.mark.parametrize(
        "dep", [PYPI_DEP, PKL, PRE_COMMIT_HOOK], ids=["pypi", "pkl", "pre-commit"]
    )
    def test_exception_lanes_use_the_recorded_window(self, dep: Dep) -> None:
        assert resolve(dep, "minor").get("minimumReleaseAge") == EXCEPTION_WINDOW

    def test_fleet_lock_refresh_uses_the_recorded_span(self) -> None:
        spans = set(re.findall(r"--window (P\w+)", _SELF_HOSTED.read_text()))
        assert spans == {EXCEPTION_SPAN}

    def test_sdk_lock_refresh_uses_the_recorded_span(self) -> None:
        spans = set(re.findall(r"--window (P\w+)", _LOCK_WORKFLOW.read_text()))
        assert spans == {EXCEPTION_SPAN}

    def test_the_exception_is_recorded(self) -> None:
        text = _CI_STANDARD.read_text()
        start = text.index("\n## Renovate release-age windows\n")
        section = text[start : text.index("\n## ", start + 1)]
        assert f"| {ACTIONS_WINDOW} |" in section
        assert f"| {EXCEPTION_WINDOW} |" in section
        windows = set(re.findall(r"\| (\d+ days) \|", section))
        assert windows == {ACTIONS_WINDOW, EXCEPTION_WINDOW}, (
            "the recorded table names a window no config is checked against"
        )
