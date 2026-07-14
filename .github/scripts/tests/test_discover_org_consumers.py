"""Tests for .github/scripts/discover_org_consumers.py."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import discover_org_consumers as doc


def test_discover_repos_default_run_is_real_gh_wrapper():
    # Guards against a real bug this test suite caught once already: a mutable
    # default argument (run: RunFn = _run_gh) is bound at def-time, so
    # monkeypatching the module-level _run_gh does NOT redirect calls that
    # rely on the default — main()/discover_repos() must accept `run` as an
    # explicit passthrough param for tests to ever safely avoid the real `gh`.
    assert (
        inspect.signature(doc.discover_repos).parameters["run"].default is doc._run_gh
    )
    assert inspect.signature(doc.main).parameters["run"].default is doc._run_gh


def test_parse_repos_valid_json_list():
    assert doc.parse_repos('["atlanhq/a", "atlanhq/b"]') == ["atlanhq/a", "atlanhq/b"]


def test_parse_repos_empty_list():
    assert doc.parse_repos("[]") == []


def test_parse_repos_malformed_json_returns_empty():
    assert doc.parse_repos("not json") == []


def test_parse_repos_non_list_json_returns_empty():
    assert doc.parse_repos('{"not": "a list"}') == []


def test_parse_repos_empty_string_returns_empty():
    assert doc.parse_repos("") == []


def test_discover_repos_calls_gh_search_with_expected_args():
    calls = []

    def fake_run(args):
        calls.append(args)
        return "[]"

    doc.discover_repos(
        "atlanhq", "atlan-application-sdk filename:pyproject.toml", run=fake_run
    )
    assert calls == [
        [
            "search",
            "code",
            "--owner",
            "atlanhq",
            "atlan-application-sdk filename:pyproject.toml",
            "--json",
            "repository",
            "--jq",
            "[.[].repository.nameWithOwner] | unique",
            "--limit",
            "100",
        ]
    ]


def test_discover_repos_returns_parsed_list():
    def fake_run(args):
        return '["atlanhq/atlan-mysql-app", "atlanhq/atlan-openapi-app"]'

    result = doc.discover_repos("atlanhq", "query", run=fake_run)
    assert result == ["atlanhq/atlan-mysql-app", "atlanhq/atlan-openapi-app"]


def test_discover_repos_empty_when_run_fails():
    def fake_run(args):
        return "[]"  # _run_gh's own failure fallback

    assert doc.discover_repos("atlanhq", "query", run=fake_run) == []


def test_main_writes_github_output(tmp_path, monkeypatch, capsys):
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    def fake_run(args):
        return '["atlanhq/a"]'

    rc = doc.main(["--owner", "atlanhq", "--query", "some query"], run=fake_run)
    assert rc == 0

    written = output_file.read_text()
    assert written == 'repos=["atlanhq/a"]\n'

    err = capsys.readouterr().err
    assert "Discovered 1 consumer repos" in err


def test_main_warns_when_no_repos_discovered(tmp_path, monkeypatch, capsys):
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    rc = doc.main(
        ["--owner", "atlanhq", "--query", "some query"], run=lambda args: "[]"
    )
    assert rc == 0
    assert output_file.read_text() == "repos=[]\n"

    err = capsys.readouterr().err
    assert "::warning::No consumer repos discovered" in err
