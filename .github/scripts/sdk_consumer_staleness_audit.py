#!/usr/bin/env python3
"""Audit which repos in a GitHub org consume ``atlan-application-sdk`` and how
long each has gone without changes.

Read-only. Every GitHub call is a GET (REST or a GraphQL query); nothing is
archived, labelled or modified. Output is a JSON dump plus a markdown report
with one row per consuming repo, bucketed by days since the last commit on its
default branch (<30, 30-60, 60-90, 90+), both for any commit and for the last
non-bot commit.

Discovery enumerates instead of searching. ``gh search code`` under-reports
private repos badly enough to read as a clean, plausible result set, so the
population comes from ``gh repo list``. One recursive tree read per repo finds
candidate manifests, and only those blobs are fetched. ``--cross-check-search``
runs code search afterwards and fails the audit if search finds a consumer that
enumeration did not.

The report names repos, and in an org with private repos those names should not
land in a public repository. Write the outputs somewhere private.

Usage:
    python .github/scripts/sdk_consumer_staleness_audit.py \\
        --owner atlanhq --out-json audit.json --out-md audit.md

Environment:
    GH_TOKEN   optional; otherwise the ambient ``gh`` login is used
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Optional

# (args) -> (returncode, stdout, stderr). Same seam as discover_org_consumers.py
# so tests stub `gh` without patching subprocess.
RunFn = Callable[[list[str]], tuple[int, str, str]]

REPO_LIST_LIMIT = 5000

# PEP 503 normalisation treats -, _ and . alike. The lookarounds stop
# atlan-application-sdk-conformance / -api (distinct packages) from matching.
_NAME = (
    r"(?<![A-Za-z0-9._-])atlan[-_.]application[-_.]sdk(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
)
_NAME_RE = re.compile(_NAME, re.IGNORECASE)
# The project's own name line (application-sdk declaring itself).
_OWN_NAME_RE = re.compile(r"^\s*name\s*=\s*[\"']" + _NAME + r"[\"']\s*$", re.I)
# What follows the name in a requirement: extras, version spec, url. Stops at a
# quote, comment, marker separator or end of line.
_SPEC_AFTER_RE = re.compile(r"([^\"'#;\n]*)")

MANIFEST_BASENAMES = ("pyproject.toml", "uv.lock", "setup.py", "setup.cfg")
_REQUIREMENTS_RE = re.compile(r"^requirements[^/]*\.(txt|in)$")
# Vendored or generated trees: a manifest in here is somebody else's package.
_SKIP_DIR_RE = re.compile(
    r"(^|/)(\.venv|venv|\.tox|node_modules|site-packages|vendor|dist|build|\.git)/"
)

# Bot authors. GitHub marks app accounts with type "Bot"; the login/name patterns
# catch commits whose author is not linked to an account (author: null).
DEFAULT_BOT_PATTERN = (
    r"\[bot\]|^renovate|^dependabot|^github-actions|^atlan-app-fleet"
    r"|mothership|^atlan-ci|^web-flow$"
)

BUCKETS = ("<30", "30-60", "60-90", "90+")
STALE_BUCKETS = ("60-90", "90+")

# Repo-name hints that a repo was never meant to live long.
_SCRATCH_NAME_RE = re.compile(
    r"(^|-)(poc|demo|test|tests|sandbox|playground|example|temp|tmp|old|"
    r"deprecated|archive|scratch|experiment|spike|hackathon)(-|$)",
    re.I,
)

FLEET_PRESET_MARKER = "application-sdk//renovate-config/default.json"

# An open PR counts as pending work only if a human touched it this recently.
ACTIVE_PR_DAYS = 60
# A commit subject or PR title seen in at least this many consumer repos is an
# org-wide sweep (a workflow removal, a policy rollout): a person made it, but
# it says nothing about anyone working on the repo.
SWEEP_MIN_REPOS = 5


class AuditError(RuntimeError):
    """A read failed for a reason other than "this does not exist".

    Raised instead of skipping the repo: a silently dropped repo is exactly the
    kind of miss this audit exists to prevent.
    """


# --------------------------------------------------------------------------- gh


def _run_gh(args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def _is_rate_limited(stderr: str) -> bool:
    s = stderr.lower()
    return "rate limit" in s or "abuse detection" in s


def _is_not_found(stderr: str) -> bool:
    return "HTTP 404" in stderr


def _is_empty_repo(stderr: str) -> bool:
    # GET commits / git/trees on a repo with no commits answers 409.
    return "HTTP 409" in stderr or "Git Repository is empty" in stderr


def make_retrying_run(
    run: RunFn = _run_gh,
    sleep: Callable[[float], None] = time.sleep,
    max_waits: int = 6,
) -> RunFn:
    """Wrap ``run`` so a rate-limit answer sleeps until reset and retries.

    The primary limit resets within the hour, so a long sweep pauses instead of
    failing half-done. Secondary limits carry no reset time; back off a minute.
    """

    def wrapped(args: list[str]) -> tuple[int, str, str]:
        for _ in range(max_waits + 1):
            rc, out, err = run(args)
            if rc == 0 or not _is_rate_limited(err):
                return rc, out, err
            wait = 60.0
            lrc, lout, _ = run(["api", "rate_limit"])
            if lrc == 0:
                try:
                    core = json.loads(lout)["resources"]["core"]
                    if core["remaining"] == 0:
                        wait = max(5.0, core["reset"] - time.time() + 5)
                except (KeyError, ValueError, TypeError):
                    pass
            print(f"rate limited; sleeping {int(wait)}s", file=sys.stderr)
            sleep(wait)
        return rc, out, err

    return wrapped


def _gh_json(run: RunFn, args: list[str], what: str):
    rc, out, err = run(args)
    if rc != 0:
        raise AuditError(f"{what}: {err.strip() or f'exit {rc}'}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise AuditError(f"{what}: unparseable response ({exc})") from exc


# ---------------------------------------------------------------- data shapes


@dataclass
class RepoInfo:
    name: str
    archived: bool
    fork: bool
    default_branch: Optional[str]


@dataclass
class ManifestMatch:
    path: str
    kind: str  # "direct" or "lock"
    spec: str = ""  # declared requirement text after the name, if any
    locked_version: str = ""


@dataclass
class AuditRow:
    repo: str
    archived: bool
    fork: bool
    default_branch: Optional[str]
    consumption: str  # "direct" or "lock-only"
    manifests: list[str] = field(default_factory=list)
    sdk_spec: str = ""
    sdk_locked: str = ""
    last_commit: Optional[str] = None
    last_human_commit: Optional[str] = None
    # True when the non-bot scan hit its page cap: last_human_commit is then the
    # oldest commit scanned, an upper bound on the real date.
    human_commit_is_bound: bool = False
    last_human_subject: str = ""
    # The last human commit's subject recurs across many consumer repos: an
    # org-wide maintenance sweep, not work on this repo.
    human_commit_is_sweep: bool = False
    days_since_commit: Optional[int] = None
    days_since_human_commit: Optional[int] = None
    bucket: str = ""
    human_bucket: str = ""
    open_prs: Optional[int] = None
    # Titles of open PRs by a human, updated within ACTIVE_PR_DAYS of as-of.
    active_human_prs: list[str] = field(default_factory=list)
    # The subset of those that are not org-wide sweeps.
    active_own_prs: int = 0
    fleet_preset: bool = False
    tree_truncated: bool = False
    proposed_action: str = ""
    action_reason: str = ""


# ------------------------------------------------------------- enumeration


def list_repos(owner: str, run: RunFn) -> list[RepoInfo]:
    """Every repo in ``owner``, archived and forks included (they are reported,
    not dropped)."""
    data = _gh_json(
        run,
        [
            "repo",
            "list",
            owner,
            "--limit",
            str(REPO_LIST_LIMIT),
            "--json",
            "name,isArchived,isFork,defaultBranchRef",
        ],
        f"gh repo list {owner}",
    )
    if len(data) >= REPO_LIST_LIMIT:
        raise AuditError(
            f"gh repo list returned {len(data)} repos, the --limit cap; "
            "raise REPO_LIST_LIMIT so no repo is silently dropped"
        )
    repos = []
    for r in data:
        ref = r.get("defaultBranchRef") or {}
        repos.append(
            RepoInfo(
                name=r["name"],
                archived=bool(r.get("isArchived")),
                fork=bool(r.get("isFork")),
                default_branch=ref.get("name") or None,
            )
        )
    return sorted(repos, key=lambda r: r.name)


def is_manifest_path(path: str) -> bool:
    if _SKIP_DIR_RE.search(path):
        return False
    parts = path.split("/")
    base = parts[-1]
    if base in MANIFEST_BASENAMES or _REQUIREMENTS_RE.match(base):
        return True
    # requirements/base.txt, requirements/prod.in
    return (
        len(parts) > 1
        and parts[-2] == "requirements"
        and base.endswith((".txt", ".in"))
    )


def manifest_blobs(tree: dict) -> list[tuple[str, str]]:
    """(path, blob sha) for every candidate manifest in a recursive tree."""
    return sorted(
        (e["path"], e["sha"])
        for e in tree.get("tree", [])
        if e.get("type") == "blob" and is_manifest_path(e["path"])
    )


# ---------------------------------------------------------------- matching


# TOML key form, as in poetry `atlan-application-sdk = "^3"` or uv sources
# `atlan-application-sdk = { git = "...", tag = "v3" }`. A single `=` (not
# `==`) after the closing quote/name is an assignment, not a version operator.
_KEY_FORM_RE = re.compile(r"[\"']?\s*=(?!=)\s*(.*)$")


def _spec_after(line: str, end: int) -> str:
    key = _KEY_FORM_RE.match(line, end)
    if key:
        value = key.group(1).split("#", 1)[0].strip().rstrip(",").strip()
        return value.strip("\"'")
    tail = _SPEC_AFTER_RE.match(line, end)
    return (tail.group(1) if tail else "").strip().rstrip(",").strip()


def parse_uv_lock(text: str) -> Optional[ManifestMatch]:
    """The resolved SDK entry in a uv.lock, or None.

    A block whose source is ``editable = "."`` / ``virtual = "."`` is the repo
    itself (application-sdk's own lock), not a consumer."""
    for block in re.split(r"^\[\[package\]\]\s*$", text, flags=re.M)[1:]:
        m = re.search(r'^name\s*=\s*"([^"]+)"', block, re.M)
        if not m or not _NAME_RE.fullmatch(m.group(1)):
            continue
        src = re.search(r"^source\s*=\s*(\{[^\n]*\})", block, re.M)
        if src and re.search(r'(editable|virtual)\s*=\s*"\."', src.group(1)):
            return None
        ver = re.search(r'^version\s*=\s*"([^"]+)"', block, re.M)
        return ManifestMatch(
            path="", kind="lock", locked_version=ver.group(1) if ver else ""
        )
    return None


def parse_declaration(path: str, text: str) -> Optional[ManifestMatch]:
    """A direct SDK requirement in pyproject / requirements / setup files."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _OWN_NAME_RE.match(line):
            continue
        m = _NAME_RE.search(line)
        if not m:
            continue
        return ManifestMatch(path=path, kind="direct", spec=_spec_after(line, m.end()))
    return None


def match_manifest(path: str, text: str) -> Optional[ManifestMatch]:
    if path.rsplit("/", 1)[-1] == "uv.lock":
        m = parse_uv_lock(text)
    else:
        m = parse_declaration(path, text)
    if m:
        m.path = path
    return m


def read_blob(owner: str, repo: str, sha: str, run: RunFn) -> str:
    data = _gh_json(
        run,
        ["api", f"repos/{owner}/{repo}/git/blobs/{sha}"],
        f"{repo}: read blob {sha}",
    )
    return base64.b64decode(data.get("content", "")).decode("utf-8", "replace")


def find_sdk_manifests(
    owner: str, repo: RepoInfo, run: RunFn
) -> tuple[list[ManifestMatch], bool, list[str]]:
    """(matches, tree_truncated, all tree paths) for one repo."""
    if not repo.default_branch:
        return [], False, []
    rc, out, err = run(
        [
            "api",
            f"repos/{owner}/{repo.name}/git/trees/{repo.default_branch}?recursive=1",
        ]
    )
    if rc != 0:
        if _is_empty_repo(err) or _is_not_found(err):
            return [], False, []
        raise AuditError(f"{repo.name}: read tree: {err.strip()}")
    tree = json.loads(out)
    matches = []
    for path, sha in manifest_blobs(tree):
        m = match_manifest(path, read_blob(owner, repo.name, sha, run))
        if m:
            matches.append(m)
    paths = [e["path"] for e in tree.get("tree", [])]
    return matches, bool(tree.get("truncated")), paths


# --------------------------------------------------------------- staleness


def is_bot_commit(commit: dict, bot_re: re.Pattern[str]) -> bool:
    author = commit.get("author") or {}
    if author.get("type") == "Bot":
        return True
    login = author.get("login") or ""
    name = ((commit.get("commit") or {}).get("author") or {}).get("name") or ""
    return bool(bot_re.search(login) or bot_re.search(name))


def _commit_date(commit: dict) -> str:
    # Committer date is when the change landed on the branch (merge/squash
    # time); author date can predate that by weeks on a long-lived PR.
    c = commit.get("commit") or {}
    return ((c.get("committer") or {}).get("date")) or (
        (c.get("author") or {}).get("date") or ""
    )


def _subject(commit: dict) -> str:
    message = (commit.get("commit") or {}).get("message") or ""
    return message.split("\n", 1)[0].strip()


@dataclass
class CommitHistory:
    last_commit: Optional[str] = None
    last_human_commit: Optional[str] = None
    last_human_subject: str = ""
    # The scan hit its page cap without finding a human commit;
    # last_human_commit is the oldest commit seen, an upper bound.
    human_is_bound: bool = False


def last_commits(
    owner: str,
    repo: RepoInfo,
    bot_re: re.Pattern[str],
    run: RunFn,
    max_pages: int = 10,
) -> CommitHistory:
    """Last commit and last non-bot commit on the default branch."""
    if not repo.default_branch:
        return CommitHistory()
    last_any: Optional[str] = None
    oldest_seen: Optional[str] = None
    for page in range(1, max_pages + 1):
        rc, out, err = run(
            [
                "api",
                f"repos/{owner}/{repo.name}/commits"
                f"?sha={repo.default_branch}&per_page=100&page={page}",
            ]
        )
        if rc != 0:
            if _is_empty_repo(err):
                return CommitHistory()
            raise AuditError(f"{repo.name}: list commits: {err.strip()}")
        commits = json.loads(out)
        if not commits:
            return CommitHistory(last_commit=last_any)
        if last_any is None:
            last_any = _commit_date(commits[0])
        for c in commits:
            if not is_bot_commit(c, bot_re):
                return CommitHistory(last_any, _commit_date(c), _subject(c))
        oldest_seen = _commit_date(commits[-1])
        if len(commits) < 100:
            return CommitHistory(last_commit=last_any)
    return CommitHistory(last_any, oldest_seen, "", True)


def open_prs(
    owner: str, repo: str, as_of: date, bot_re: re.Pattern[str], run: RunFn
) -> tuple[int, list[str]]:
    """(open PR count, titles of open human PRs updated in ACTIVE_PR_DAYS).

    A bare count would read a pile of stale Renovate/Dependabot PRs as pending
    work, which is the opposite of what it means."""
    q = (
        "query($o:String!,$n:String!){repository(owner:$o,name:$n)"
        "{pullRequests(states:OPEN,first:100,"
        "orderBy:{field:UPDATED_AT,direction:DESC})"
        "{totalCount nodes{title updatedAt author{__typename login}}}}}"
    )
    data = _gh_json(
        run,
        ["api", "graphql", "-f", f"query={q}", "-f", f"o={owner}", "-f", f"n={repo}"],
        f"{repo}: open PRs",
    )
    prs = data["data"]["repository"]["pullRequests"]
    active = []
    for pr in prs.get("nodes") or []:
        author = pr.get("author") or {}
        if author.get("__typename") == "Bot" or bot_re.search(
            author.get("login") or ""
        ):
            continue
        days = days_between(pr.get("updatedAt"), as_of)
        if days is not None and days < ACTIVE_PR_DAYS:
            active.append(pr.get("title") or "")
    return int(prs["totalCount"]), active


def extends_fleet_preset(owner: str, repo: str, paths: list[str], run: RunFn) -> bool:
    for candidate in ("renovate.json", ".github/renovate.json", "renovate.json5"):
        if candidate not in paths:
            continue
        rc, out, err = run(
            [
                "api",
                f"repos/{owner}/{repo}/contents/{candidate}",
                "-H",
                "Accept: application/vnd.github.raw",
            ]
        )
        if rc != 0:
            raise AuditError(f"{repo}: read {candidate}: {err.strip()}")
        return FLEET_PRESET_MARKER in out
    return False


def days_between(iso: Optional[str], as_of: date) -> Optional[int]:
    if not iso:
        return None
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return (as_of - d.date()).days


def bucket_for(days: Optional[int]) -> str:
    """<30, 30-60, 60-90, 90+. A repo with no qualifying commit is 90+: nothing
    has happened in it that we can see."""
    if days is None or days >= 90:
        return "90+"
    if days >= 60:
        return "60-90"
    if days >= 30:
        return "30-60"
    return "<30"


# ----------------------------------------------------------------- actions


def propose_action(row: AuditRow) -> tuple[str, str]:
    """A starting recommendation for a human to confirm, never an instruction.

    Keyed on the non-bot bucket: a repo kept warm only by dependency bumps is
    still unattended."""
    if row.human_bucket not in STALE_BUCKETS:
        return "", ""
    age = (
        f"no human commit in {row.days_since_human_commit}d"
        if row.days_since_human_commit is not None
        else "no human commit found"
    )
    if row.human_commit_is_sweep:
        age += " (and that one is an org-wide sweep)"
    if row.archived:
        return "exclude from fleet tooling", "already archived on GitHub"
    if row.fork:
        return "exclude from fleet tooling", f"fork, not a source repo; {age}"
    if row.active_own_prs:
        return (
            "keep",
            f"{row.active_own_prs} human PR(s) active in the last "
            f"{ACTIVE_PR_DAYS}d; {age}",
        )
    if _SCRATCH_NAME_RE.search(row.repo) and row.human_bucket == "90+":
        return "archive", f"name marks it as scratch/legacy; {age}"
    if row.fleet_preset:
        return (
            "exclude from fleet tooling",
            f"on the fleet Renovate preset, so bots keep committing; {age}",
        )
    if row.human_bucket == "90+":
        return "archive", f"{age}, no active human PRs, not on fleet tooling"
    return "keep", f"{age}; recheck next run before archiving"


def normalize_subject(subject: str) -> str:
    """Strip the trailing PR ref and collapse numbers, so per-repo variants of
    one sweep compare equal."""
    s = re.sub(r"\s*\(#\d+\)\s*$", "", subject.strip())
    return re.sub(r"\d+", "N", s.lower())


def mark_sweeps(rows: list[AuditRow], min_repos: int = SWEEP_MIN_REPOS) -> set[str]:
    """Flag commits/PRs whose subject recurs across ``min_repos`` or more repos,
    then (re)derive each row's own-PR count and proposed action.

    Subjects are compared after normalize_subject, so the same sweep landing
    as PR #5 in one repo and #17 in another, or bumping 3 deps here and 7 there,
    is recognised as one. Needs every row first, so it runs after the per-repo
    fan-out."""
    seen: dict[str, set[str]] = {}
    for r in rows:
        subjects = {r.last_human_subject, *r.active_human_prs} - {""}
        for key in {normalize_subject(s) for s in subjects}:
            seen.setdefault(key, set()).add(r.repo)
    sweeps = {k for k, repos in seen.items() if len(repos) >= min_repos}
    for r in rows:
        r.human_commit_is_sweep = (
            bool(r.last_human_subject)
            and normalize_subject(r.last_human_subject) in sweeps
        )
        r.active_own_prs = sum(
            normalize_subject(t) not in sweeps for t in r.active_human_prs
        )
        r.proposed_action, r.action_reason = propose_action(r)
    return sweeps


# -------------------------------------------------------------- orchestration


def audit_repo(
    owner: str,
    repo: RepoInfo,
    as_of: date,
    bot_re: re.Pattern[str],
    run: RunFn,
) -> Optional[AuditRow]:
    matches, truncated, paths = find_sdk_manifests(owner, repo, run)
    if not matches:
        return None
    direct = [m for m in matches if m.kind == "direct"]
    locks = [m for m in matches if m.kind == "lock"]
    row = AuditRow(
        repo=repo.name,
        archived=repo.archived,
        fork=repo.fork,
        default_branch=repo.default_branch,
        consumption="direct" if direct else "lock-only",
        manifests=[m.path for m in matches],
        sdk_spec="; ".join(sorted({m.spec for m in direct if m.spec})),
        sdk_locked=", ".join(
            sorted({m.locked_version for m in locks if m.locked_version})
        ),
        tree_truncated=truncated,
    )
    h = last_commits(owner, repo, bot_re, run)
    row.last_commit, row.last_human_commit = h.last_commit, h.last_human_commit
    row.last_human_subject, row.human_commit_is_bound = (
        h.last_human_subject,
        h.human_is_bound,
    )
    row.days_since_commit = days_between(row.last_commit, as_of)
    row.days_since_human_commit = days_between(row.last_human_commit, as_of)
    row.bucket = bucket_for(row.days_since_commit)
    row.human_bucket = bucket_for(row.days_since_human_commit)
    row.open_prs, row.active_human_prs = open_prs(owner, repo.name, as_of, bot_re, run)
    row.fleet_preset = extends_fleet_preset(owner, repo.name, paths, run)
    # Proposed action is set by mark_sweeps once every row is in.
    return row


def search_consumers(owner: str, run: RunFn) -> set[str]:
    """Repos code search reports as mentioning the SDK in a manifest. Used only
    as a cross-check: search misses repos, so it can never shrink the result."""
    found: set[str] = set()
    for filename in ("pyproject.toml", "uv.lock", "requirements.txt", "setup.py"):
        data = _gh_json(
            run,
            [
                "search",
                "code",
                '"atlan-application-sdk"',
                "--owner",
                owner,
                "--filename",
                filename,
                "--limit",
                "1000",
                "--json",
                "repository",
            ],
            f"code search ({filename})",
        )
        for hit in data:
            full = (hit.get("repository") or {}).get("nameWithOwner", "")
            if full:
                found.add(full.split("/", 1)[-1])
    return found


def run_audit(
    owner: str,
    as_of: date,
    exclude: set[str],
    bot_pattern: str = DEFAULT_BOT_PATTERN,
    workers: int = 8,
    run: RunFn = _run_gh,
) -> tuple[list[AuditRow], int]:
    """(rows sorted stalest first, number of repos scanned)."""
    bot_re = re.compile(bot_pattern, re.I)
    repos = [r for r in list_repos(owner, run) if r.name not in exclude]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(
            pool.map(lambda r: audit_repo(owner, r, as_of, bot_re, run), repos)
        )
    rows = [r for r in results if r is not None]
    mark_sweeps(rows)
    rows.sort(
        key=lambda r: (
            -(
                r.days_since_human_commit
                if r.days_since_human_commit is not None
                else 10**6
            ),
            r.repo,
        )
    )
    return rows, len(repos)


# ---------------------------------------------------------------- rendering


def bucket_counts(rows: list[AuditRow], attr: str) -> dict[str, int]:
    counts = {b: 0 for b in BUCKETS}
    for r in rows:
        counts[getattr(r, attr)] += 1
    return counts


def _md_cell(value: object) -> str:
    return str(value).replace("|", "\\|") if value not in (None, "") else "—"


def render_markdown(rows: list[AuditRow], owner: str, as_of: date, scanned: int) -> str:
    any_c = bucket_counts(rows, "bucket")
    human_c = bucket_counts(rows, "human_bucket")
    lines = [
        f"# atlan-application-sdk consumer staleness — {owner}, as of {as_of.isoformat()}",
        "",
        f"{len(rows)} consuming repos out of {scanned} scanned "
        f"({sum(r.consumption == 'direct' for r in rows)} direct, "
        f"{sum(r.consumption == 'lock-only' for r in rows)} lock-only; "
        f"{sum(r.archived for r in rows)} archived).",
        "",
        "| Bucket | Any commit | Non-bot commit |",
        "|---|---:|---:|",
        *(f"| {b} | {any_c[b]} | {human_c[b]} |" for b in BUCKETS),
        "",
        "| Repo | SDK locked | SDK spec | Last commit | Last human commit | Bucket "
        "| Human bucket | Archived | Open PRs (active own) | Fleet preset "
        "| Proposed action | Reason |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        human = (r.last_human_commit or "")[:10]
        if r.human_commit_is_bound and human:
            human = f"before {human}"
        if r.human_commit_is_sweep and human:
            human += " (sweep)"
        lines.append(
            "| "
            + " | ".join(
                _md_cell(v)
                for v in (
                    r.repo,
                    r.sdk_locked,
                    r.sdk_spec,
                    (r.last_commit or "")[:10],
                    human,
                    r.bucket,
                    r.human_bucket,
                    "yes" if r.archived else "",
                    f"{r.open_prs} ({r.active_own_prs})",
                    "yes" if r.fleet_preset else "",
                    r.proposed_action,
                    r.action_reason,
                )
            )
            + " |"
        )
    # One example subject per sweep, keyed by its normalized form.
    examples: dict[str, str] = {}
    for r in rows:
        if r.human_commit_is_sweep:
            examples.setdefault(
                normalize_subject(r.last_human_subject), r.last_human_subject
            )
    sweeps = sorted(examples.values())
    if sweeps:
        lines += [
            "",
            f"Org-wide sweep commits (subject in {SWEEP_MIN_REPOS}+ repos; "
            "not counted as work on the repo):",
            *(f"- `{s}`" for s in sweeps),
        ]
    truncated = [r.repo for r in rows if r.tree_truncated]
    if truncated:
        lines += [
            "",
            f"Tree listing truncated (manifests may be missed): {', '.join(truncated)}",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- cli


def main(argv: Optional[list[str]] = None, run: RunFn = _run_gh) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--owner", default="atlanhq")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="date the buckets are measured against (default: today, UTC)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=["application-sdk"],
        help="repo to skip (repeatable); the SDK itself is skipped by default",
    )
    parser.add_argument("--bot-pattern", default=DEFAULT_BOT_PATTERN)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    parser.add_argument(
        "--cross-check-search",
        action="store_true",
        help="also run code search; fail if it finds a consumer enumeration missed",
    )
    args = parser.parse_args(argv)

    as_of = args.as_of or datetime.now(timezone.utc).date()
    retrying = make_retrying_run(run)
    try:
        rows, scanned = run_audit(
            args.owner,
            as_of,
            set(args.exclude),
            args.bot_pattern,
            args.workers,
            retrying,
        )
        missed: list[str] = []
        if args.cross_check_search:
            found = {r.repo for r in rows}
            missed = sorted(
                search_consumers(args.owner, retrying) - found - set(args.exclude)
            )
    except AuditError as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 1

    with open(args.out_json, "w") as f:
        json.dump(
            {
                "owner": args.owner,
                "as_of": as_of.isoformat(),
                "scanned": scanned,
                "rows": [asdict(r) for r in rows],
            },
            f,
            indent=2,
        )
    with open(args.out_md, "w") as f:
        f.write(render_markdown(rows, args.owner, as_of, scanned))

    print(f"{len(rows)} consumers of {scanned} repos scanned", file=sys.stderr)
    if missed:
        # Search only ever under-reports, so a repo it found that enumeration did
        # not means the manifest matcher is wrong. Fail rather than publish a
        # list that looks complete.
        print(
            "code search found consumers enumeration missed: " + ", ".join(missed),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
