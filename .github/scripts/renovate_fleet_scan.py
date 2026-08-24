#!/usr/bin/env python3
"""Fetch open + recently-merged Renovate PRs across the fleet via GraphQL search.

Replaces the O(repos x 2) `gh pr list` REST loop in renovate-dashboard.yaml with a
constant-ish number of paginated GraphQL search queries (one search covers every repo
in scope at once), so the workflow's GitHub API call count no longer scales with fleet
size. Output is written in the exact per-repo JSON file layout that
`conformance renovate-scan --input/--merged` already expects — see
packages/conformance/conformance/renovate/scan.py `_parse_pr` / `_auto_merge_stats` for
the consuming schema this script must match.

One follow-up query per PR is issued after the search, and only for PRs that are red
with a uv.lock-only diff: it reads that lock so the classifier can distinguish a stale
bounded-lock refusal from an ordinary broken build (FND-782). See fetch_lock_texts.

Environment:
    GH_TOKEN   bearer token (GitHub App installation token or PAT) for api.github.com

Usage:
    renovate_fleet_scan.py --org atlanhq --since 2026-06-06 \\
        --open-dir /tmp/renovate-input/open --merged-dir /tmp/renovate-input/merged \\
        --known-repos-file /tmp/repos.json

    renovate_fleet_scan.py --org atlanhq --repo atlanhq/atlan-mysql-app --since 2026-06-06 \\
        --open-dir /tmp/renovate-input/open --merged-dir /tmp/renovate-input/merged \\
        --known-repos-file /tmp/repos.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

GRAPHQL_URL = "https://api.github.com/graphql"
PAGE_SIZE = 100
# Safety backstop, not a real ceiling: 50 pages x 100 = 5000 PRs in one search window,
# far beyond any realistic fleet. Trips only if a query is unexpectedly unbounded.
MAX_PAGES = 50

# Ceiling on the follow-up uv.lock blob fetches (see fetch_lock_texts). Each one
# pulls a few hundred KB, so this is a cost guard, not a correctness bound — the
# pre-filter it backs up should leave a handful of candidates fleet-wide, and
# anything over the cap is reported rather than silently dropped.
MAX_LOCK_FETCHES = 25

_OPEN_PR_FIELDS = """
number
url
title
createdAt
updatedAt
headRefName
isDraft
body
mergeable
reviewDecision
autoMergeRequest { enabledAt }
repository { nameWithOwner }
labels(first: 20) { nodes { name } }
files(first: 100) { nodes { path } }
commits(last: 1) {
  nodes {
    commit {
      committedDate
      statusCheckRollup {
        contexts(first: 100) {
          nodes {
            __typename
            ... on StatusContext { state }
            ... on CheckRun { conclusion status }
          }
        }
      }
    }
  }
}
"""

_MERGED_PR_FIELDS = """
url
repository { nameWithOwner }
reviews(first: 50) {
  nodes {
    state
    body
    author { login }
  }
}
"""

PostFn = Callable[[str, dict], dict]


def resolve_scope(org: str, repo: Optional[str]) -> str:
    """Single-repo mode (repo set) scopes to that repo; otherwise scope to the whole org."""
    return f"repo:{repo}" if repo else f"org:{org}"


# Renovate PR authors to scan. The self-hosted runner (app/atlan-app-fleet)
# covers the app fleet; application-sdk itself still uses the Mend-hosted app
# (app/renovate) for its own workflow-action updates, so keep both. GitHub PR
# search OR's multiple author: qualifiers.
RENOVATE_PR_AUTHORS = ("app/renovate", "app/atlan-app-fleet")


def build_search_query(scope: str, extra: str) -> str:
    """Build a GitHub search-syntax query, e.g.
    'org:atlanhq is:pr author:app/renovate author:app/atlan-app-fleet is:open'."""
    authors = " ".join(f"author:{a}" for a in RENOVATE_PR_AUTHORS)
    return f"{scope} is:pr {authors} {extra}".strip()


def build_graphql_payload(search_query: str, fields: str, after: Optional[str]) -> dict:
    after_arg = f", after: {json.dumps(after)}" if after else ""
    query = f"""
    query {{
      search(query: {json.dumps(search_query)}, type: ISSUE, first: {PAGE_SIZE}{after_arg}) {{
        issueCount
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          ... on PullRequest {{
            {fields}
          }}
        }}
      }}
    }}
    """
    return {"query": query}


def _post_graphql(token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"GraphQL request failed: {exc.code} {exc.reason}: {body}"
        ) from exc


def fetch_all_prs(
    token: str,
    search_query: str,
    fields: str,
    post: PostFn = _post_graphql,
) -> list[dict]:
    """Paginate a GraphQL PR search to completion. Raises on a GraphQL 'errors' response,
    or if the result was silently truncated by the search API's ~1000-item hard cap."""
    nodes: list[dict] = []
    issue_count: Optional[int] = None
    after: Optional[str] = None
    for _ in range(MAX_PAGES):
        payload = build_graphql_payload(search_query, fields, after)
        data = post(token, payload)
        if "errors" in data:
            raise RuntimeError(
                f"GraphQL errors for query {search_query!r}: {data['errors']}"
            )
        search = data["data"]["search"]
        if issue_count is None:
            issue_count = search["issueCount"]
        nodes.extend(search["nodes"])
        page_info = search["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    else:
        raise RuntimeError(
            f"exceeded {MAX_PAGES} pages for query {search_query!r} "
            "— fleet larger than the safety backstop expects"
        )

    if issue_count is not None and len(nodes) < issue_count:
        raise RuntimeError(
            f"query {search_query!r} matched {issue_count} results but only "
            f"{len(nodes)} were returned — the search API's result cap likely "
            "truncated this query. Narrow it (e.g. split by date range) rather "
            "than silently reporting incomplete dashboard data."
        )
    return nodes


def _status_rollup_to_list(pr: dict) -> list[dict]:
    """Flatten a PullRequest node's last-commit statusCheckRollup to the shape
    `conformance.renovate.scan._parse_checks_state` expects: a list of either
    {"state": ...} (StatusContext) or {"conclusion": ..., "status": ...} (CheckRun) dicts.
    """
    commits = (pr.get("commits") or {}).get("nodes") or []
    if not commits:
        return []
    rollup = ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup")
    if not rollup:
        return []
    out: list[dict] = []
    for ctx in (rollup.get("contexts") or {}).get("nodes") or []:
        if ctx.get("__typename") == "StatusContext":
            out.append({"state": ctx.get("state")})
        else:  # CheckRun
            out.append(
                {"conclusion": ctx.get("conclusion"), "status": ctx.get("status")}
            )
    return out


# Conclusions/states that make a check red. Mirrors
# conformance.renovate.scan._CHECKS_FAILING — duplicated rather than imported
# because .github/scripts/ runs on a bare interpreter with no conformance package
# installed. Only used as a fetch pre-filter here; the authoritative reduction
# still happens in the classifier.
_FAILING_CHECK_STATES = frozenset(
    {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
)

_LOCK_BLOB_QUERY = """
query($owner: String!, $name: String!, $expression: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $expression) {
      ... on Blob { text }
    }
  }
}
"""


def _head_committed_at(pr: dict) -> str:
    commits = (pr.get("commits") or {}).get("nodes") or []
    if not commits:
        return ""
    return ((commits[0] or {}).get("commit") or {}).get("committedDate") or ""


def _has_failing_check(pr: dict) -> bool:
    return any(
        (c.get("state") or c.get("conclusion") or c.get("status") or "").upper()
        in _FAILING_CHECK_STATES
        for c in _status_rollup_to_list(pr)
    )


def lock_refusal_candidate(pr: dict) -> Optional[str]:
    """Path of the uv.lock worth fetching for this PR, or None.

    The bounded-lock refusal signal (FND-782) needs the branch's lock *contents*,
    which no PR search field carries. Fetching one blob per open Renovate PR would
    move hundreds of MB across the fleet for a condition that should be rare, so
    narrow it first to the only shape that can possibly be a refusal: a red PR
    whose entire diff is a single uv.lock. Both facts are already in hand from the
    search response, and both are conditions the classifier independently requires
    anyway — this is a pre-filter, not a second copy of the classification.
    """
    if not _has_failing_check(pr):
        return None
    paths = [f["path"] for f in (pr.get("files") or {}).get("nodes") or []]
    if len(paths) != 1 or paths[0].rsplit("/", 1)[-1] != "uv.lock":
        return None
    return paths[0]


def fetch_lock_texts(token: str, prs: list[dict], post: PostFn = _post_graphql) -> int:
    """Attach ``uvLockText`` to every PR that could be carrying a lock refusal.

    Mutates the nodes in place and returns how many were fetched. A blob that
    cannot be read (deleted branch, permissions, an unexpected object type) is
    warned about and skipped: the PR then classifies exactly as it did before this
    signal existed, which is the safe direction to fail.
    """
    candidates = [(pr, path) for pr in prs if (path := lock_refusal_candidate(pr))]
    if len(candidates) > MAX_LOCK_FETCHES:
        skipped = [pr["url"] for pr, _ in candidates[MAX_LOCK_FETCHES:]]
        print(
            f"Warning: {len(candidates)} lock-refusal candidates exceeds the "
            f"{MAX_LOCK_FETCHES}-fetch cap; not inspecting the lock for: "
            f"{', '.join(skipped)}",
            file=sys.stderr,
        )
        candidates = candidates[:MAX_LOCK_FETCHES]

    fetched = 0
    for pr, path in candidates:
        owner, _, name = pr["repository"]["nameWithOwner"].partition("/")
        payload = {
            "query": _LOCK_BLOB_QUERY,
            "variables": {
                "owner": owner,
                "name": name,
                "expression": f"{pr.get('headRefName') or ''}:{path}",
            },
        }
        try:
            data = post(token, payload)
            if "errors" in data:
                raise RuntimeError(str(data["errors"]))
            obj = ((data["data"] or {}).get("repository") or {}).get("object") or {}
            text = obj.get("text")
        except (RuntimeError, KeyError, TypeError) as exc:
            print(
                f"Warning: could not read {path} on {pr['url']}: {exc}",
                file=sys.stderr,
            )
            continue
        if text:
            pr["uvLockText"] = text
            fetched += 1
    return fetched


def normalize_open_pr(pr: dict) -> dict:
    """Map a GraphQL PullRequest node to the `gh pr list --json ...` shape renovate-scan expects."""
    return {
        "number": pr["number"],
        "url": pr["url"],
        "title": pr["title"],
        "headRefName": pr.get("headRefName") or "",
        "labels": [
            {"name": lb["name"]} for lb in (pr.get("labels") or {}).get("nodes") or []
        ],
        "mergeable": pr.get("mergeable") or "UNKNOWN",
        "reviewDecision": pr.get("reviewDecision"),
        # autoMergeRequest is non-null iff GitHub-native auto-merge is armed.
        "autoMergeEnabled": pr.get("autoMergeRequest") is not None,
        "statusCheckRollup": _status_rollup_to_list(pr),
        "files": [
            {"path": f["path"]} for f in (pr.get("files") or {}).get("nodes") or []
        ],
        "createdAt": pr["createdAt"],
        "updatedAt": pr["updatedAt"],
        "isDraft": bool(pr.get("isDraft") or False),
        "body": pr.get("body") or "",
        # Beyond the `gh pr list --json` schema, both read by
        # conformance.renovate.scan._parse_pr and both optional there: the branch
        # head's commit date (the clock a bounded-lock refusal expires against) and
        # the head uv.lock, present only for the few PRs fetch_lock_texts picked.
        "headCommittedAt": _head_committed_at(pr),
        "uvLockText": pr.get("uvLockText") or "",
    }


def normalize_merged_pr(pr: dict) -> dict:
    """Map a GraphQL PullRequest node to the subset of `gh pr list --json ...` fields
    `conformance.renovate.scan._auto_merge_stats` actually reads (only `reviews`)."""
    return {
        "reviews": [
            {
                "state": r.get("state"),
                "body": r.get("body"),
                "author": {"login": (r.get("author") or {}).get("login")},
            }
            for r in (pr.get("reviews") or {}).get("nodes") or []
        ],
    }


def group_by_repo(
    prs: list[dict], normalize: Callable[[dict], dict]
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for pr in prs:
        repo = pr["repository"]["nameWithOwner"]
        grouped.setdefault(repo, []).append(normalize(pr))
    return grouped


def slug_for(repo: str) -> str:
    return repo.replace("/", "_")


def write_repo_files(
    grouped: dict[str, list[dict]], out_dir: Path, known_repos: list[str]
) -> None:
    """Write one <slug>.json per repo. Repos with no matching PRs still get a `[]`
    file as long as they're in `known_repos`, preserving the "0 PRs = up to date"
    vs. "not configured" distinction the dashboard relies on."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for repo in set(known_repos) | set(grouped):
        path = out_dir / f"{slug_for(repo)}.json"
        path.write_text(json.dumps(grouped.get(repo, [])))


def run(
    scope: str,
    since: str,
    open_dir: Path,
    merged_dir: Path,
    known_repos: list[str],
    token: str,
    post: PostFn = _post_graphql,
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    open_nodes = fetch_all_prs(
        token, build_search_query(scope, "is:open"), _OPEN_PR_FIELDS, post
    )
    merged_nodes = fetch_all_prs(
        token,
        build_search_query(scope, f"is:merged merged:>={since}"),
        _MERGED_PR_FIELDS,
        post,
    )

    # Second pass, deliberately narrow: only the red uv.lock-only PRs, and only
    # then, get their lock contents pulled so the classifier can tell a stale
    # bounded-lock refusal from an ordinary broken build (FND-782).
    fetched = fetch_lock_texts(token, open_nodes, post)
    if fetched:
        print(
            f"Fetched uv.lock for {fetched} lock-refusal candidate(s)", file=sys.stderr
        )

    open_grouped = group_by_repo(open_nodes, normalize_open_pr)
    merged_grouped = group_by_repo(merged_nodes, normalize_merged_pr)

    write_repo_files(open_grouped, open_dir, known_repos)
    write_repo_files(merged_grouped, merged_dir, known_repos)

    return open_grouped, merged_grouped


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--org", required=True, help="org to scan in full-fleet mode, e.g. 'atlanhq'"
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "single repo to scan instead of the whole org, e.g. 'atlanhq/atlan-mysql-app'. "
            "Safe to always pass (even as an empty string) — resolve_scope() falls back to "
            "--org whenever this is empty/unset."
        ),
    )
    parser.add_argument(
        "--since",
        required=True,
        help="merged:>=YYYY-MM-DD lower bound for the merged-PR window",
    )
    parser.add_argument("--open-dir", required=True, type=Path)
    parser.add_argument("--merged-dir", required=True, type=Path)
    parser.add_argument(
        "--known-repos-file",
        type=Path,
        default=None,
        help="JSON array of repo full names to seed with an empty PR list even when they have zero matching PRs",
    )
    args = parser.parse_args(argv)

    token = os.environ["GH_TOKEN"]

    known_repos: list[str] = []
    if args.known_repos_file and args.known_repos_file.exists():
        known_repos = json.loads(args.known_repos_file.read_text())

    open_grouped, merged_grouped = run(
        scope=resolve_scope(args.org, args.repo),
        since=args.since,
        open_dir=args.open_dir,
        merged_dir=args.merged_dir,
        known_repos=known_repos,
        token=token,
    )

    open_count = sum(len(v) for v in open_grouped.values())
    merged_count = sum(len(v) for v in merged_grouped.values())
    print(
        f"Open Renovate PRs: {open_count} across {len(open_grouped)} repos",
        file=sys.stderr,
    )
    print(
        f"Merged Renovate PRs (since {args.since}): {merged_count} across {len(merged_grouped)} repos",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
