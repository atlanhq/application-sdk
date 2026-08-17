"""Guard: the renovate-config-validator pre-commit hook must be pinned to the
same renovate version the fleet runner installs (FND-359).

`.pre-commit-config.yaml` pins the `renovatebot/pre-commit-hooks` rev, whose
`additional_dependencies` is `renovate@<rev>`; `.github/workflows/renovate.yaml`
pins the CLI the fleet actually runs. If those drift, the validator stops being
evidence about the engine reading these files: a config could validate locally
and still be rejected — or, worse, silently degraded — on the runner, which is
precisely the failure the hook was added to catch.

Renovate ships several releases a week, so the two pins are bumped together
(one Renovate PR touches both files, since the hook rev and the npm pin are
separate datasources). This test is what makes "together" non-optional.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PRE_COMMIT = _REPO_ROOT / ".pre-commit-config.yaml"
_WORKFLOW = _REPO_ROOT / ".github/workflows/renovate.yaml"

_HOOKS_REPO = "https://github.com/renovatebot/pre-commit-hooks"
_CLI_PIN_RE = re.compile(r"npm install -g renovate@(?P<version>[\w.\-]+)")


def hook_rev() -> str:
    """The pinned rev of the renovate pre-commit hooks repo."""
    config = yaml.safe_load(_PRE_COMMIT.read_text())
    for repo in config["repos"]:
        if repo.get("repo") == _HOOKS_REPO:
            return str(repo["rev"])
    raise AssertionError(
        f"{_HOOKS_REPO} is not in .pre-commit-config.yaml — the shared preset "
        "and the runner's admin config would go unvalidated again"
    )


def runner_cli_version() -> str:
    """The renovate CLI version the fleet runner installs."""
    match = _CLI_PIN_RE.search(_WORKFLOW.read_text())
    assert match, "no `npm install -g renovate@<version>` pin in renovate.yaml"
    return match.group("version")


def validated_hook_ids() -> list[str]:
    """Hook ids enabled from the renovate pre-commit hooks repo."""
    config = yaml.safe_load(_PRE_COMMIT.read_text())
    for repo in config["repos"]:
        if repo.get("repo") == _HOOKS_REPO:
            return [hook["id"] for hook in repo["hooks"]]
    return []


def validator_files_pattern() -> str:
    """The `files` override on the validator hook."""
    config = yaml.safe_load(_PRE_COMMIT.read_text())
    for repo in config["repos"]:
        if repo.get("repo") == _HOOKS_REPO:
            for hook in repo["hooks"]:
                if hook["id"] == "renovate-config-validator":
                    return hook.get("files", "")
    return ""


class TestPinParity:
    def test_hook_rev_matches_the_runner_cli_pin(self):
        assert hook_rev() == runner_cli_version(), (
            "the pre-commit validator and the fleet runner must be the same "
            "renovate version, or local validation stops being evidence about "
            "the engine that actually reads these files"
        )


class TestHookCoverage:
    def test_the_validator_hook_is_enabled(self):
        assert "renovate-config-validator" in validated_hook_ids()

    def test_shared_preset_is_covered(self):
        # Upstream's default filter only matches renovate.json / .renovaterc,
        # which would leave the file with fleet-wide blast radius unvalidated.
        pattern = validator_files_pattern()
        assert pattern, "the hook must override `files` to cover renovate-config/"
        assert re.match(pattern, "renovate-config/default.json")

    def test_admin_config_is_covered(self):
        assert re.match(validator_files_pattern(), "renovate-config/self-hosted.js")

    def test_repo_own_config_is_covered(self):
        assert re.match(validator_files_pattern(), "renovate.json")

    def test_unrelated_json_is_not_swept_in(self):
        pattern = validator_files_pattern()
        assert not re.match(pattern, "contract_schema.lock.json")
        assert not re.match(pattern, "packages/conformance/pyproject.toml")
