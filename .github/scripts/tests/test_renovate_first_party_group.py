"""Guard: the SDK, conformance and contract-toolkit lanes share one Renovate PR (FND-2868)."""

from __future__ import annotations

import json
from pathlib import Path

_PRESET = Path(__file__).resolve().parents[3] / "renovate-config/default.json"
_GROUP = "atlan platform"


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
