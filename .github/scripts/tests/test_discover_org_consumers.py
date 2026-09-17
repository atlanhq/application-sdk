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
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import discover_org_consumers as doc

MARKER = doc.PRESET_MARKER


NOT_FOUND = (1, "", "gh: Not Found (HTTP 404)")


def _fake_gh(repo_list, renovate_json: dict, fails: Optional[dict] = None):
    """Build a `run` stub returning (returncode, stdout, stderr).

    `repo_list` is what `gh repo list` returns (as a JSON array of
    nameWithOwner), or None to make the listing itself fail; `renovate_json`
    maps 'owner/repo' -> renovate.json contents, and an absent key is a real 404
    (no such file). `fails` maps 'owner/repo' -> stderr for reads that fail for
    some OTHER reason, which must never be read as "no renovate.json"."""
    fails = fails or {}

    def run(args: list) -> tuple:
        if args[:2] == ["repo", "list"]:
            if repo_list is None:
                return 1, "", "HTTP 401: Bad credentials"
            return 0, json.dumps(repo_list), ""
        if args[0] == "api" and args[-1].startswith("repos/"):
            repo = (
                args[-1].removeprefix("repos/").removesuffix("/contents/renovate.json")
            )
            if repo in fails:
                return 1, "", fails[repo]
            if repo not in renovate_json:
                return NOT_FOUND
            return 0, renovate_json[repo], ""
        raise AssertionError(f"unexpected gh call: {args}")

    return run


def test_run_seam_defaults_are_real_gh_wrapper():
    # main()/discover_fleet() must accept `run` as an explicit passthrough (a
    # mutable-default footgun this suite caught before), defaulting to _run_gh.
    assert (
        inspect.signature(doc.discover_fleet).parameters["run"].default is doc._run_gh
    )
    assert inspect.signature(doc.main).parameters["run"].default is doc._run_gh


def test_run_gh_reports_the_exit_code_and_both_streams(monkeypatch):
    """The seam interprets nothing: the caller decides what a failure means."""

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "HTTP 401: Bad credentials"

    monkeypatch.setattr(doc.subprocess, "run", lambda *a, **k: _Result())
    assert doc._run_gh(["api", "repos/x/contents/renovate.json"]) == (
        1,
        "",
        "HTTP 401: Bad credentials",
    )


def test_only_a_404_reads_as_a_missing_file():
    # The whole point of the distinction: everything except a 404 says something
    # about the request, not about the repo.
    assert doc._is_not_found("gh: Not Found (HTTP 404)") is True
    assert doc._is_not_found("HTTP 401: Bad credentials") is False
    assert doc._is_not_found("HTTP 403: rate limit exceeded") is False
    assert doc._is_not_found("HTTP 502: Bad Gateway") is False
    assert doc._is_not_found("dial tcp: lookup api.github.com: no such host") is False


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
    # Missing renovate.json (not in the map) -> a real 404 -> False.
    assert doc.extends_preset("atlanhq/atlan-noconfig-app", MARKER, run=run) is False


def test_extends_preset_raises_when_the_read_fails_for_any_other_reason():
    """A 401/403/5xx says nothing about the repo's renovate.json.

    Reading it as False drops a live consumer from the roster, which shows up as
    a missing dashboard row — or, in the unlisted sweep, as deleted data for a
    repo that does belong.
    """
    for stderr in (
        "HTTP 401: Bad credentials",
        "HTTP 403: API rate limit exceeded",
        "HTTP 502: Bad Gateway",
    ):
        run = _fake_gh([], {}, fails={"atlanhq/atlan-mysql-app": stderr})
        with pytest.raises(doc.DiscoveryError):
            doc.extends_preset("atlanhq/atlan-mysql-app", MARKER, run=run)


def test_list_candidate_repos_raises_when_the_listing_fails():
    """An empty org listing is always a token problem, never an empty org."""
    with pytest.raises(doc.DiscoveryError):
        doc.list_candidate_repos(
            "atlanhq", doc.DEFAULT_NAME_PATTERN, run=_fake_gh(None, {})
        )


def test_discover_fleet_aborts_rather_than_returning_a_partial_fleet():
    """One unreadable repo invalidates the whole answer.

    Returning "the fleet minus an unknown number of repos we could not check"
    gives every caller a roster it cannot tell from a complete one.
    """
    run = _fake_gh(
        ["atlanhq/atlan-mysql-app", "atlanhq/atlan-postgres-app"],
        {"atlanhq/atlan-mysql-app": f'{{"extends": ["github>{MARKER}"]}}'},
        fails={"atlanhq/atlan-postgres-app": "HTTP 403: API rate limit exceeded"},
    )
    with pytest.raises(doc.DiscoveryError):
        doc.discover_fleet("atlanhq", doc.DEFAULT_NAME_PATTERN, MARKER, set(), run=run)


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


def test_main_fails_on_empty_when_asked(tmp_path, monkeypatch, capsys):
    """--fail-on-empty reds the run instead of proceeding on an empty fleet.

    A 401 on `gh repo list` yields no data, which is indistinguishable from a
    fleet with no consumers. The dashboard derives its ENTIRE publish scope from
    this output, so an empty answer there must stop the run.
    """
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    rc = doc.main(["--owner", "atlanhq", "--fail-on-empty"], run=_fake_gh([], {}))
    assert rc == 1
    # The output is still written, so a downstream step that reads it on failure
    # sees an explicit empty scope rather than an unset variable.
    assert output_file.read_text() == "repos=[]\n"
    assert "::error::No fleet repos discovered" in capsys.readouterr().err


def test_main_fail_on_empty_is_quiet_when_the_fleet_is_found(tmp_path, monkeypatch):
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    run = _fake_gh(
        ["atlanhq/atlan-mysql-app"],
        {"atlanhq/atlan-mysql-app": f'{{"extends": ["github>{MARKER}"]}}'},
    )
    assert doc.main(["--owner", "atlanhq", "--fail-on-empty"], run=run) == 0
    assert output_file.read_text() == 'repos=["atlanhq/atlan-mysql-app"]\n'


def test_main_writes_no_roster_at_all_when_a_read_fails(tmp_path, monkeypatch, capsys):
    """A read failure must produce NO `repos=` output, not a short one.

    Both consumers are safe against a missing output — the dashboard step's `||`
    falls through and the sweep refuses an absent roster — and neither can detect
    a roster that is merely incomplete. This holds without --fail-on-empty: the
    abort is not a variant of "the fleet is empty".
    """
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    run = _fake_gh(
        ["atlanhq/atlan-mysql-app", "atlanhq/atlan-postgres-app"],
        {"atlanhq/atlan-mysql-app": f'{{"extends": ["github>{MARKER}"]}}'},
        fails={"atlanhq/atlan-postgres-app": "HTTP 403: API rate limit exceeded"},
    )
    assert doc.main(["--owner", "atlanhq"], run=run) == 1
    assert output_file.read_text() == ""
    err = capsys.readouterr().err
    assert "::error::Fleet discovery failed" in err
    assert "rate limit" in err


# --- auto-merge mode ------------------------------------------------------
#
# The mode rides along on the renovate.json read membership already pays for. It
# exists so the dashboard can tell a repo that never arms GitHub-native
# auto-merge BY POLICY from one that failed to: without it, a green unarmed PR in
# a soft-rollout repo looks exactly like a Renovate arming fault, and the soft
# half of the fleet buries the real instances.

SOFT_CONFIG = json.dumps(
    {
        "extends": [f"github>{MARKER}"],
        "lockFileMaintenance": {"automerge": False, "platformAutomerge": False},
        "packageRules": [
            {
                "description": "Soft rollout: Renovate raises PRs but does not auto-merge.",
                "matchPackageNames": ["*"],
                "automerge": False,
                "platformAutomerge": False,
            }
        ],
    }
)
AUTO_CONFIG = json.dumps({"extends": [f"github>{MARKER}"]})


def test_automerge_mode_reads_the_soft_rollout_template_as_soft():
    assert doc.automerge_mode(SOFT_CONFIG) == "soft"


def test_automerge_mode_reads_a_bare_preset_extend_as_auto():
    assert doc.automerge_mode(AUTO_CONFIG) == "auto"


def test_automerge_mode_reads_a_top_level_optout_as_soft():
    assert doc.automerge_mode(json.dumps({"automerge": False})) == "soft"


def test_automerge_mode_reads_a_lockfile_only_optout_as_soft():
    # The soft template's other half, on its own: the lock lane's automerge comes
    # from the preset's lockFileMaintenance block, so an override there is a
    # blanket opt-out for it.
    config = json.dumps({"lockFileMaintenance": {"automerge": False}})
    assert doc.automerge_mode(config) == "soft"


def test_automerge_mode_treats_a_per_package_optout_as_auto():
    # Deliberately NOT soft: this repo still auto-merges every other lane, so its
    # unarmed PRs are worth reporting. Narrowing the verdict to the lane the PR
    # is actually in is a refinement this does not attempt.
    config = json.dumps(
        {
            "packageRules": [
                {"matchPackageNames": ["atlan-application-sdk"], "automerge": False}
            ]
        }
    )
    assert doc.automerge_mode(config) == "auto"


def test_automerge_mode_treats_a_matcherless_optout_as_soft():
    # No matchPackageNames at all means the rule applies to every package.
    config = json.dumps({"packageRules": [{"automerge": False}]})
    assert doc.automerge_mode(config) == "soft"


@pytest.mark.parametrize("bad", ["not json at all", "[1, 2, 3]", ""])
def test_automerge_mode_is_unknown_rather_than_a_guess(bad):
    # "unknown" classifies exactly as a repo did before this signal existed. A
    # guess either way would invent a fault or hide one.
    assert doc.automerge_mode(bad) == "unknown"


def test_discover_fleet_with_modes_reads_each_repo_once():
    # The mode must not cost a second read per repo: discovery is already the
    # dominant API cost at fleet scale (see the workflow's discovery step).
    reads = []
    base = _fake_gh(
        ["atlanhq/atlan-mysql-app", "atlanhq/atlan-soft-app"],
        {
            "atlanhq/atlan-mysql-app": AUTO_CONFIG,
            "atlanhq/atlan-soft-app": SOFT_CONFIG,
        },
    )

    def run(args: list) -> tuple:
        if args[0] == "api":
            reads.append(args[-1])
        return base(args)

    fleet, modes = doc.discover_fleet_with_modes(
        "atlanhq", doc.DEFAULT_NAME_PATTERN, MARKER, set(), run=run
    )
    assert fleet == ["atlanhq/atlan-mysql-app", "atlanhq/atlan-soft-app"]
    assert modes == {
        "atlanhq/atlan-mysql-app": "auto",
        "atlanhq/atlan-soft-app": "soft",
    }
    assert len(reads) == len(set(reads)) == 2


def test_main_writes_modes_file_when_asked(tmp_path, monkeypatch):
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    modes_file = tmp_path / "modes.json"

    run = _fake_gh(
        ["atlanhq/atlan-mysql-app", "atlanhq/atlan-soft-app"],
        {
            "atlanhq/atlan-mysql-app": AUTO_CONFIG,
            "atlanhq/atlan-soft-app": SOFT_CONFIG,
        },
    )
    rc = doc.main(["--owner", "atlanhq", "--modes-out", str(modes_file)], run=run)
    assert rc == 0
    assert json.loads(modes_file.read_text()) == {
        "atlanhq/atlan-mysql-app": "auto",
        "atlanhq/atlan-soft-app": "soft",
    }


def test_main_writes_an_empty_modes_file_when_the_fleet_is_empty(tmp_path, monkeypatch):
    # Written even when empty, like `repos=`: a consumer branching on the file's
    # presence cannot tell "found nothing" from "the run never got that far".
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    modes_file = tmp_path / "modes.json"

    run = _fake_gh(["atlanhq/atlan-legacy-app"], {})
    rc = doc.main(["--owner", "atlanhq", "--modes-out", str(modes_file)], run=run)
    assert rc == 0
    assert json.loads(modes_file.read_text()) == {}
