"""Tests for .github/scripts/check_renovate_allowed_commands.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_renovate_allowed_commands as guard

UV_BOUND = (
    "uv lock --upgrade --exclude-newer P7D "
    "--exclude-newer-package atlan-application-sdk=P0D "
    "--exclude-newer-package atlan-application-sdk-conformance=P0D"
)

SELF_HOSTED_WITH_BOUND = (
    "module.exports = {\n"
    "  allowedCommands: [\n"
    '    "^renovate-pkl-sync --contract-dir contract --regenerate (true|false) --no-commit$",\n'
    f'    "^{UV_BOUND}$",\n'
    "  ],\n"
    "};\n"
)

SELF_HOSTED_WITHOUT_BOUND = (
    "module.exports = {\n"
    "  allowedCommands: [\n"
    '    "^renovate-pkl-sync --contract-dir contract --regenerate (true|false) --no-commit$",\n'
    "  ],\n"
    "};\n"
)


def preset(*commands: str, nest: str = "lockFileMaintenance") -> str:
    """A minimal preset declaring *commands* under *nest*."""
    tasks = {
        "postUpgradeTasks": {"commands": list(commands), "fileFilters": ["uv.lock"]}
    }
    return json.dumps({nest: tasks} if nest else tasks)


class TestPresetCommands:
    def test_finds_commands_under_lock_file_maintenance(self):
        assert guard.preset_commands(preset(UV_BOUND)) == [UV_BOUND]

    def test_finds_commands_at_the_top_level(self):
        assert guard.preset_commands(preset(UV_BOUND, nest="")) == [UV_BOUND]

    def test_finds_commands_inside_package_rules(self):
        text = json.dumps(
            {
                "packageRules": [
                    {"postUpgradeTasks": {"commands": ["renovate-pkl-sync x"]}}
                ]
            }
        )
        assert guard.preset_commands(text) == ["renovate-pkl-sync x"]

    def test_empty_when_no_post_upgrade_tasks(self):
        assert guard.preset_commands('{"packageRules": [{"groupName": "x"}]}') == []


class TestAllowedPatterns:
    def test_extracts_every_entry(self):
        patterns = guard.allowed_patterns(SELF_HOSTED_WITH_BOUND)
        assert len(patterns) == 2
        assert patterns[1] == f"^{UV_BOUND}$"

    def test_empty_when_allowlist_is_absent(self):
        assert guard.allowed_patterns("module.exports = { platform: 'github' };") == []


class TestUnauthorizedCommands:
    def test_flags_command_the_allowlist_does_not_cover(self):
        assert guard.unauthorized_commands(
            preset(UV_BOUND), SELF_HOSTED_WITHOUT_BOUND
        ) == [UV_BOUND]

    def test_clean_when_allowlist_covers_the_command(self):
        assert (
            guard.unauthorized_commands(preset(UV_BOUND), SELF_HOSTED_WITH_BOUND) == []
        )

    def test_flags_a_command_that_drifts_by_one_character(self):
        # The failure this guard exists for: the allowlist is anchored, so
        # editing the duration in the preset alone silently disables the bound.
        drifted = UV_BOUND.replace("P7D", "P3D")
        assert guard.unauthorized_commands(preset(drifted), SELF_HOSTED_WITH_BOUND) == [
            drifted
        ]

    def test_missing_allowlist_authorizes_nothing(self):
        assert guard.unauthorized_commands(
            preset(UV_BOUND), "module.exports = {};"
        ) == [UV_BOUND]

    def test_clean_against_the_real_preset_and_admin_config(self):
        assert (
            guard.unauthorized_commands(
                guard.PRESET.read_text(), guard.SELF_HOSTED.read_text()
            )
            == []
        )


class TestMain:
    def test_exits_zero_against_the_real_repo_state(self):
        assert guard.main() == 0

    def test_exits_nonzero_when_a_command_is_unauthorized(self, monkeypatch, tmp_path):
        preset_path = tmp_path / "default.json"
        preset_path.write_text(preset(UV_BOUND))
        admin_path = tmp_path / "self-hosted.js"
        admin_path.write_text(SELF_HOSTED_WITHOUT_BOUND)

        monkeypatch.setattr(guard, "PRESET", preset_path)
        monkeypatch.setattr(guard, "SELF_HOSTED", admin_path)

        assert guard.main() == 1
