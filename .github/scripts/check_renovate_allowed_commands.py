#!/usr/bin/env python3
"""Guard: every postUpgradeTasks command in the shared fleet preset must be
authorized by the self-hosted runner's allowedCommands allowlist.

renovate-config/default.json (repo-settable) declares the commands;
renovate-config/self-hosted.js (admin-only) declares the regexes that authorize
them. Renovate silently skips a command the allowlist does not match — it logs a
line and carries on, producing a PR that looks normal, so the drift is invisible
at exactly the moment it matters (FND-367: a command dropped this way refreshed a
lock with none of the constraints it was supposed to carry, and nothing went red).
The two files sit in different languages and are edited independently, so the
pairing gets a test rather than a comment.

Assumes the allowlist entries are regexes valid in both JS and Python (the
current ones are plain anchored literals with alternation). Keep them that way.

Run standalone (`python3 check_renovate_allowed_commands.py`) or via the tested
functions here (`.github/scripts/tests/test_check_renovate_allowed_commands.py`).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
PRESET = REPO_ROOT / "renovate-config" / "default.json"
SELF_HOSTED = REPO_ROOT / "renovate-config" / "self-hosted.js"

# Where the allowedCommands array starts in the admin config. The array is then
# scanned character by character rather than matched with a regex: the entries
# are themselves regexes, and `allowedCommands:\s*\[(.*?)\]` stops at the first
# `]` — including one inside a character class like `[0-9]`, which silently
# truncates the allowlist and makes the guard pass commands it never checked.
# Exactly the silent-failure mode this file exists to prevent, so it is scanned
# properly. A JS parser is still not worth the dependency for one array of
# string literals.
_ALLOWLIST_START_RE = re.compile(r"allowedCommands:\s*\[")


def preset_commands(preset_json: str) -> list[str]:
    """Every postUpgradeTasks command declared anywhere in the preset.

    Walks the whole document rather than the known locations: postUpgradeTasks is
    inheritable, so it can sit at the top level, inside lockFileMaintenance, or
    inside any packageRules entry, and a future lane could add one elsewhere.
    """
    commands: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            tasks = node.get("postUpgradeTasks")
            if isinstance(tasks, dict):
                commands.extend(
                    c for c in tasks.get("commands", []) if isinstance(c, str)
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(preset_json))
    return commands


def allowed_patterns(self_hosted_source: str) -> list[str]:
    """Regex strings from the admin config's allowedCommands allowlist.

    Scans to the array's closing bracket while tracking string state, so a `]`
    inside an entry (a character class, say) ends the entry rather than the
    allowlist. `//` comments between entries are skipped, including any quotes
    they contain — prose about the patterns is normal here, and a stray
    apostrophe-pair in a comment would otherwise be collected as a pattern and
    desynchronise everything after it.
    """
    start = _ALLOWLIST_START_RE.search(self_hosted_source)
    if not start:
        return []

    patterns: list[str] = []
    current: list[str] = []
    in_string = False
    escaped = False
    in_comment = False
    previous = ""

    for char in self_hosted_source[start.end() :]:
        if in_comment:
            if char == "\n":
                in_comment = False
            continue
        if not in_string and char == "/" and previous == "/":
            in_comment = True
            previous = ""
            continue
        previous = char
        if in_string:
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\":
                current.append(char)
                escaped = True
            elif char == '"':
                patterns.append("".join(current))
                current = []
                in_string = False
            else:
                current.append(char)
        elif char == '"':
            in_string = True
        elif char == "]":
            break

    return patterns


def unauthorized_commands(preset_json: str, self_hosted_source: str) -> list[str]:
    """Preset commands that no allowlist pattern authorizes."""
    patterns = [re.compile(p) for p in allowed_patterns(self_hosted_source)]
    return [
        command
        for command in preset_commands(preset_json)
        if not any(p.search(command) for p in patterns)
    ]


def main() -> int:
    unauthorized = unauthorized_commands(PRESET.read_text(), SELF_HOSTED.read_text())
    if unauthorized:
        for command in unauthorized:
            print(
                f"postUpgradeTasks command is not authorized by "
                f"renovate-config/self-hosted.js allowedCommands: {command!r}. "
                "Add a matching anchored regex there, or the fleet runner will "
                "skip the command with only a log line to show for it.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
