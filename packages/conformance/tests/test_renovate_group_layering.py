"""The fleet preset layered under an app's renovate.json lands each first-party
bump on the branch and auto-merge setting the fleet expects (FND-2868).

Renovate applies packageRules in order, preset first, and a later matching rule
overrides only the options it sets. The branch comes from ``groupSlug`` when one
is set, else from ``groupName``. A split in the app's own renovate.json only
works when nothing upstream pins the slug, so these tests evaluate the layered
result, not the preset alone.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import pytest
from conformance.bootstrap.render import render

_PRESET = Path(__file__).resolve().parents[3] / "renovate-config" / "default.json"

SDK = {"name": "atlan-application-sdk", "manager": "pep621", "datasource": "pypi"}
CONFORMANCE = {
    "name": "atlan-application-sdk-conformance",
    "manager": "pep621",
    "datasource": "pypi",
}
TOOLKIT = {"name": "app-contract-toolkit", "manager": "custom.regex", "datasource": ""}


def _names_match(patterns: list[str], name: str) -> bool:
    positive = [p for p in patterns if not p.startswith("!")]
    negative = [p[1:] for p in patterns if p.startswith("!")]
    if any(fnmatch.fnmatchcase(name, p) for p in negative):
        return False
    return not positive or any(fnmatch.fnmatchcase(name, p) for p in positive)


def _matches(rule: dict, dep: dict, update_type: str) -> bool:
    checks = {
        "matchPackageNames": dep["name"],
        "matchDepNames": dep["name"],
    }
    for key, value in checks.items():
        if key in rule and not _names_match(rule[key], value):
            return False
    if "matchManagers" in rule and dep["manager"] not in rule["matchManagers"]:
        return False
    if "matchDatasources" in rule and dep["datasource"] not in rule["matchDatasources"]:
        return False
    if "matchUpdateTypes" in rule and update_type not in rule["matchUpdateTypes"]:
        return False
    return True


def _effective(repo_json: dict, dep: dict, update_type: str = "minor") -> dict:
    rules = json.loads(_PRESET.read_text())["packageRules"] + repo_json.get(
        "packageRules", []
    )
    config: dict = {}
    for rule in rules:
        if _matches(rule, dep, update_type):
            config.update(rule)
    return config


def _branch(config: dict) -> str:
    slug = config.get("groupSlug") or config.get("groupName") or ""
    return "renovate/" + re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")


HARD = json.loads(render("renovate.json"))
SOFT = json.loads(render("renovate.json", automerge="false"))
GROUP_BRANCH = "renovate/atlan-framework-dependencies"


@pytest.mark.parametrize("dep", [SDK, CONFORMANCE, TOOLKIT], ids=lambda d: d["name"])
def test_hard_mode_groups_every_first_party_bump_and_auto_merges(dep: dict) -> None:
    config = _effective(HARD, dep)
    assert _branch(config) == GROUP_BRANCH
    assert config.get("automerge") is True


def test_soft_mode_splits_conformance_onto_its_own_auto_merged_branch() -> None:
    config = _effective(SOFT, CONFORMANCE)
    assert _branch(config) == "renovate/conformance-package"
    assert config.get("automerge") is True


@pytest.mark.parametrize("dep", [SDK, TOOLKIT], ids=lambda d: d["name"])
def test_soft_mode_keeps_the_rest_grouped_for_a_human(dep: dict) -> None:
    config = _effective(SOFT, dep)
    assert _branch(config) == GROUP_BRANCH
    assert config.get("automerge") is False


def test_sdk_opt_out_recipe_splits_the_sdk_and_leaves_the_group_auto_merging() -> None:
    opt_out = {
        "packageRules": [
            {
                "matchPackageNames": ["atlan-application-sdk"],
                "groupName": "atlan-application-sdk",
                "automerge": False,
                "platformAutomerge": False,
            }
        ]
    }
    sdk = _effective(opt_out, SDK)
    assert _branch(sdk) == "renovate/atlan-application-sdk"
    assert sdk.get("automerge") is False
    conformance = _effective(opt_out, CONFORMANCE)
    assert _branch(conformance) == GROUP_BRANCH
    assert conformance.get("automerge") is True
