"""Tests for .github/scripts/discover_org_consumers.py.

The discovery is deterministic: enumerate atlan-*-app repos via `gh repo list`,
then keep only those whose renovate.json extends the shared preset. `gh` is
stubbed via the `run` seam so no real calls are made.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import discover_org_consumers as doc

MARKER = doc.PRESET_MARKER


def _fake_gh(repo_list: list, renovate_json: dict):
    """Build a `run` stub. `repo_list` is what `gh repo list` returns (as a JSON
    array of nameWithOwner); `renovate_json` maps 'owner/repo' -> renovate.json
    contents (absent key => file missing => "")."""

    def run(args: list) -> str:
        if args[:2] == ["repo", "list"]:
            return json.dumps(repo_list)
        if args[0] == "api" and args[-1].startswith("repos/"):
            repo = (
                args[-1].removeprefix("repos/").removesuffix("/contents/renovate.json")
            )
            return renovate_json.get(repo, "")
        raise AssertionError(f"unexpected gh call: {args}")

    return run


def test_run_seam_defaults_are_real_gh_wrapper():
    # main()/discover_fleet() must accept `run` as an explicit passthrough (a
    # mutable-default footgun this suite caught before), defaulting to _run_gh.
    assert (
        inspect.signature(doc.discover_fleet).parameters["run"].default is doc._run_gh
    )
    assert inspect.signature(doc.main).parameters["run"].default is doc._run_gh


def test_run_gh_success_returns_stdout(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "hello"
        stderr = ""

    monkeypatch.setattr(doc.subprocess, "run", lambda *a, **k: _Result())
    assert doc._run_gh(["repo", "list"]) == "hello"


def test_run_gh_failure_returns_empty_and_warns_stderr(monkeypatch, capsys):
    # A real auth/scope/network failure must surface `gh`'s stderr in the log
    # (diagnosable) rather than silently collapsing to "no data".
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "HTTP 401: Bad credentials"

    monkeypatch.setattr(doc.subprocess, "run", lambda *a, **k: _Result())
    assert doc._run_gh(["api", "repos/x/contents/renovate.json"]) == ""
    err = capsys.readouterr().err
    assert "::warning::" in err
    assert "HTTP 401: Bad credentials" in err


def test_parse_repos_valid_and_malformed():
    assert doc.parse_repos('["a", "b"]') == ["a", "b"]
    assert doc.parse_repos("[]") == []
    assert doc.parse_repos("not json") == []
    assert doc.parse_repos('{"not": "a list"}') == []
    assert doc.parse_repos("") == []


def test_list_candidate_repos_filters_to_app_pattern():
    run = _fake_gh(
        [
            "atlanhq/atlan-mysql-app",
            "atlanhq/atlan-hello-world-app",
            "atlanhq/application-sdk",  # not atlan-*-app
            "atlanhq/connectors-sql",  # mirror monorepo, not atlan-*-app
            "atlanhq/atlan-cli",  # not -app
        ],
        {},
    )
    got = doc.list_candidate_repos("atlanhq", doc.DEFAULT_NAME_PATTERN, run=run)
    assert got == ["atlanhq/atlan-mysql-app", "atlanhq/atlan-hello-world-app"]


def test_list_candidate_repos_warns_when_repo_list_hits_cap(monkeypatch, capsys):
    # If the org ever grows into the --limit cap, discovery may be truncated and
    # a consumer silently dropped. That must surface as a loud ::warning::, not a
    # quiet miss. Shrink the cap so the stub can trip it deterministically.
    monkeypatch.setattr(doc, "REPO_LIST_LIMIT", 2)
    run = _fake_gh(
        ["atlanhq/atlan-mysql-app", "atlanhq/atlan-hello-world-app"],  # == cap
        {},
    )
    doc.list_candidate_repos("atlanhq", doc.DEFAULT_NAME_PATTERN, run=run)
    assert "::warning::" in capsys.readouterr().err


def test_extends_preset_true_false_and_missing():
    run = _fake_gh(
        [],
        {
            "atlanhq/atlan-mysql-app": '{"extends": ["github>atlanhq/application-sdk//renovate-config/default.json"]}',
            "atlanhq/atlan-legacy-app": '{"extends": ["config:recommended"]}',
        },
    )
    assert doc.extends_preset("atlanhq/atlan-mysql-app", MARKER, run=run) is True
    assert doc.extends_preset("atlanhq/atlan-legacy-app", MARKER, run=run) is False
    # Missing renovate.json (not in the map) -> "" -> False.
    assert doc.extends_preset("atlanhq/atlan-noconfig-app", MARKER, run=run) is False


def test_discover_fleet_keeps_only_preset_adopters_sorted():
    run = _fake_gh(
        [
            "atlanhq/atlan-mysql-app",
            "atlanhq/atlan-hello-world-app",
            "atlanhq/atlan-legacy-app",  # renovate.json but not our preset
            "atlanhq/atlan-noconfig-app",  # no renovate.json
            "atlanhq/application-sdk",  # filtered by name pattern
        ],
        {
            "atlanhq/atlan-mysql-app": f'{{"extends": ["github>{MARKER}"]}}',
            "atlanhq/atlan-hello-world-app": f'{{"extends": ["github>{MARKER}"]}}',
            "atlanhq/atlan-legacy-app": '{"extends": ["config:recommended"]}',
        },
    )
    got = doc.discover_fleet(
        "atlanhq", doc.DEFAULT_NAME_PATTERN, MARKER, set(), run=run
    )
    # Only the two preset-adopters, sorted.
    assert got == ["atlanhq/atlan-hello-world-app", "atlanhq/atlan-mysql-app"]


def test_discover_fleet_honors_excludes():
    run = _fake_gh(
        ["atlanhq/atlan-mysql-app", "atlanhq/atlan-hello-world-app"],
        {
            "atlanhq/atlan-mysql-app": f'{{"extends": ["github>{MARKER}"]}}',
            "atlanhq/atlan-hello-world-app": f'{{"extends": ["github>{MARKER}"]}}',
        },
    )
    got = doc.discover_fleet(
        "atlanhq",
        doc.DEFAULT_NAME_PATTERN,
        MARKER,
        {"atlanhq/atlan-hello-world-app"},
        run=run,
    )
    assert got == ["atlanhq/atlan-mysql-app"]


def test_main_writes_github_output(tmp_path, monkeypatch, capsys):
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    run = _fake_gh(
        ["atlanhq/atlan-mysql-app", "atlanhq/atlan-legacy-app"],
        {"atlanhq/atlan-mysql-app": f'{{"extends": ["github>{MARKER}"]}}'},
    )
    rc = doc.main(["--owner", "atlanhq"], run=run)
    assert rc == 0
    assert output_file.read_text() == 'repos=["atlanhq/atlan-mysql-app"]\n'
    assert "Discovered 1 fleet repos" in capsys.readouterr().err


def test_main_warns_when_no_fleet(tmp_path, monkeypatch, capsys):
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    rc = doc.main(["--owner", "atlanhq"], run=_fake_gh([], {}))
    assert rc == 0
    assert output_file.read_text() == "repos=[]\n"
    assert "::warning::No fleet repos discovered" in capsys.readouterr().err
