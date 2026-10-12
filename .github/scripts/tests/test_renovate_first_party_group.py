"""Guard: the SDK, conformance and contract-toolkit lanes share one Renovate PR (FND-2868)."""

from __future__ import annotations

import json
from pathlib import Path

_PRESET = Path(__file__).resolve().parents[3] / "renovate-config/default.json"
_GROUP = "atlan framework dependencies"


def _first_party_rules() -> list[dict]:
    rules = json.loads(_PRESET.read_text())["packageRules"]
    return [
        r
        for r in rules
        if r.get("enabled", True)
        and (
            r.get("matchPackageNames")
            in (["atlan-application-sdk"], ["atlan-application-sdk-conformance"])
            or r.get("matchDepNames") == ["app-contract-toolkit"]
        )
    ]


def test_first_party_lanes_share_one_group() -> None:
    rules = _first_party_rules()
    assert len(rules) == 3
    assert {r.get("groupName") for r in rules} == {_GROUP}
    assert {r.get("prPriority") for r in rules} == {10}
    assert not any("groupSlug" in r for r in rules)


def test_group_rules_name_their_dependency() -> None:
    rules = json.loads(_PRESET.read_text())["packageRules"]
    for rule in rules:
        if rule.get("groupName") == _GROUP:
            assert rule.get("matchPackageNames") or rule.get("matchDepNames"), rule


def test_first_party_post_upgrade_tasks_run_per_update() -> None:
    for rule in _first_party_rules():
        tasks = rule.get("postUpgradeTasks")
        if tasks:
            assert tasks["executionMode"] == "update"


def test_slow_lanes_rebase_only_on_conflict() -> None:
    # FND-3316: behind-base reruns are redundant under a merge queue, so the
    # default is `conflicted`; only the lanes that must re-resolve opt back in.
    assert json.loads(_PRESET.read_text())["rebaseWhen"] == "conflicted"


def test_re_resolving_lanes_rebase_when_behind_base() -> None:
    # Every member rule carries it, not just one: rebaseWhen is branch-level, a
    # grouped branch takes its config from its first member, and a repo may split
    # a member out under its own groupName (soft-mode, SDK opt-out).
    for rule in _first_party_rules():
        assert rule.get("rebaseWhen") == "behind-base-branch", rule
    rules = json.loads(_PRESET.read_text())["packageRules"]
    lock_rules = [
        r for r in rules if r.get("matchUpdateTypes") == ["lockFileMaintenance"]
    ]
    assert [r.get("rebaseWhen") for r in lock_rules] == ["behind-base-branch"]
