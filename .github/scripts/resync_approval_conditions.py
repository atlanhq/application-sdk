#!/usr/bin/env python3
"""Approval gate for conformance-resync PRs — the atlan-ci code-owner review.

connector-pulse's conformance resync lane (``conformance-resync.yml``) keeps one
PR per app repo on ``bot/conformance-resync``, authored by the connectivity-ai
App, carrying ``atlan-application-sdk-conformance bootstrap --resync`` output:
the re-rendered tests.yaml / renovate.json / managed workflow shims / review
kit. Those PRs rewrite ``.github/workflows/*`` files, so the Renovate gate in
``renovate_approval_conditions.py`` correctly refuses them (pin-only workflow
diffs, FND-1996). This module is the separate, narrower path for them.

**Identity selects; content proves.** Author and branch only decide which PRs
reach this gate — the connectivity-ai App also runs the AI remediation lane, and
a branch name is something anyone with push access can create, so neither is
trusted as evidence. The approval rests on re-rendering the PR independently
here and requiring a byte-identical result:

  a. author is ``connectivity-ai[bot]``, head branch is exactly
     ``bot/conformance-resync`` in this same repo, PR open and not a draft,
     current HEAD is the SHA under evaluation
  b. the body carries the lane's marker, naming the suite version it rendered
  c. exactly one commit on the PR, authored by the lane, with one parent
  d. that parent is in the base branch's history (the render base is real main)
  e. re-render: check out the parent, read the conformance version its
     ``uv.lock`` resolves (must equal the marker), run ``bootstrap --resync
     --json`` at exactly that version, stage exactly what the lane stages, and
     require the resulting git tree to EQUAL the PR head's tree — any extra,
     missing or altered byte anywhere withholds the approval
  f. the re-render dropped no per-repo setting (``.bak`` set-compare)
  g. every ruleset-required check is green
  h. atlan-ci has not already approved this head with the resync signature

The staging rules in :func:`stage_like_the_lane` must stay in lockstep with
connector-pulse ``scripts/conformance_resync.py``; a drift makes the trees
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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Constants other automation keys off (connector-pulse's lane + dashboard).
# Changing any of these is a cross-repo change.
RESYNC_AUTHOR = "connectivity-ai[bot]"
RESYNC_BRANCH = "bot/conformance-resync"
RESYNC_SIGNATURE = "**Conformance resync auto-approval:**"
APPROVER_LOGIN = "atlan-ci"
CONFORMANCE_PACKAGE = "atlan-application-sdk-conformance"
_MARKER_RE = re.compile(r"<!--\s*conformance-resync-lane\s+suite=(\d+\.\d+\.\d+)\s*-->")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
BOOTSTRAP_TIMEOUT = 600

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


def lost_setting_lines(backup_text: str, new_text: str) -> list[str]:
    """Non-comment lines in the ``.bak`` absent from its replacement
    (reorder-immune). Mirrors connector-pulse's lane."""
    new = {_normalise(x) for x in new_text.splitlines()}
    lost: list[str] = []
    for line in backup_text.splitlines():
        norm = _normalise(line)
        if not norm or norm.startswith("#") or norm.startswith("//"):
            continue
        if norm in {"{", "}", "[", "]", "},", "],"}:
            continue
        if norm not in new and line.strip() not in lost:
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
    ``work``; return lost settings. Must match connector-pulse
    ``scripts/conformance_resync.py``."""
    root = pathlib.Path(work)
    lost: dict[str, list[str]] = {}
    backups = sorted(
        p for p in root.rglob("*.bak") if ".git" not in p.relative_to(root).parts
    )
    for bak in backups:
        original = bak.with_suffix("")
        if original.exists():
            missing = lost_setting_lines(
                bak.read_text(encoding="utf-8", errors="replace"),
                original.read_text(encoding="utf-8", errors="replace"),
            )
            if missing:
                lost[str(original.relative_to(root))] = missing
        bak.unlink()
    touched = safe_touched(manifest, root)
    if touched:
        _git(["add", "-A", "-f", "--", *touched], work, runner)
    return lost


def render_and_compare(
    repo: str, parent_sha: str, head_sha: str, suite: str, runner: Runner
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
            [
                "uvx",
                "--isolated",
                "--no-config",
                "--from",
                f"{CONFORMANCE_PACKAGE}=={suite}",
                CONFORMANCE_PACKAGE,
                "bootstrap",
                "--resync",
                "--json",
            ],
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
    if not suite:
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

    print(
        f"PR #{pr}: re-rendering bootstrap --resync at conformance {suite} "
        f"on {parent_sha[:12]}..."
    )
    result = renderer(repo, parent_sha, head_sha, suite, runner)
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

    runner(
        [
            "gh",
            "pr",
            "review",
            pr,
            "--repo",
            repo,
            "--approve",
            "--body",
            RESYNC_APPROVAL_BODY,
        ],
        check=True,
    )
    print(f"✅ Approved PR #{pr} as atlan-ci (conformance resync auto-approval).")
    return True
