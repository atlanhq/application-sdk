"""Every workflow's command line for the FND-3839 scripts must parse.

Unit tests call these scripts' functions directly, so none of them sees the
argv a workflow actually builds. That gap shipped a `--repo` placed after an
argparse subcommand: rejected with exit 2 on every PR, while every unit test
passed. This lifts each `python3 …/<script>.py …` invocation out of the
workflows, substitutes placeholder values for its `$VARS`, and feeds the
result to the script's real parser — stopping right after a successful parse,
so nothing touches GitHub.
"""

from __future__ import annotations

import argparse
import importlib
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

REPO_ROOT = SCRIPTS_DIR.parents[1]
WORKFLOWS = REPO_ROOT / ".github/workflows"

#: Scripts whose workflow argv is checked (the ones FND-3839 added or rewired).
SCRIPTS = (
    "bump_pr_opt_in",
    "pr_test_command",
    "gate_check_verdict",
    "test_command_comment",
    "e2e_branch_recheck",
    "rerun_deferred_gate",
    # FND-4036: the release train's argv carries its safety mode.
    "release_train",
)

_CALL = re.compile(r"python3?\s+\S*?/(?P<script>[a-z_]+)\.py\b(?P<args>.*)", re.S)


class _Parsed(Exception):
    pass


def _invocations() -> list[tuple[str, str, list[str]]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                run = (step or {}).get("run")
                if not run:
                    continue
                # Folded (`>-`) and continued (`\\`) lines become one command.
                flat = run.replace("\\\n", " ")
                for line in flat.splitlines() if "\\" in run else [flat]:
                    match = _CALL.search(line)
                    if not match or match.group("script") not in SCRIPTS:
                        continue
                    args = match.group("args").split(">>")[0].split("|")[0]
                    # "1" is a valid placeholder for both string and int args.
                    argv = [
                        "1" if part.startswith("$") else part
                        for part in shlex.split(args)
                    ]
                    found.append((f"{path.name}:{job_id}", match.group("script"), argv))
    return found


INVOCATIONS = _invocations()


def test_the_sweep_finds_the_callers() -> None:
    scripts = {script for _, script, _ in INVOCATIONS}
    assert {"bump_pr_opt_in", "pr_test_command", "gate_check_verdict"} <= scripts


@pytest.mark.parametrize(
    ("where", "script", "argv"),
    INVOCATIONS,
    ids=[f"{w}:{s}" for w, s, _ in INVOCATIONS],
)
def test_workflow_argv_parses(
    monkeypatch: pytest.MonkeyPatch, where: str, script: str, argv: list[str]
) -> None:
    real = argparse.ArgumentParser.parse_args

    def parse_then_stop(self, args=None, namespace=None):
        real(self, args, namespace)
        raise _Parsed

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse_then_stop)
    monkeypatch.setenv("GITHUB_REPOSITORY", "atlanhq/example")
    module = importlib.import_module(script)
    with pytest.raises(_Parsed):
        module.main(argv)


def test_the_guard_catches_an_option_after_its_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red-green: the exact argv that shipped broken must fail this parse."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "atlanhq/example")
    module = importlib.import_module("bump_pr_opt_in")
    with pytest.raises(SystemExit) as exit_:
        module.main(["e2e-requested", "--repo", "1", "--head-ref", "1", "--sha", "1"])
    assert exit_.value.code == 2
