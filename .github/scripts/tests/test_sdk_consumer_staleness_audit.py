"""Tests for .github/scripts/sdk_consumer_staleness_audit.py.

`gh` is stubbed through the `run` seam, so no network calls are made.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import sdk_consumer_staleness_audit as audit

AS_OF = date(2026, 11, 4)
BOT_RE = re.compile(audit.DEFAULT_BOT_PATTERN, re.I)


# ----------------------------------------------------------------- matching


@pytest.mark.parametrize(
    "line,spec",
    [
        ('  "atlan-application-sdk[workflows]>=3.50.0",', "[workflows]>=3.50.0"),
        ('  "atlan_application_sdk==3.1.0",', "==3.1.0"),
        ('    "Atlan.Application.SDK>=1"', ">=1"),
        ("atlan-application-sdk==3.54.0  # pinned", "==3.54.0"),
        ('atlan-application-sdk = "^3.2"', "^3.2"),
    ],
)
def test_declaration_matches_requirement_forms(line, spec):
    m = audit.parse_declaration(
        "pyproject.toml", f"[project]\ndependencies = [\n{line}\n]\n"
    )
    assert m is not None
    assert m.kind == "direct"
    assert m.spec == spec


@pytest.mark.parametrize(
    "text",
    [
        '"atlan-application-sdk-conformance>=1.0"',
        '"atlan-application-sdk-api==3.0"',
        '"my-atlan-application-sdk>=1"',
        "# atlan-application-sdk>=3 is commented out",
        'name = "atlan-application-sdk"',
    ],
)
def test_declaration_ignores_sibling_packages_comments_and_own_name(text):
    assert audit.parse_declaration("pyproject.toml", text + "\n") is None


UV_LOCK_CONSUMER = """version = 1

[[package]]
name = "atlan-application-sdk-conformance"
version = "9.9.9"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "atlan-application-sdk"
version = "3.54.0"
source = { registry = "https://pypi.org/simple" }
"""

UV_LOCK_SELF = """version = 1

[[package]]
name = "atlan-application-sdk"
version = "3.54.0"
source = { editable = "." }
"""


def test_uv_lock_reports_locked_version_not_the_sibling_package():
    m = audit.match_manifest("uv.lock", UV_LOCK_CONSUMER)
    assert m is not None
    assert (m.kind, m.locked_version, m.path) == ("lock", "3.54.0", "uv.lock")


def test_uv_lock_of_the_sdk_itself_is_not_a_consumer():
    assert audit.match_manifest("uv.lock", UV_LOCK_SELF) is None


@pytest.mark.parametrize(
    "path,expected",
    [
        ("pyproject.toml", True),
        ("app/sub/pyproject.toml", True),
        ("uv.lock", True),
        ("requirements-dev.txt", True),
        ("requirements/base.txt", True),
        ("docs/notes.txt", False),
        ("setup.cfg", True),
        (".venv/lib/pyproject.toml", False),
        ("node_modules/x/setup.py", False),
        ("README.md", False),
    ],
)
def test_is_manifest_path(path, expected):
    assert audit.is_manifest_path(path) is expected


# ---------------------------------------------------------------- staleness


@pytest.mark.parametrize(
    "days,bucket",
    [
        (0, "<30"),
        (29, "<30"),
        (30, "30-60"),
        (59, "30-60"),
        (60, "60-90"),
        (89, "60-90"),
        (90, "90+"),
        (None, "90+"),
    ],
)
def test_bucket_boundaries(days, bucket):
    assert audit.bucket_for(days) == bucket


def _commit(
    date_s: str,
    login: Optional[str] = None,
    kind: str = "User",
    name: str = "Dev",
    message: str = "feat: work\n\nbody",
):
    return {
        "author": {"login": login, "type": kind} if login else None,
        "commit": {
            "message": message,
            "author": {"name": name, "date": date_s},
            "committer": {"date": date_s},
        },
    }


@pytest.mark.parametrize(
    "commit,is_bot",
    [
        (_commit("2026-11-01T00:00:00Z", "renovate[bot]", "Bot"), True),
        (_commit("2026-11-01T00:00:00Z", "someapp", "Bot"), True),
        (_commit("2026-11-01T00:00:00Z", None, name="github-actions"), True),
        (_commit("2026-11-01T00:00:00Z", None, name="dependabot[bot]"), True),
        (_commit("2026-11-01T00:00:00Z", "a-human"), False),
        (_commit("2026-11-01T00:00:00Z", None, name="A Human"), False),
    ],
)
def test_is_bot_commit(commit, is_bot):
    assert audit.is_bot_commit(commit, BOT_RE) is is_bot


REPO = audit.RepoInfo(
    name="atlan-x-app", archived=False, fork=False, default_branch="main"
)


def _commits_run(pages: list[list[dict]]):
    def run(args):
        m = re.search(r"[?&]page=(\d+)", args[-1])
        assert m, args
        i = int(m.group(1)) - 1
        return 0, json.dumps(pages[i] if i < len(pages) else []), ""

    return run


def test_last_commits_skips_bot_commits_to_find_the_human_one():
    run = _commits_run(
        [
            [
                _commit("2026-11-03T00:00:00Z", "renovate[bot]", "Bot"),
                _commit("2026-07-01T00:00:00Z", "dev"),
            ]
        ]
    )
    assert audit.last_commits("o", REPO, BOT_RE, run) == audit.CommitHistory(
        "2026-11-03T00:00:00Z", "2026-07-01T00:00:00Z", "feat: work", False
    )


def test_last_commits_reports_a_bound_when_the_page_cap_is_hit():
    bots = [
        _commit(f"2026-10-{d:02d}T00:00:00Z", "renovate[bot]", "Bot")
        for d in range(30, 0, -1)
    ]
    page = (bots * 4)[:100]
    run = _commits_run([page, page])
    h = audit.last_commits("o", REPO, BOT_RE, run, max_pages=2)
    assert h.last_commit == page[0]["commit"]["committer"]["date"]
    assert h.last_human_commit == page[-1]["commit"]["committer"]["date"]
    assert h.human_is_bound is True


def test_last_commits_no_human_commit_in_short_history():
    run = _commits_run([[_commit("2026-11-03T00:00:00Z", "renovate[bot]", "Bot")]])
    assert audit.last_commits("o", REPO, BOT_RE, run) == audit.CommitHistory(
        last_commit="2026-11-03T00:00:00Z"
    )


def test_last_commits_empty_repo_is_not_an_error():
    def run(args):
        return 1, "", "gh: Git Repository is empty. (HTTP 409)"

    assert audit.last_commits("o", REPO, BOT_RE, run) == audit.CommitHistory()


def test_last_commits_other_failures_raise():
    def run(args):
        return 1, "", "gh: Server Error (HTTP 502)"

    with pytest.raises(audit.AuditError):
        audit.last_commits("o", REPO, BOT_RE, run)


# ------------------------------------------------------------------ actions


def _row(**kw) -> audit.AuditRow:
    base = dict(
        repo="atlan-x-app",
        archived=False,
        fork=False,
        default_branch="main",
        consumption="direct",
        human_bucket="90+",
        days_since_human_commit=120,
        open_prs=0,
    )
    base.update(kw)
    return audit.AuditRow(**base)


@pytest.mark.parametrize(
    "kw,action",
    [
        ({"human_bucket": "<30"}, ""),
        ({"human_bucket": "30-60"}, ""),
        ({"archived": True}, "exclude from fleet tooling"),
        ({"fork": True}, "exclude from fleet tooling"),
        ({"open_prs": 9}, "archive"),  # stale bot PRs are not pending work
        ({"open_prs": 2, "active_own_prs": 1}, "keep"),
        ({"repo": "atlan-x-poc-app"}, "archive"),
        ({"fleet_preset": True}, "exclude from fleet tooling"),
        ({}, "archive"),
        ({"human_bucket": "60-90", "days_since_human_commit": 70}, "keep"),
    ],
)
def test_propose_action(kw, action):
    got, reason = audit.propose_action(_row(**kw))
    assert got == action
    assert bool(reason) is bool(action)


# -------------------------------------------------------------- end to end


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


OPEN_PRS = {
    "totalCount": 3,
    "nodes": [
        {
            "title": "fix: own work",
            "updatedAt": "2026-10-30T00:00:00Z",
            "author": {"__typename": "User", "login": "dev"},
        },
        {
            "title": "chore(deps): bump",
            "updatedAt": "2026-11-01T00:00:00Z",
            "author": {"__typename": "Bot", "login": "renovate"},
        },
        {
            "title": "feat: abandoned",
            "updatedAt": "2026-03-01T00:00:00Z",
            "author": {"__typename": "User", "login": "dev"},
        },
    ],
}


def test_open_prs_counts_only_recent_human_prs():
    def run(args):
        return 0, json.dumps({"data": {"repository": {"pullRequests": OPEN_PRS}}}), ""

    assert audit.open_prs("o", "r", AS_OF, BOT_RE, run) == (3, ["fix: own work"])


def test_mark_sweeps_discounts_org_wide_commits_and_prs():
    rows = [
        _row(
            repo=f"atlan-r{i}-app",
            last_human_subject=f"chore: remove scanner workflow (#{i + 3})",
            active_human_prs=[f"fix(deps): bump {i} deps to clear {i * 4} CVEs"]
            + (["fix: real"] if i == 0 else []),
        )
        for i in range(5)
    ]
    rows.append(
        _row(
            repo="atlan-solo-app",
            last_human_subject="feat: solo",
            active_human_prs=["fix(deps): bump 9 deps to clear 40 CVEs"],
        )
    )
    sweeps = audit.mark_sweeps(rows)
    assert sweeps == {
        "chore: remove scanner workflow",
        "fix(deps): bump N deps to clear N cves",
    }
    assert [r.human_commit_is_sweep for r in rows] == [True] * 5 + [False]
    assert [r.active_own_prs for r in rows] == [1, 0, 0, 0, 0, 0]
    assert rows[0].proposed_action == "keep"
    assert rows[1].proposed_action == "archive"
    assert "org-wide sweep" in rows[1].action_reason


def _fake_org(fail_tree_for: Optional[str] = None):
    repo_list = [
        {
            "name": "atlan-live-app",
            "isArchived": False,
            "isFork": False,
            "defaultBranchRef": {"name": "main"},
        },
        {
            "name": "atlan-dead-app",
            "isArchived": True,
            "isFork": False,
            "defaultBranchRef": {"name": "master"},
        },
        {
            "name": "unrelated",
            "isArchived": False,
            "isFork": False,
            "defaultBranchRef": {"name": "main"},
        },
        {
            "name": "empty",
            "isArchived": False,
            "isFork": False,
            "defaultBranchRef": None,
        },
        {
            "name": "application-sdk",
            "isArchived": False,
            "isFork": False,
            "defaultBranchRef": {"name": "main"},
        },
    ]
    trees = {
        "atlan-live-app": [
            ("pyproject.toml", "p1"),
            ("uv.lock", "l1"),
            ("renovate.json", "r1"),
        ],
        "atlan-dead-app": [("requirements.txt", "q1")],
        "unrelated": [("pyproject.toml", "p2")],
        "application-sdk": [("pyproject.toml", "p3")],
    }
    blobs = {
        "p1": '[project]\ndependencies = ["atlan-application-sdk[workflows]>=3.50"]\n',
        "l1": UV_LOCK_CONSUMER,
        "q1": "atlan-application-sdk==0.1.0\n",
        "p2": '[project]\ndependencies = ["atlan-application-sdk-conformance"]\n',
        "p3": '[project]\nname = "atlan-application-sdk"\n',
    }
    commits = {
        "atlan-live-app": [
            _commit("2026-11-03T00:00:00Z", "atlan-app-fleet[bot]", "Bot"),
            _commit("2026-10-20T00:00:00Z", "dev"),
        ],
        "atlan-dead-app": [_commit("2026-06-01T00:00:00Z", "dev")],
    }
    calls: list[list[str]] = []

    def run(args):
        calls.append(args)
        if args[:2] == ["repo", "list"]:
            return 0, json.dumps(repo_list), ""
        if args[:2] == ["api", "graphql"]:
            return (
                0,
                json.dumps({"data": {"repository": {"pullRequests": OPEN_PRS}}}),
                "",
            )
        path = args[1]
        repo = path.split("/")[2]
        if "/git/trees/" in path:
            if repo == fail_tree_for:
                return 1, "", "gh: Server Error (HTTP 500)"
            tree = [{"path": p, "sha": s, "type": "blob"} for p, s in trees[repo]]
            return 0, json.dumps({"tree": tree, "truncated": False}), ""
        if "/git/blobs/" in path:
            return 0, json.dumps({"content": _b64(blobs[path.rsplit("/", 1)[-1]])}), ""
        if "/commits?" in path:
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            return 0, json.dumps(commits[repo] if page == 1 else []), ""
        if path.endswith("/contents/renovate.json"):
            return 0, json.dumps({"extends": [audit.FLEET_PRESET_MARKER]}), ""
        raise AssertionError(f"unexpected gh call: {args}")

    return run, calls


def test_run_audit_end_to_end():
    run, _ = _fake_org()
    rows, scanned = audit.run_audit("o", AS_OF, {"application-sdk"}, workers=1, run=run)
    assert scanned == 4
    by = {r.repo: r for r in rows}
    assert set(by) == {"atlan-live-app", "atlan-dead-app"}

    live = by["atlan-live-app"]
    assert live.consumption == "direct"
    assert live.sdk_spec == "[workflows]>=3.50"
    assert live.sdk_locked == "3.54.0"
    assert (live.bucket, live.human_bucket) == ("<30", "<30")
    assert live.fleet_preset is True
    assert live.proposed_action == ""

    dead = by["atlan-dead-app"]
    assert dead.archived is True
    assert dead.days_since_commit == 156
    assert (dead.bucket, dead.human_bucket) == ("90+", "90+")
    assert dead.proposed_action == "exclude from fleet tooling"

    # stalest first
    assert [r.repo for r in rows] == ["atlan-dead-app", "atlan-live-app"]


def test_run_audit_never_reads_beyond_get():
    run, calls = _fake_org()
    audit.run_audit("o", AS_OF, set(), workers=1, run=run)
    for args in calls:
        assert "-X" not in args and "--method" not in args, args
        if args[:2] == ["api", "graphql"]:
            assert args[3].startswith("query=query("), args


def test_run_audit_fails_loudly_on_unreadable_repo():
    run, _ = _fake_org(fail_tree_for="atlan-live-app")
    with pytest.raises(audit.AuditError):
        audit.run_audit("o", AS_OF, set(), workers=1, run=run)


def test_main_writes_reports_and_fails_on_search_miss(tmp_path):
    run, _ = _fake_org()

    def run_with_search(args):
        if args[:2] == ["search", "code"]:
            hits = [{"repository": {"nameWithOwner": "o/atlan-hidden-app"}}]
            return 0, json.dumps(hits if "pyproject.toml" in args else []), ""
        return run(args)

    out_json, out_md = tmp_path / "a.json", tmp_path / "a.md"
    argv = [
        "--owner",
        "o",
        "--as-of",
        "2026-11-04",
        "--workers",
        "1",
        "--out-json",
        str(out_json),
        "--out-md",
        str(out_md),
    ]
    assert audit.main(argv, run=run) == 0
    assert audit.main([*argv, "--cross-check-search"], run=run_with_search) == 1

    data = json.loads(out_json.read_text())
    assert data["as_of"] == "2026-11-04"
    assert len(data["rows"]) == 2
    md = out_md.read_text()
    assert "| 90+ | 1 | 1 |" in md
    assert "atlan-dead-app" in md


def test_retrying_run_sleeps_until_reset_then_retries():
    answers = [(1, "", "API rate limit exceeded"), (0, "ok", "")]
    slept: list[float] = []

    def run(args):
        if args == ["api", "rate_limit"]:
            return (
                0,
                json.dumps({"resources": {"core": {"remaining": 5, "reset": 0}}}),
                "",
            )
        return answers.pop(0)

    wrapped = audit.make_retrying_run(run, sleep=slept.append)
    assert wrapped(["api", "x"]) == (0, "ok", "")
    assert slept == [60.0]


def test_retrying_run_passes_other_errors_through():
    def run(args):
        return 1, "", "gh: Not Found (HTTP 404)"

    wrapped = audit.make_retrying_run(run, sleep=lambda s: pytest.fail("slept"))
    assert wrapped(["api", "x"]) == (1, "", "gh: Not Found (HTTP 404)")
