"""Tests for .github/scripts/renovate_fleet_scan.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import renovate_fleet_scan as rfs

# ---------------------------------------------------------------------------
# Scope resolution + query building
# ---------------------------------------------------------------------------


def test_resolve_scope_full_fleet_when_no_repo():
    assert rfs.resolve_scope("atlanhq", None) == "org:atlanhq"


def test_resolve_scope_single_repo_when_repo_set():
    assert (
        rfs.resolve_scope("atlanhq", "atlanhq/atlan-mysql-app")
        == "repo:atlanhq/atlan-mysql-app"
    )


def test_build_search_query_full_fleet():
    q = rfs.build_search_query("org:atlanhq", "is:open")
    assert (
        q == "org:atlanhq is:pr author:app/renovate author:app/atlan-app-fleet is:open"
    )


def test_build_search_query_single_repo():
    q = rfs.build_search_query(
        "repo:atlanhq/atlan-mysql-app", "is:merged merged:>=2026-06-01"
    )
    assert q == (
        "repo:atlanhq/atlan-mysql-app is:pr "
        "author:app/renovate author:app/atlan-app-fleet "
        "is:merged merged:>=2026-06-01"
    )


def test_build_graphql_payload_first_page_has_no_after_arg():
    payload = rfs.build_graphql_payload("org:atlanhq is:pr", "number", after=None)
    assert "after:" not in payload["query"]
    assert '"org:atlanhq is:pr"' in payload["query"]


def test_build_graphql_payload_includes_after_cursor():
    payload = rfs.build_graphql_payload(
        "org:atlanhq is:pr", "number", after="CURSOR123"
    )
    assert 'after: "CURSOR123"' in payload["query"]


def test_build_graphql_payload_escapes_quotes_in_query_string():
    payload = rfs.build_graphql_payload(
        'org:atlanhq is:pr "weird"', "number", after=None
    )
    # json.dumps must escape the embedded quotes so the GraphQL string literal stays valid.
    assert '\\"weird\\"' in payload["query"]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _page(nodes, has_next, cursor=None, issue_count=None):
    return {
        "data": {
            "search": {
                "issueCount": len(nodes) if issue_count is None else issue_count,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }


def test_fetch_all_prs_single_page():
    calls = []

    def fake_post(token, payload):
        calls.append(payload)
        return _page([{"number": 1}, {"number": 2}], has_next=False)

    result = rfs.fetch_all_prs("tok", "org:atlanhq is:pr", "number", post=fake_post)
    assert result == [{"number": 1}, {"number": 2}]
    assert len(calls) == 1


def test_fetch_all_prs_paginates_until_exhausted():
    pages = [
        _page([{"number": 1}], has_next=True, cursor="c1", issue_count=3),
        _page([{"number": 2}], has_next=True, cursor="c2", issue_count=3),
        _page([{"number": 3}], has_next=False, issue_count=3),
    ]

    def fake_post(token, payload):
        return pages.pop(0)

    result = rfs.fetch_all_prs("tok", "org:atlanhq is:pr", "number", post=fake_post)
    assert [pr["number"] for pr in result] == [1, 2, 3]


def test_fetch_all_prs_raises_on_graphql_errors():
    def fake_post(token, payload):
        return {"errors": [{"message": "boom"}]}

    try:
        rfs.fetch_all_prs("tok", "org:atlanhq is:pr", "number", post=fake_post)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "boom" in str(exc)


def test_fetch_all_prs_trips_safety_backstop():
    def fake_post(token, payload):
        return _page([{"number": 1}], has_next=True, cursor="same")

    try:
        rfs.fetch_all_prs("tok", "org:atlanhq is:pr", "number", post=fake_post)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "safety backstop" in str(exc)


def test_fetch_all_prs_raises_when_search_api_cap_truncates_results():
    # The search API caps total matches (commonly ~1000) — pageInfo can report
    # hasNextPage=False while issueCount still exceeds what was actually returned.
    # This must fail loudly rather than silently reporting an incomplete dashboard.
    def fake_post(token, payload):
        return _page([{"number": 1}], has_next=False, issue_count=1500)

    try:
        rfs.fetch_all_prs("tok", "org:atlanhq is:pr", "number", post=fake_post)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "1500" in str(exc)
        assert "cap" in str(exc).lower()


def test_fetch_all_prs_no_error_when_issue_count_matches_returned_nodes():
    def fake_post(token, payload):
        return _page([{"number": 1}, {"number": 2}], has_next=False, issue_count=2)

    result = rfs.fetch_all_prs("tok", "org:atlanhq is:pr", "number", post=fake_post)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# statusCheckRollup mapping — must match conformance.renovate.scan._parse_checks_state's
# two expected item shapes: {"state": ...} (StatusContext) and
# {"conclusion": ..., "status": ...} (CheckRun, state omitted).
# ---------------------------------------------------------------------------


def _pr_with_contexts(context_nodes):
    return {
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {"contexts": {"nodes": context_nodes}}
                    }
                }
            ]
        }
    }


def test_status_rollup_maps_status_context():
    pr = _pr_with_contexts([{"__typename": "StatusContext", "state": "SUCCESS"}])
    assert rfs._status_rollup_to_list(pr) == [{"state": "SUCCESS"}]


def test_status_rollup_maps_check_run():
    pr = _pr_with_contexts(
        [{"__typename": "CheckRun", "conclusion": "FAILURE", "status": "COMPLETED"}]
    )
    assert rfs._status_rollup_to_list(pr) == [
        {"conclusion": "FAILURE", "status": "COMPLETED"}
    ]


def test_status_rollup_empty_when_no_commits():
    assert rfs._status_rollup_to_list({"commits": {"nodes": []}}) == []


def test_status_rollup_empty_when_rollup_missing():
    pr = {"commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}}
    assert rfs._status_rollup_to_list(pr) == []


# ---------------------------------------------------------------------------
# Normalization — output shape must match what `gh pr list --json ...` would have
# produced, since conformance.renovate.scan._parse_pr / _auto_merge_stats read it.
# ---------------------------------------------------------------------------


def test_normalize_open_pr_full_shape():
    pr = {
        "number": 42,
        "url": "https://github.com/atlanhq/atlan-mysql-app/pull/42",
        "title": "Update foo to v2",
        "headRefName": "renovate/foo-2.x",
        "labels": {"nodes": [{"name": "update:minor"}]},
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "autoMergeRequest": {"enabledAt": "2026-06-01T01:00:00Z"},
        "files": {"nodes": [{"path": "uv.lock"}]},
        "createdAt": "2026-06-01T00:00:00Z",
        "updatedAt": "2026-06-02T00:00:00Z",
        "isDraft": False,
        "body": "bumps foo",
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "committedDate": "2026-06-02T00:00:00Z",
                        "statusCheckRollup": None,
                    }
                }
            ]
        },
    }
    out = rfs.normalize_open_pr(pr)
    assert out == {
        "number": 42,
        "url": "https://github.com/atlanhq/atlan-mysql-app/pull/42",
        "title": "Update foo to v2",
        "headRefName": "renovate/foo-2.x",
        "labels": [{"name": "update:minor"}],
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "autoMergeEnabled": True,
        "statusCheckRollup": [],
        "files": [{"path": "uv.lock"}],
        "createdAt": "2026-06-01T00:00:00Z",
        "updatedAt": "2026-06-02T00:00:00Z",
        "isDraft": False,
        "body": "bumps foo",
        "headCommittedAt": "2026-06-02T00:00:00Z",
        "uvLockText": "",
    }


def test_normalize_open_pr_defaults_for_missing_optional_fields():
    pr = {
        "number": 1,
        "url": "https://x/1",
        "title": "t",
        "createdAt": "2026-06-01T00:00:00Z",
        "updatedAt": "2026-06-01T00:00:00Z",
    }
    out = rfs.normalize_open_pr(pr)
    assert out["headRefName"] == ""
    assert out["labels"] == []
    assert out["files"] == []
    assert out["mergeable"] == "UNKNOWN"
    assert out["statusCheckRollup"] == []
    assert out["isDraft"] is False
    assert out["body"] == ""
    # No autoMergeRequest in the node → auto-merge is not armed.
    assert out["autoMergeEnabled"] is False
    # Never emit a bare None for fields conformance.renovate.scan reads with two-arg
    # dict.get(key, default) — an explicit null would bypass that default.
    for key in (
        "headRefName",
        "labels",
        "files",
        "mergeable",
        "isDraft",
        "body",
        "statusCheckRollup",
        "autoMergeEnabled",
        "headCommittedAt",
        "uvLockText",
    ):
        assert out[key] is not None


def test_normalize_merged_pr_maps_reviews():
    pr = {
        "reviews": {
            "nodes": [
                {
                    "state": "APPROVED",
                    "body": "**Renovate auto-approval:** ...",
                    "author": {"login": "atlan-ci"},
                },
            ]
        }
    }
    out = rfs.normalize_merged_pr(pr)
    assert out == {
        "reviews": [
            {
                "state": "APPROVED",
                "body": "**Renovate auto-approval:** ...",
                "author": {"login": "atlan-ci"},
            },
        ]
    }


def test_normalize_merged_pr_handles_missing_author():
    pr = {"reviews": {"nodes": [{"state": "COMMENTED", "body": None}]}}
    out = rfs.normalize_merged_pr(pr)
    assert out["reviews"][0]["author"] == {"login": None}


# ---------------------------------------------------------------------------
# Grouping + file writing
# ---------------------------------------------------------------------------


def test_group_by_repo():
    prs = [
        {"repository": {"nameWithOwner": "atlanhq/a"}, "number": 1},
        {"repository": {"nameWithOwner": "atlanhq/b"}, "number": 2},
        {"repository": {"nameWithOwner": "atlanhq/a"}, "number": 3},
    ]
    grouped = rfs.group_by_repo(prs, lambda pr: {"n": pr["number"]})
    assert grouped == {"atlanhq/a": [{"n": 1}, {"n": 3}], "atlanhq/b": [{"n": 2}]}


def test_slug_for():
    assert rfs.slug_for("atlanhq/atlan-mysql-app") == "atlanhq_atlan-mysql-app"


def test_write_repo_files_writes_empty_list_for_known_repo_with_no_prs(tmp_path):
    rfs.write_repo_files({}, tmp_path, known_repos=["atlanhq/quiet-repo"])
    written = json.loads((tmp_path / "atlanhq_quiet-repo.json").read_text())
    assert written == []


def test_write_repo_files_writes_grouped_data(tmp_path):
    grouped = {"atlanhq/busy-repo": [{"number": 1}]}
    rfs.write_repo_files(grouped, tmp_path, known_repos=["atlanhq/busy-repo"])
    written = json.loads((tmp_path / "atlanhq_busy-repo.json").read_text())
    assert written == [{"number": 1}]


def test_write_repo_files_includes_repos_not_in_known_list_too(tmp_path):
    # A repo that has PRs but wasn't in the discovery list should still get written —
    # known_repos only guarantees a floor of `[]` files, never excludes real data.
    grouped = {"atlanhq/surprise-repo": [{"number": 7}]}
    rfs.write_repo_files(grouped, tmp_path, known_repos=[])
    written = json.loads((tmp_path / "atlanhq_surprise-repo.json").read_text())
    assert written == [{"number": 7}]


# ---------------------------------------------------------------------------
# End-to-end `run()` with a fake transport
# ---------------------------------------------------------------------------


def test_run_writes_open_and_merged_files(tmp_path):
    open_dir = tmp_path / "open"
    merged_dir = tmp_path / "merged"

    def fake_post(token, payload):
        query = payload["query"]
        if "is:open" in query:
            return _page(
                [
                    {
                        "number": 1,
                        "url": "https://x/1",
                        "title": "t",
                        "createdAt": "2026-06-01T00:00:00Z",
                        "updatedAt": "2026-06-01T00:00:00Z",
                        "repository": {"nameWithOwner": "atlanhq/a"},
                    }
                ],
                has_next=False,
            )
        return _page(
            [
                {
                    "url": "https://x/2",
                    "repository": {"nameWithOwner": "atlanhq/a"},
                    "reviews": {"nodes": []},
                }
            ],
            has_next=False,
        )

    rfs.run(
        scope="org:atlanhq",
        since="2026-06-01",
        open_dir=open_dir,
        merged_dir=merged_dir,
        known_repos=["atlanhq/a", "atlanhq/b"],
        token="tok",
        post=fake_post,
    )

    assert json.loads((open_dir / "atlanhq_a.json").read_text())[0]["number"] == 1
    assert json.loads((open_dir / "atlanhq_b.json").read_text()) == []
    assert json.loads((merged_dir / "atlanhq_a.json").read_text()) == [{"reviews": []}]
    assert json.loads((merged_dir / "atlanhq_b.json").read_text()) == []


# ---------------------------------------------------------------------------
# Lock-refusal candidate pre-filter + blob fetch (FND-782)
# ---------------------------------------------------------------------------


def _candidate(
    *,
    conclusion="FAILURE",
    paths=("uv.lock",),
    url="https://github.com/atlanhq/atlan-mysql-app/pull/7",
    branch="renovate/lock-file-maintenance",
):
    return {
        "url": url,
        "headRefName": branch,
        "repository": {"nameWithOwner": "atlanhq/atlan-mysql-app"},
        "files": {"nodes": [{"path": p} for p in paths]},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "committedDate": "2026-08-20T00:00:00Z",
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "conclusion": conclusion,
                                        "status": "COMPLETED",
                                    }
                                ]
                            }
                        },
                    }
                }
            ]
        },
    }


def test_head_committed_at_read_from_last_commit():
    assert rfs._head_committed_at(_candidate()) == "2026-08-20T00:00:00Z"


def test_head_committed_at_empty_when_no_commits():
    assert rfs._head_committed_at({"commits": {"nodes": []}}) == ""


def test_lock_refusal_candidate_matches_red_lock_only_pr():
    assert rfs.lock_refusal_candidate(_candidate()) == "uv.lock"


def test_lock_refusal_candidate_matches_a_nested_lock():
    assert rfs.lock_refusal_candidate(_candidate(paths=("apps/api/uv.lock",))) == (
        "apps/api/uv.lock"
    )


def test_lock_refusal_candidate_skips_green_prs():
    # The overwhelming majority of lock PRs. Fetching their locks would be the
    # whole cost of this feature for none of the signal.
    assert rfs.lock_refusal_candidate(_candidate(conclusion="SUCCESS")) is None


def test_lock_refusal_candidate_skips_multi_file_diffs():
    assert (
        rfs.lock_refusal_candidate(_candidate(paths=("uv.lock", "pyproject.toml")))
        is None
    )


def test_lock_refusal_candidate_skips_non_lock_diffs():
    assert rfs.lock_refusal_candidate(_candidate(paths=("package-lock.json",))) is None


def test_fetch_lock_texts_attaches_only_to_candidates():
    prs = [_candidate(), _candidate(conclusion="SUCCESS", url="https://x/2")]
    calls = []

    def fake_post(token, payload):
        calls.append(payload["variables"])
        return {"data": {"repository": {"object": {"text": "[options]\n"}}}}

    assert rfs.fetch_lock_texts("tok", prs, fake_post) == 1
    assert calls == [
        {
            "owner": "atlanhq",
            "name": "atlan-mysql-app",
            "expression": "renovate/lock-file-maintenance:uv.lock",
        }
    ]
    assert prs[0]["uvLockText"] == "[options]\n"
    assert "uvLockText" not in prs[1]


def test_fetch_lock_texts_survives_an_unreadable_blob(capsys):
    # Deleted branch / permissions / an unexpected object type. The PR then
    # classifies exactly as it did before this signal existed.
    prs = [_candidate()]

    def fake_post(token, payload):
        return {"errors": [{"message": "Could not resolve to a Repository"}]}

    assert rfs.fetch_lock_texts("tok", prs, fake_post) == 0
    assert "uvLockText" not in prs[0]
    assert "could not read uv.lock" in capsys.readouterr().err


def test_fetch_lock_texts_skips_a_null_object():
    prs = [_candidate()]

    def fake_post(token, payload):
        return {"data": {"repository": {"object": None}}}

    assert rfs.fetch_lock_texts("tok", prs, fake_post) == 0
    assert "uvLockText" not in prs[0]


def test_fetch_lock_texts_caps_fetches_and_reports_what_it_skipped(monkeypatch, capsys):
    monkeypatch.setattr(rfs, "MAX_LOCK_FETCHES", 2)
    prs = [_candidate(url=f"https://x/{i}") for i in range(4)]

    def fake_post(token, payload):
        return {"data": {"repository": {"object": {"text": "x"}}}}

    assert rfs.fetch_lock_texts("tok", prs, fake_post) == 2
    err = capsys.readouterr().err
    # No silent truncation: the ones that were dropped are named.
    assert "https://x/2" in err and "https://x/3" in err


def test_open_pr_fields_request_the_head_commit_date():
    # The clock the refusal signal expires against rides on a selection the query
    # already makes; a rename upstream would silently disable the signal.
    assert "committedDate" in rfs._OPEN_PR_FIELDS


def test_post_graphql_wraps_transport_errors_as_runtime_error(monkeypatch):
    # urlopen raises URLError for DNS/connect failures. Left unwrapped it escapes
    # fetch_lock_texts' handler and one flaky blob fetch aborts the whole
    # dashboard update before any repo file is written.
    def boom(req, timeout=None):
        raise rfs.urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(rfs.urllib.request, "urlopen", boom)
    try:
        rfs._post_graphql("tok", {"query": "{}"})
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "name resolution failed" in str(exc)


def test_post_graphql_wraps_timeouts_as_runtime_error(monkeypatch):
    # urlopen raises TimeoutError directly rather than wrapping it in URLError,
    # so it needs naming separately.
    def boom(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(rfs.urllib.request, "urlopen", boom)
    try:
        rfs._post_graphql("tok", {"query": "{}"})
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "timed out" in str(exc)


def test_fetch_lock_texts_survives_a_transport_failure(capsys):
    # The whole point of the wrap: an enrichment pass degrades to "no signal for
    # this PR", never to a dashboard that skipped every repo.
    prs = [_candidate()]

    def flaky_post(token, payload):
        raise RuntimeError("GraphQL request failed: <urlopen error timed out>")

    assert rfs.fetch_lock_texts("tok", prs, flaky_post) == 0
    assert "uvLockText" not in prs[0]
    assert "could not read uv.lock" in capsys.readouterr().err
