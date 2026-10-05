#!/usr/bin/env python3
"""Approval gate for conformance-resync PRs — the atlan-ci code-owner review.

The conformance resync lane (``.github/workflows/conformance-resync.yml``, FND-2868)
keeps one PR per app repo on ``bot/conformance-resync``, authored by the
atlan-conformance-sync App, carrying ``atlan-application-sdk-conformance bootstrap --resync`` output:
the re-rendered tests.yaml / renovate.json / managed workflow shims / review
kit. Those PRs rewrite ``.github/workflows/*`` files, so the Renovate gate in
``renovate_approval_conditions.py`` correctly refuses them (pin-only workflow
diffs, FND-1996). This module is the separate, narrower path for them.

**Identity selects; content proves.** Author and branch only decide which PRs
reach this gate — a branch name is something anyone with push access can create, so neither is
trusted as evidence. The approval rests on re-rendering the PR independently
here and requiring a byte-identical result:

  a. author is ``atlan-conformance-sync[bot]``, head branch is exactly
     ``bot/conformance-resync`` in this same repo, base is ``main``, PR open
     and not a draft,
     current HEAD is the SHA under evaluation
  b. the body carries the lane's marker, naming the suite version it rendered
  c. exactly one commit on the PR, authored by the lane, with one parent
  d. that parent is in the base branch's history (the render base is real main)
  e0. ``renovate.json`` at that parent is in auto-merge mode
     (``discover_org_consumers.automerge_mode`` == ``auto``); soft, unknown,
     missing and unreadable withhold the approval, so a person reviews resync
     PRs in soft-mode repos
  e. re-render: check out the parent, read the conformance version its
     ``uv.lock`` resolves (must equal the marker), run ``bootstrap --resync
     --json`` at exactly that version with the marker's ``resolved-at``
     resolution fence (:func:`resync_command`), stage exactly what the lane stages, and
     require the resulting git tree to EQUAL the PR head's tree — any extra,
     missing or altered byte anywhere withholds the approval
  f. the re-render dropped no per-repo setting (``.bak`` set-compare)
  g. every ruleset-required check is green
  h. atlan-ci has not already approved this head with the resync signature;
     the head is re-read just before posting, and the review is pinned to it
     with ``commit_id``

The lane (``.github/scripts/conformance_resync.py``) imports
:func:`stage_like_the_lane`, :data:`ACCEPTED_DROPS` and the marker from here, so
the two cannot drift; if they ever did, the trees would differ, which makes the trees
differ, which fails CLOSED (no approval), never open.

Fail closed throughout: anything other than an affirmative signal skips.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import subprocess
import tempfile
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import discover_org_consumers as discover

# Constants the resync lane (conformance_resync.py) shares with this gate.
RESYNC_AUTHOR = "atlan-conformance-sync[bot]"
RESYNC_BRANCH = "bot/conformance-resync"
RESYNC_SIGNATURE = "**Conformance resync auto-approval:**"
APPROVER_LOGIN = "atlan-ci"
CONFORMANCE_PACKAGE = "atlan-application-sdk-conformance"
_MARKER_RE = re.compile(
    r"<!--\s*conformance-resync-lane\s+suite=(\d+\.\d+\.\d+)"
    r"\s+resolved-at=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*-->"
)
BASE_BRANCH = "main"
RELEASE_AGE = timedelta(days=7)
FIRST_PARTY = (
    "atlan-application-sdk",
    "atlan-application-sdk-conformance",
    "pyatlan",
)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
BOOTSTRAP_TIMEOUT = 600


def pr_marker(suite_version: str, resolved_at: str) -> str:
    """The hidden marker every lane PR body leads with. Shared with the lane
    (``conformance_resync.py``) so the two never render it differently.
    ``resolved_at`` fixes the dependency resolution both sides use."""
    return (
        f"<!-- conformance-resync-lane suite={suite_version} "
        f"resolved-at={resolved_at} -->"
    )


def resync_command(suite: str, resolved_at: str) -> list[str]:
    """The one ``bootstrap --resync`` invocation the lane and this gate run.
    Third-party packages resolve as of ``resolved_at`` minus the org release-age
    window; first-party packages as of ``resolved_at`` itself."""
    at = datetime.strptime(resolved_at, "%Y-%m-%dT%H:%M:%SZ")
    cutoff = (at - RELEASE_AGE).strftime("%Y-%m-%dT%H:%M:%SZ")
    first_party = [
        arg
        for pkg in FIRST_PARTY
        for arg in ("--exclude-newer-package", f"{pkg}={resolved_at}")
    ]
    return [
        "uvx",
        "--isolated",
        "--no-config",
        "--exclude-newer",
        cutoff,
        *first_party,
        "--from",
        f"{CONFORMANCE_PACKAGE}=={suite}",
        CONFORMANCE_PACKAGE,
        "bootstrap",
        "--resync",
        "--json",
    ]


RESYNC_APPROVAL_BODY = (
    f"{RESYNC_SIGNATURE} this PR's tree is byte-identical to an independent "
    "`bootstrap --resync` render of its base commit at the conformance version "
    "that commit's `uv.lock` pins, it is a single commit by the resync lane, it "
    "drops no per-repo setting, and every required check is green.\n\n"
    "Posted by `.github/scripts/resync_approval_conditions.py` "
    "(application-sdk). Any push to this branch dismisses this approval."
)

Runner = Callable[..., subprocess.CompletedProcess]


class GhError(RuntimeError):
    """A GitHub or git call failed — aborts the step (a red step is visible)."""


# ---------------------------------------------------------------------------
# Pure conditions
# ---------------------------------------------------------------------------


def is_candidate(meta: dict[str, Any]) -> bool:
    """Whether this PR belongs to the resync path at all (routing only — not
    evidence; see the module docstring)."""
    return ((meta.get("user") or {}).get("login")) == RESYNC_AUTHOR and (
        (meta.get("head") or {}).get("ref")
    ) == RESYNC_BRANCH


def marker_suite_version(body: str | None) -> str | None:
    m = _MARKER_RE.search(body or "")
    return m.group(1) if m else None


def marker_resolved_at(body: str | None) -> str | None:
    m = _MARKER_RE.search(body or "")
    return m.group(2) if m else None


def check_meta(
    pr: str, meta: dict[str, Any], repo: str, eval_sha: str
) -> tuple[bool, str]:
    """Condition (a)."""
    head = meta.get("head") or {}
    if ((meta.get("user") or {}).get("login")) != RESYNC_AUTHOR:
        return False, f"PR #{pr}: author is not {RESYNC_AUTHOR} — skipping."
    if head.get("ref") != RESYNC_BRANCH:
        return False, f"PR #{pr}: head branch is not {RESYNC_BRANCH} — skipping."
    if ((head.get("repo") or {}).get("full_name")) != repo:
        return False, f"PR #{pr}: head is not in {repo} (fork?) — skipping."
    if ((meta.get("base") or {}).get("ref")) != BASE_BRANCH:
        return False, f"PR #{pr}: base is not {BASE_BRANCH} — skipping."
    if meta.get("state") != "open" or meta.get("draft"):
        return False, f"PR #{pr}: not open, or a draft — skipping."
    if not eval_sha or head.get("sha") != eval_sha:
        return False, f"PR #{pr}: HEAD moved since this run was triggered — skipping."
    return True, ""


def check_commits(pr: str, commits: list[Any], head_sha: str) -> tuple[bool, str, str]:
    """Condition (c): ``(ok, message, parent_sha)``."""
    if len(commits) != 1:
        return (
            False,
            f"PR #{pr}: {len(commits)} commits (expected the lane's one) — skipping.",
            "",
        )
    c = commits[0] if isinstance(commits[0], dict) else {}
    if c.get("sha") != head_sha:
        return False, f"PR #{pr}: the commit is not the PR head — skipping.", ""
    if ((c.get("author") or {}).get("login")) != RESYNC_AUTHOR:
        return (
            False,
            f"PR #{pr}: commit not authored by {RESYNC_AUTHOR} — skipping.",
            "",
        )
    parents = c.get("parents") or []
    if len(parents) != 1 or not (parents[0] or {}).get("sha"):
        return (
            False,
            f"PR #{pr}: commit does not have exactly one parent — skipping.",
            "",
        )
    return True, "", str(parents[0]["sha"])


def pinned_conformance(uv_lock_text: str) -> str | None:
    """The conformance version a ``uv.lock`` resolves, or None."""
    try:
        data = tomllib.loads(uv_lock_text)
    except tomllib.TOMLDecodeError:
        return None
    for pkg in data.get("package", []):
        if isinstance(pkg, dict) and pkg.get("name") == CONFORMANCE_PACKAGE:
            version = pkg.get("version")
            if isinstance(version, str) and _SEMVER_RE.match(version):
                return version
            return None
    return None


def parse_manifest(stdout: str) -> dict | None:
    """The last ``--json`` manifest line (an object with a ``touched`` list)."""
    manifest = None
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            doc = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("touched"), list):
            manifest = doc
    return manifest


def _normalise(line: str) -> str:
    # A reordered JSON object flips `"x": "y",` <-> `"x": "y"` on its last
    # member; strip that so a pure reorder never reads as a lost setting.
    return line.strip().rstrip(",").strip()


# Settings the canonical templates deliberately stopped carrying: a render
# that drops one of these is the intended change, not a lost per-repo value.
# Keyed by repo-relative path; matched on the YAML/JSON key. The lane imports
# this mapping, so there is one copy.
ACCEPTED_DROPS: dict[str, frozenset[str]] = {
    ".github/workflows/tests.yaml": frozenset(
        {"container-health-timeout-seconds", "e2e-clouds"}
    ),
}


def _setting_key(line: str) -> str:
    head = line.strip().split(":", 1)[0] if ":" in line else ""
    return head.strip().strip('"').strip("'").lstrip("-").strip()


def still_lost(path: str, lost: list[str]) -> list[str]:
    """Lost lines for ``path`` minus the accepted drops."""
    accepted = ACCEPTED_DROPS.get(path, frozenset())
    return [line for line in lost if _setting_key(line) not in accepted]


def lost_setting_lines(backup_text: str, new_text: str) -> list[str]:
    """Non-comment lines in the ``.bak`` absent from its replacement
    (reorder-immune, counted per line so a duplicate elsewhere in the file
    cannot stand in for a removed one). The lane imports this function."""
    remaining = Counter(_normalise(x) for x in new_text.splitlines())
    lost: list[str] = []
    for line in backup_text.splitlines():
        norm = _normalise(line)
        if not norm or norm.startswith("#") or norm.startswith("//"):
            continue
        if norm in {"{", "}", "[", "]", "},", "],"}:
            continue
        if remaining[norm] > 0:
            remaining[norm] -= 1
        elif line.strip() not in lost:
            lost.append(line.strip())
    return lost


def safe_touched(manifest: dict, root: pathlib.Path) -> list[str]:
    """The lane's staging filter: manifest paths only, no escapes, no backups,
    nothing reached through a symlinked directory."""
    out: set[str] = set()
    for p in manifest.get("touched") or []:
        if not isinstance(p, str) or not p:
            continue
        if p.startswith("/") or ".." in p.split("/") or p.endswith(".bak"):
            continue
        cur = root
        via_link = False
        for part in pathlib.PurePosixPath(p).parts[:-1]:
            cur = cur / part
            if cur.is_symlink():
                via_link = True
                break
        if not via_link:
            out.add(p)
    return sorted(out)


def count_resync_approvals(reviews: list[Any], head_sha: str) -> int:
    """Condition (h): atlan-ci approvals of THIS head with the resync signature."""
    return sum(
        1
        for r in reviews
        if isinstance(r, dict)
        and ((r.get("user") or {}).get("login")) == APPROVER_LOGIN
        and r.get("state") == "APPROVED"
        and r.get("commit_id") == head_sha
        and str(r.get("body") or "").startswith(RESYNC_SIGNATURE)
    )


# ---------------------------------------------------------------------------
# Re-render (the proof)
# ---------------------------------------------------------------------------


@dataclass
class RenderResult:
    tree_matches: bool
    suite: str | None = None
    lost: dict[str, list[str]] = field(default_factory=dict)
    note: str = ""


def _git_env() -> dict[str, str]:
    """Token in env only (GIT_CONFIG_*) — never on argv or in a remote URL."""
    token = os.environ.get("GH_TOKEN", "")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(args: list[str], cwd: str, runner: Runner) -> str:
    result = runner(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_git_env(),
        check=False,
    )
    if result.returncode != 0:
        raise GhError(f"git {args[0]} failed: {(result.stderr or '')[-300:]}")
    return result.stdout or ""


def stage_like_the_lane(
    work: str, manifest: dict, runner: Runner
) -> dict[str, list[str]]:
    """Apply the lane's ``.bak`` discipline and staging to the render in
    ``work``; return lost settings. The lane imports this function."""
    root = pathlib.Path(work)
    lost: dict[str, list[str]] = {}
    backups = sorted(
        p for p in root.rglob("*.bak") if ".git" not in p.relative_to(root).parts
    )
    for bak in backups:
        original = bak.with_suffix("")
        if original.exists():
            rel = str(original.relative_to(root))
            missing = still_lost(
                rel,
                lost_setting_lines(
                    bak.read_text(encoding="utf-8", errors="replace"),
                    original.read_text(encoding="utf-8", errors="replace"),
                ),
            )
            if missing:
                lost[rel] = missing
        bak.unlink()
    touched = safe_touched(manifest, root)
    if touched:
        _git(["add", "-A", "-f", "--", *touched], work, runner)
    return lost


def parent_automerge_mode(repo: str, parent_sha: str, runner: Runner) -> str:
    """Condition (e0): the repo's renovate.json at the render base, classified
    by the fleet's shared rule. ``auto`` is the only approvable answer; soft,
    unknown, missing and unreadable all withhold the approval."""
    result = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/contents/renovate.json?ref={parent_sha}",
            "-q",
            ".content",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        return "missing"
    try:
        text = base64.b64decode(result.stdout).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return "unreadable"
    return discover.automerge_mode(text)


def parent_pinned_conformance(repo: str, parent_sha: str, runner: Runner) -> str | None:
    """The conformance version ``uv.lock`` resolves at the render base, read
    raw (``uv.lock`` routinely exceeds the contents API's 1 MB base64 cap)."""
    result = runner(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo}/contents/uv.lock?ref={parent_sha}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    return pinned_conformance(result.stdout)


def parent_preconditions(
    repo: str, parent_sha: str, suite: str, runner: Runner
) -> tuple[bool, str]:
    """The gate's checks on the render base that do not need a render: its
    ``uv.lock`` pins ``suite`` (condition e, first half) and its
    ``renovate.json`` is in auto-merge mode (condition e0).

    The lane calls this too before leaving an unchanged PR alone. A PR whose
    parent fails it can never be approved however often the approver is
    dispatched, so the lane must re-push it onto current main instead.
    """
    pinned = parent_pinned_conformance(repo, parent_sha, runner)
    if pinned != suite:
        return False, f"uv.lock at the parent pins {pinned}, the PR marker says {suite}"
    mode = parent_automerge_mode(repo, parent_sha, runner)
    if mode != "auto":
        return False, f"renovate.json at the parent is {mode}, not auto-merge"
    return True, ""


def render_and_compare(
    repo: str,
    parent_sha: str,
    head_sha: str,
    suite: str,
    resolved_at: str,
    runner: Runner,
) -> RenderResult:
    """Condition (e)/(f): render the parent at ``suite`` and compare trees."""
    with tempfile.TemporaryDirectory(prefix="resync-verify-") as tmp:
        work = os.path.join(tmp, "repo")
        os.makedirs(work)
        _git(["init", "-q"], work, runner)
        _git(["remote", "add", "origin", f"https://github.com/{repo}"], work, runner)
        _git(
            ["fetch", "-q", "--depth", "1", "origin", parent_sha, head_sha],
            work,
            runner,
        )
        _git(["checkout", "-q", "--detach", parent_sha], work, runner)
        lock = pathlib.Path(work, "uv.lock")
        pinned = (
            pinned_conformance(lock.read_text(encoding="utf-8"))
            if lock.is_file()
            else None
        )
        if pinned != suite:
            return RenderResult(
                False,
                pinned,
                note=f"base uv.lock pins {pinned}, PR marker says {suite}",
            )
        # bootstrap only renders templates onto disk; it gets no credential.
        env = {
            k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}
        }
        proc = runner(
            resync_command(suite, resolved_at),
            cwd=work,
            capture_output=True,
            text=True,
            env=env,
            timeout=BOOTSTRAP_TIMEOUT,
            check=False,
        )
        manifest = parse_manifest(proc.stdout or "")
        if proc.returncode != 0 or manifest is None or manifest.get("skipped"):
            return RenderResult(
                False, suite, note=f"bootstrap render failed (exit {proc.returncode})"
            )
        lost = stage_like_the_lane(work, manifest, runner)
        rendered_tree = _git(["write-tree"], work, runner).strip()
        head_tree = _git(["rev-parse", f"{head_sha}^{{tree}}"], work, runner).strip()
        matches = bool(rendered_tree) and rendered_tree == head_tree
        return RenderResult(
            matches,
            suite,
            lost,
            note="" if matches else "rendered tree differs from the PR head",
        )


# ---------------------------------------------------------------------------
# GitHub I/O + orchestration
# ---------------------------------------------------------------------------


def _gh_json(args: list[str], runner: Runner, *, what: str) -> Any:
    result = runner(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GhError(f"{what}: {(result.stderr or '').strip()[-300:]}")
    try:
        return json.loads(result.stdout or "null")
    except ValueError as exc:
        raise GhError(f"{what}: unparseable response") from exc


def _flatten(payload: Any) -> list[Any]:
    """``--paginate --slurp`` yields a list of pages; flatten to one list."""
    if (
        isinstance(payload, list)
        and payload
        and all(isinstance(p, list) for p in payload)
    ):
        return [item for page in payload for item in page]
    return payload if isinstance(payload, list) else []


def process_resync_pr(
    repo: str,
    pr: str,
    eval_sha: str,
    meta: dict[str, Any],
    runner: Runner,
    renderer: Callable[..., RenderResult] = render_and_compare,
) -> bool:
    """Evaluate one resync PR; approve iff every condition holds. Returns True
    iff a new approval was posted."""
    ok, message = check_meta(pr, meta, repo, eval_sha)
    if not ok:
        print(message)
        return False
    head_sha = str((meta.get("head") or {}).get("sha"))
    suite = marker_suite_version(meta.get("body"))
    resolved_at = marker_resolved_at(meta.get("body"))
    if not suite or not resolved_at:
        print(f"PR #{pr}: no conformance-resync marker in the body — skipping.")
        return False

    commits = _flatten(
        _gh_json(
            ["api", f"repos/{repo}/pulls/{pr}/commits", "--paginate", "--slurp"],
            runner,
            what=f"listing commits for PR #{pr}",
        )
    )
    ok, message, parent_sha = check_commits(pr, commits, head_sha)
    if not ok:
        print(message)
        return False

    base_ref = str((meta.get("base") or {}).get("ref") or "")
    compare = _gh_json(
        ["api", f"repos/{repo}/compare/{parent_sha}...{base_ref}"],
        runner,
        what=f"checking PR #{pr}'s base ancestry",
    )
    if not isinstance(compare, dict) or compare.get("status") not in {
        "identical",
        "ahead",
    }:
        print(f"PR #{pr}: its parent commit is not in {base_ref}'s history — skipping.")
        return False

    ok, why = parent_preconditions(repo, parent_sha, suite, runner)
    if not ok:
        print(f"PR #{pr}: {why} — skipping.")
        return False

    print(
        f"PR #{pr}: re-rendering bootstrap --resync at conformance {suite} "
        f"on {parent_sha[:12]}..."
    )
    result = renderer(repo, parent_sha, head_sha, suite, resolved_at, runner)
    if result.lost:
        print(
            f"PR #{pr}: the render drops per-repo settings in "
            f"{sorted(result.lost)} — skipping."
        )
        return False
    if not result.tree_matches:
        print(f"PR #{pr}: {result.note or 'render does not match the PR'} — skipping.")
        return False
    print(f"PR #{pr}: PR tree is byte-identical to the independent render.")

    checks = runner(
        ["gh", "pr", "checks", pr, "--repo", repo, "--required"],
        capture_output=True,
        text=True,
        check=False,
    )
    for stream in (checks.stdout, checks.stderr):
        if stream and stream.strip():
            print(stream.rstrip())
    if checks.returncode != 0:
        print(f"PR #{pr}: required checks not yet all green — skipping.")
        return False

    reviews = _flatten(
        _gh_json(
            ["api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate", "--slurp"],
            runner,
            what=f"listing reviews for PR #{pr}",
        )
    )
    if count_resync_approvals(reviews, head_sha):
        print(
            f"PR #{pr}: already approved at this head with the resync signature — skipping."
        )
        return False

    live = _gh_json(
        ["api", f"repos/{repo}/pulls/{pr}"],
        runner,
        what=f"re-reading PR #{pr}'s head",
    )
    if not isinstance(live, dict) or (live.get("head") or {}).get("sha") != head_sha:
        print(f"PR #{pr}: HEAD moved during verification — skipping.")
        return False
    runner(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr}/reviews",
            "-X",
            "POST",
            "-f",
            f"commit_id={head_sha}",
            "-f",
            "event=APPROVE",
            "-f",
            f"body={RESYNC_APPROVAL_BODY}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    print(f"✅ Approved PR #{pr} as atlan-ci (conformance resync auto-approval).")
    return True
