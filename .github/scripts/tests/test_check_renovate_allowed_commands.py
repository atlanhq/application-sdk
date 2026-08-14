"""Tests for .github/scripts/check_renovate_allowed_commands.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_renovate_allowed_commands as guard

# A synthetic second command, not one the preset actually declares: these tests
# exercise the pairing logic, and pinning them to whatever command happens to be
# configured today would make them re-fail every time a lane is added or removed.
# The real preset and admin config are checked against each other separately, in
# TestUnauthorizedCommands.test_clean_against_the_real_preset_and_admin_config.
SECOND_COMMAND = "fleet-tool --mode strict --window P7D --scope all"

SELF_HOSTED_WITH_BOUND = (
    "module.exports = {\n"
    "  allowedCommands: [\n"
    '    "^renovate-pkl-sync --contract-dir contract --regenerate (true|false) --no-commit$",\n'
    f'    "^{SECOND_COMMAND}$",\n'
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
        assert guard.preset_commands(preset(SECOND_COMMAND)) == [SECOND_COMMAND]

    def test_finds_commands_at_the_top_level(self):
        assert guard.preset_commands(preset(SECOND_COMMAND, nest="")) == [
            SECOND_COMMAND
        ]

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
        assert patterns[1] == f"^{SECOND_COMMAND}$"

    def test_empty_when_allowlist_is_absent(self):
        assert guard.allowed_patterns("module.exports = { platform: 'github' };") == []

    def test_a_bracket_inside_an_entry_does_not_truncate_the_allowlist(self):
        # An entry is itself a regex, so it can contain a character class. Ending
        # the scan at the first `]` would drop every later entry and silently
        # authorize nothing — the guard would then pass commands it never saw.
        source = (
            "module.exports = {\n"
            "  allowedCommands: [\n"
            '    "^first --window P[0-9]+D$",\n'
            '    "^second --flag$",\n'
            "  ],\n"
            "};\n"
        )
        assert guard.allowed_patterns(source) == [
            "^first --window P[0-9]+D$",
            "^second --flag$",
        ]

    def test_quotes_inside_a_comment_are_not_collected_as_entries(self):
        # Comments explaining the patterns are normal in this array, and prose
        # routinely quotes things. A quote pair in a comment must not be read as
        # a pattern, or every entry after it desynchronises.
        source = (
            "module.exports = {\n"
            "  allowedCommands: [\n"
            '    // avoid backslashes: "\\d" is just "d" to a JS string literal\n'
            '    "^only --real$",\n'
            "  ],\n"
            "};\n"
        )
        assert guard.allowed_patterns(source) == ["^only --real$"]


class TestUnauthorizedCommands:
    def test_flags_command_the_allowlist_does_not_cover(self):
        assert guard.unauthorized_commands(
            preset(SECOND_COMMAND), SELF_HOSTED_WITHOUT_BOUND
        ) == [SECOND_COMMAND]

    def test_clean_when_allowlist_covers_the_command(self):
        assert (
            guard.unauthorized_commands(preset(SECOND_COMMAND), SELF_HOSTED_WITH_BOUND)
            == []
        )

    def test_flags_a_command_that_drifts_by_one_character(self):
        # The failure this guard exists for: the allowlist is anchored, so
        # editing the duration in the preset alone silently disables the bound.
        drifted = SECOND_COMMAND.replace("P7D", "P3D")
        assert guard.unauthorized_commands(preset(drifted), SELF_HOSTED_WITH_BOUND) == [
            drifted
        ]

    def test_missing_allowlist_authorizes_nothing(self):
        assert guard.unauthorized_commands(
            preset(SECOND_COMMAND), "module.exports = {};"
        ) == [SECOND_COMMAND]

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
        preset_path.write_text(preset(SECOND_COMMAND))
        admin_path = tmp_path / "self-hosted.js"
        admin_path.write_text(SELF_HOSTED_WITHOUT_BOUND)

        monkeypatch.setattr(guard, "PRESET", preset_path)
        monkeypatch.setattr(guard, "SELF_HOSTED", admin_path)

        assert guard.main() == 1
