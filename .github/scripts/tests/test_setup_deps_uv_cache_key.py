"""The setup-deps uv binary cache must be keyed on the uv version it installs.

The key and the ``astral-sh/setup-uv`` ``version:`` input are two literals in
.github/actions/setup-deps/action.yaml. They drifted once (key 0.11.21, install
0.13.4), after which every run restored a uv binary setup-uv then ignored.
Renovate bumps the install through its github-actions manager and the key
through a regex manager in renovate.json; these tests keep both halves honest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
ACTION = REPO_ROOT / ".github/actions/setup-deps/action.yaml"
ACTION_REL = ".github/actions/setup-deps/action.yaml"


def _steps() -> list[dict]:
    return yaml.safe_load(ACTION.read_text())["runs"]["steps"]


def _step(predicate) -> dict:
    matches = [s for s in _steps() if predicate(s)]
    assert len(matches) == 1, matches
    return matches[0]


def _installed_version() -> str:
    step = _step(lambda s: str(s.get("uses", "")).startswith("astral-sh/setup-uv@"))
    return str(step["with"]["version"])


def _cache_key() -> str:
    return str(_step(lambda s: s.get("id") == "cache-uv-bin")["with"]["key"])


def _uv_key_manager() -> dict:
    managers = json.loads((REPO_ROOT / "renovate.json").read_text())["customManagers"]
    matches = [
        m
        for m in managers
        if any(
            re.search(p.strip("/"), ACTION_REL)
            for p in m.get("managerFilePatterns", [])
        )
    ]
    assert len(matches) == 1, matches
    return matches[0]


def test_cache_key_names_the_installed_uv_version():
    assert _cache_key().endswith(f"-{_installed_version()}")


def test_renovate_regex_manager_tracks_the_key_under_the_install_dep_name():
    manager = _uv_key_manager()
    # Renovate regex managers use JS named groups; Python spells them (?P<..>).
    pattern = manager["matchStrings"][0].replace("(?<", "(?P<")
    found = re.search(pattern, ACTION.read_text())
    assert found, "renovate.json's uv cache-key manager no longer matches the key"
    assert found.group("currentValue") == _installed_version()
    # Same depName as the github-actions manager's setup-uv extraction, so
    # Renovate puts both bumps in one branch.
    assert manager["depNameTemplate"] == "astral-sh/uv"
