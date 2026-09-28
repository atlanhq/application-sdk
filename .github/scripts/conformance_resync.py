#!/usr/bin/env python3
"""Conformance resync lane (FND-2848/FND-2868) — deterministic, no AI.

Run by ``.github/workflows/conformance-resync.yml`` with the
atlan-conformance-sync GitHub App token in ``GH_TOKEN``. For every
Renovate-onboarded fleet repo (``discover_org_consumers.discover_fleet`` — the
same roster ``renovate.yaml`` builds) it:

  1. skips repos with no ``renovate.json`` on main (the roster already filters
     these) and repos whose main ``uv.lock`` resolves no conformance package
     (recorded, not an error);
  2. shallow-clones latest ``main`` and runs, at exactly the pinned version,
     ``atlan-application-sdk-conformance bootstrap --resync --json``;
  3. stages only the paths the manifest's ``touched`` list names, set-compares
     every ``.bak`` against its replacement (a resync that would drop a
     per-repo setting HOLDS the repo — no PR opened or updated) and never
     commits a ``.bak``;
  4. keeps exactly one PR per repo on ``resync_approval_conditions.RESYNC_BRANCH``,
     authored by ``resync_approval_conditions.RESYNC_AUTHOR``: force-pushed in
     place from the fresh render, closed when main already carries the
     changes, and any duplicate lane PR closed;
  5. arms auto-merge only when the lane switch is on AND the repo's
     ``renovate.json`` lets Renovate auto-merge;
  6. for an existing lane PR this run did NOT touch, whose required checks are
     green and which atlan-ci has not yet approved with the resync signature,
     dispatches that repo's own ``renovate-auto-approve.yml`` by
     ``workflow_dispatch(pr_number=...)`` — the app repos' approver only
     listens for ``workflow_run`` on ``renovate/**`` branches, and
     ``bot/conformance-resync`` is not re-rendered by this lane, so nothing
     else would ever ask it to look at these PRs.

The byte-for-byte proof that a lane PR is safe to approve lives in
``resync_approval_conditions.py`` (the atlan-ci gate in this same repo); this
script imports its shared constants and staging/compare helpers rather than
keeping a second copy that could drift out of sync with what the gate expects.

Usage:
    python3 .github/scripts/conformance_resync.py [--repos owner/a,owner/b]
        [--dry-run] [--out results.json] [--diffs-dir diffs/]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import discover_org_consumers as discover  # noqa: E402
import resync_approval_conditions as gate  # noqa: E402

Runner = Callable[..., subprocess.CompletedProcess]

DEFAULT_OWNER = "atlanhq"
EXCLUDE_REPOS = {"atlanhq/application-sdk"}
BASE_BRANCH = "main"
RESYNC_LABEL = "conformance-resync"
APPROVE_WORKFLOW = "renovate-auto-approve.yml"
REQUEST_TIMEOUT = 60
BOOTSTRAP_TIMEOUT = 600
_REPO_RE = re.compile(r"^atlanhq/[A-Za-z0-9._-]+$")


class LaneError(RuntimeError):
    """A gh/git call the lane cannot recover from — aborts that repo only."""


# ── GitHub / git plumbing ────────────────────────────────────────────────


def _run(args: list[str], runner: Runner, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    return runner(args, **kwargs)


def gh_json(args: list[str], runner: Runner, *, what: str) -> object:
    result = _run(["gh", *args], runner)
    if result.returncode != 0:
        raise LaneError(f"{what}: {(result.stderr or '').strip()[-300:]}")
    try:
        return json.loads(result.stdout or "null")
    except ValueError as exc:
        raise LaneError(f"{what}: unparseable response") from exc


def _flatten(payload: object) -> list:
    if (
        isinstance(payload, list)
        and payload
        and all(isinstance(p, list) for p in payload)
    ):
        return [item for page in payload for item in page]
    return payload if isinstance(payload, list) else []


def read_repo_file(repo: str, path: str, runner: Runner) -> str | None:
    """``path`` off ``repo``'s default branch, or ``None`` if it does not exist."""
    result = _run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo}/contents/{path}",
        ],
        runner,
    )
    if result.returncode != 0:
        if "HTTP 404" in (result.stderr or ""):
            return None
        raise LaneError(
            f"reading {repo}/{path}: {(result.stderr or '').strip()[-300:]}"
        )
    return result.stdout


def _git_env() -> dict[str, str]:
    """Token in env only, never on argv or in a remote URL — mirrors
    ``resync_approval_conditions._git_env``."""
    token = os.environ.get("GH_TOKEN", "")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def git(args: list[str], cwd: str, runner: Runner, *, check: bool = True) -> str:
    result = _run(["git", *args], runner, cwd=cwd, env=_git_env())
    if check and result.returncode != 0:
        raise LaneError(f"git {args[0]} failed: {(result.stderr or '')[-400:]}")
    return result.stdout or ""


def bot_identity(login: str, runner: Runner) -> tuple[str, str]:
    """(login, no-reply email) for the App's bot user — needed so the lane's
    single commit is authored as ``resync_approval_conditions.RESYNC_AUTHOR``,
    which ``check_commits`` (the approval gate) requires."""
    user = gh_json(
        ["api", f"/users/{urllib.parse.quote(login)}"],
        runner,
        what="resolving bot identity",
    )
    return login, f"{(user or {}).get('id')}+{login}@users.noreply.github.com"


# ── Roster ────────────────────────────────────────────────────────────────


def discover_roster(scoped: list[str], runner: Runner) -> list[str]:
    """Renovate-onboarded fleet repos, the same way ``renovate.yaml`` builds
    them — repos matching the atlan-*-app pattern whose ``renovate.json`` on
    main extends the shared preset (which also means a repo with no
    ``renovate.json`` on main is already excluded here)."""

    def _run_for_discover(args: list[str]) -> tuple[int, str, str]:
        result = _run(["gh", *args], runner)
        return result.returncode, result.stdout, result.stderr

    fleet = discover.discover_fleet(
        DEFAULT_OWNER,
        discover.DEFAULT_NAME_PATTERN,
        discover.PRESET_MARKER,
        EXCLUDE_REPOS,
        run=_run_for_discover,
    )
    if scoped:
        fleet = sorted(set(fleet) & set(scoped))
    return fleet


def resync_eligibility(pinned: str | None) -> tuple[bool, str]:
    """Whether a repo is resynced this run — it runs at exactly the
    conformance version main's ``uv.lock`` pins, latest or not. A repo whose
    ``uv.lock`` resolves no conformance package is ignored, reason recorded."""
    if not pinned:
        return False, (
            "ignored: main's uv.lock does not resolve "
            f"{gate.CONFORMANCE_PACKAGE} — nothing to render the scaffolds from"
        )
    return True, ""


def automerge_allowed(renovate_json_text: str | None) -> tuple[bool, str]:
    """True only when the repo's renovate.json lets Renovate auto-merge.

    Reuses ``discover_org_consumers.automerge_mode`` (the same classifier the
    fleet discovery already runs) rather than a second copy of the same
    soft/auto/unknown rules, but fails closed on top of it: only ``"auto"``
    arms anything here, whereas the discovery step's own use of ``"unknown"``
    is informational.
    """
    if not renovate_json_text:
        return False, "renovate.json unreadable on main"
    mode = discover.automerge_mode(renovate_json_text)
    if mode == "auto":
        return True, ""
    if mode == "soft":
        return False, "renovate.json is in soft mode (auto-merge disabled)"
    return False, "renovate.json could not be classified — failing closed"


# ── One PR per repo ──────────────────────────────────────────────────────


def open_prs(repo: str, runner: Runner) -> list[dict]:
    return _flatten(
        gh_json(
            [
                "api",
                f"repos/{repo}/pulls?state=open&per_page=100",
                "--paginate",
                "--slurp",
            ],
            runner,
            what=f"listing open PRs for {repo}",
        )
    )


def split_lane_prs(prs: list[dict]) -> tuple[dict | None, list[dict], dict | None]:
    """(keep, duplicates, foreign) among ``repo``'s open PRs.

    ``keep`` is the lane's own open PR on ``RESYNC_BRANCH``; any other PR the
    lane's author opened is a duplicate (should not normally happen — this App
    only ever pushes that one branch — but closed on sight if it does).
    ``foreign`` is someone else's open PR sitting on ``RESYNC_BRANCH`` itself;
    the lane never force-pushes over a person's PR, so a repo with one is left
    alone entirely this run.
    """
    keep: dict | None = None
    dupes: list[dict] = []
    foreign: dict | None = None
    for pr in prs:
        head = pr.get("head") or {}
        author = (pr.get("user") or {}).get("login")
        same_repo = (head.get("repo") or {}).get("full_name") == repo_of(pr)
        if head.get("ref") == gate.RESYNC_BRANCH:
            if author == gate.RESYNC_AUTHOR and same_repo:
                keep = pr
            else:
                foreign = pr
        elif author == gate.RESYNC_AUTHOR and same_repo:
            dupes.append(pr)
    return keep, dupes, foreign


def repo_of(pr: dict) -> str | None:
    return (pr.get("base") or {}).get("repo", {}).get("full_name")


def close_pr(
    repo: str, pr: dict, reason: str, runner: Runner, *, delete_branch: bool
) -> None:
    number = pr["number"]
    gh_json(
        [
            "api",
            f"repos/{repo}/issues/{number}/comments",
            "-X",
            "POST",
            "-f",
            f"body={reason}",
        ],
        runner,
        what=f"commenting on PR #{number}",
    )
    _run(["gh", "pr", "close", str(number), "--repo", repo], runner, check=True)
    ref = (pr.get("head") or {}).get("ref")
    if delete_branch and ref:
        _run(
            [
                "gh",
                "api",
                "-X",
                "DELETE",
                f"repos/{repo}/git/refs/heads/{urllib.parse.quote(ref)}",
            ],
            runner,
        )


def set_automerge(repo: str, pr_number: int, enable: bool, runner: Runner) -> str:
    """Arm/disarm GitHub-native auto-merge. Never fatal — a repo on a merge
    queue refuses a merge-method flag, and a refusal is reported, not raised."""
    flag = "--auto" if enable else "--disable-auto"
    result = _run(["gh", "pr", "merge", str(pr_number), "--repo", repo, flag], runner)
    if result.returncode != 0:
        return f"refused ({(result.stderr or result.stdout or '').strip()[-160:]})"
    return "armed" if enable else "disarmed"


# ── PR text ──────────────────────────────────────────────────────────────


def pr_title(suite_version: str) -> str:
    return (
        f"chore(conformance): resync bootstrap scaffolds to conformance {suite_version}"
    )


def render_pr_body(
    *,
    suite_version: str,
    resolved_at: str,
    touched: list[str],
    lost: dict[str, list[str]],
    automerge: bool,
    automerge_reason: str,
) -> str:
    lines = [
        gate.pr_marker(suite_version, resolved_at),
        "",
        "Re-renders this repo's bootstrap-managed scaffolds from the canonical "
        f"templates in `{gate.CONFORMANCE_PACKAGE}=={suite_version}` — the exact "
        "version this repo's `uv.lock` pins on `main`:",
        "",
        "```",
        f"{gate.CONFORMANCE_PACKAGE} bootstrap --resync --json",
        "```",
        "",
        "**Files written** (only these are committed):",
        "",
        *[f"- `{p}`" for p in touched],
        "",
    ]
    if lost:
        lines += [
            "> [!WARNING]",
            "> This should never ship — a resync that drops a per-repo setting is",
            "> HELD before a PR is ever opened. If you are reading this, report it.",
            "",
        ]
        for path, missing in sorted(lost.items()):
            lines.append(f"- `{path}`")
            lines += [f"  - `{m}`" for m in missing]
    else:
        lines += [
            "Every `.bak` bootstrap wrote was set-compared against the file that "
            "replaced it (non-comment lines) — no per-repo setting was lost. "
            "Backups are never committed.",
            "",
        ]
    lines += [
        "**Auto-merge:** "
        + (
            "armed — this repo's `renovate.json` lets Renovate auto-merge."
            if automerge
            else f"not armed — {automerge_reason}."
        ),
        "",
        "This is the repo's single conformance-resync PR. The lane re-renders it "
        f"from latest `{BASE_BRANCH}` on every run and closes it once `{BASE_BRANCH}` "
        "already carries the changes. Please don't push to this branch — the "
        "next run force-pushes over it.",
        "",
        "Opened by application-sdk `conformance-resync.yml`.",
    ]
    return "\n".join(lines) + "\n"


# ── Approval dispatch (new: FND-2868) ────────────────────────────────────


def should_dispatch_approval(
    pr: dict | None, checks_ok: bool, reviews: list[dict]
) -> bool:
    """Whether the lane should nudge this repo's own ``renovate-auto-approve.yml``
    to look at ``pr`` right now.

    Pure and independent of any subprocess: the caller resolves ``checks_ok``
    (``gh pr checks --required``) and ``reviews`` (the PR's review list), so
    this is unit-testable without gh/network. All three must hold:
    the PR is open, its required checks are all completed and green, and
    atlan-ci has not already approved this exact head with the resync
    signature (``resync_approval_conditions.count_resync_approvals``).
    """
    if not pr or pr.get("state") != "open":
        return False
    head_sha = (pr.get("head") or {}).get("sha")
    if not head_sha or not checks_ok:
        return False
    return gate.count_resync_approvals(reviews, head_sha) == 0


def choose_resolved_at(keep: dict | None, pinned: str, now: str) -> str:
    """Reuse the open PR's resolution timestamp while it renders the same
    suite, so an unchanged PR re-renders identically and its body stays put."""
    body = (keep or {}).get("body")
    if gate.marker_suite_version(body) == pinned and gate.marker_resolved_at(body):
        return str(gate.marker_resolved_at(body))
    return now


def pr_matches_render(
    repo: str, pr_number: int, staged: list[str], work: str, runner: Runner
) -> bool:
    """The PR changes exactly the rendered paths, with the rendered content.
    Compared per path, not per tree, so unrelated commits on main never force
    a re-push."""
    files = _flatten(
        gh_json(
            ["api", f"repos/{repo}/pulls/{pr_number}/files", "--paginate", "--slurp"],
            runner,
            what="listing lane PR files",
        )
    )
    changed = sorted(str(f.get("filename")) for f in files if isinstance(f, dict))
    if changed != sorted(staged):
        return False
    for path in staged:
        ours = git(["rev-parse", f"HEAD:{path}"], work, runner, check=False).strip()
        theirs = git(["rev-parse", f"FETCH_HEAD:{path}"], work, runner, check=False).strip()
        if not ours or ours != theirs:
            return False
    return True


def withdraw_lane_pr(
    repo: str, keep: dict | None, reason: str, dry_run: bool, runner: Runner, result: dict
) -> None:
    """A held or ineligible repo must not keep an approvable lane PR open."""
    if not keep:
        return
    if not dry_run:
        close_pr(
            repo,
            keep,
            f"Closing: {reason}. The lane opens a fresh PR once the repo is eligible again.",
            runner,
            delete_branch=True,
        )
    result["trace"].append(
        f"{'would close' if dry_run else 'closed'} lane PR #{keep['number']} ({reason})."
    )
    result["closed"] = keep.get("number")


def lane_commits_ok(repo: str, pr_number: int, head_sha: str, runner: Runner) -> bool:
    commits = _flatten(
        gh_json(
            ["api", f"repos/{repo}/pulls/{pr_number}/commits", "--paginate", "--slurp"],
            runner,
            what="reading lane PR commits",
        )
    )
    ok, _, _ = gate.check_commits(str(pr_number), commits, head_sha)
    return ok


def checks_all_green(repo: str, pr_number: int, runner: Runner) -> bool:
    result = _run(
        ["gh", "pr", "checks", str(pr_number), "--repo", repo, "--required"], runner
    )
    return result.returncode == 0


def dispatch_approval(repo: str, pr_number: int, runner: Runner) -> str:
    """Empty string on success, else the failure reason."""
    result = _run(
        [
            "gh",
            "workflow",
            "run",
            APPROVE_WORKFLOW,
            "-R",
            repo,
            "-f",
            f"pr_number={pr_number}",
        ],
        runner,
    )
    if result.returncode == 0:
        return ""
    return (result.stderr or "").strip()[-300:] or f"gh exited {result.returncode}"


# ── One repo ─────────────────────────────────────────────────────────────


def run_bootstrap(workdir: str, version: str, resolved_at: str) -> tuple[int, str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}}
    proc = subprocess.run(
        gate.resync_command(version, resolved_at),
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
        timeout=BOOTSTRAP_TIMEOUT,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def process_repo(
    repo: str,
    *,
    identity: tuple[str, str],
    resolved_now: str,
    dry_run: bool,
    automerge_enabled: bool,
    diffs_dir: pathlib.Path | None,
    runner: Runner,
) -> dict:
    result: dict = {"repo": repo, "trace": []}
    would = "would " if dry_run else ""

    def step(text: str) -> None:
        result["trace"].append(text)

    prs = open_prs(repo, runner)
    keep, dupes, foreign = split_lane_prs(prs)
    if foreign:
        step(
            f"#{foreign['number']} on {gate.RESYNC_BRANCH} was opened by someone else "
            "— never force-pushing over a person's PR, leaving the repo alone."
        )
        result.update(
            action="skipped",
            reason=f"foreign PR #{foreign['number']} on {gate.RESYNC_BRANCH}",
        )
        return result

    for d in dupes:
        if not dry_run:
            close_pr(
                repo,
                d,
                f"Closing: duplicate conformance-resync PR. The lane keeps one PR per repo on `{gate.RESYNC_BRANCH}`.",
                runner,
                delete_branch=(d.get("head") or {}).get("ref") != gate.RESYNC_BRANCH,
            )
        step(f"Closed duplicate lane PR #{d['number']} ({would}closed).")
    result["duplicatesClosed"] = [d["number"] for d in dupes]

    renovate_json = read_repo_file(repo, "renovate.json", runner)
    uv_lock = read_repo_file(repo, "uv.lock", runner)
    pinned = gate.pinned_conformance(uv_lock) if uv_lock else None
    result["pinned"] = pinned

    ok, why = resync_eligibility(pinned)
    pushed_this_run = False
    if not ok:
        step(why)
        result.update(action="skipped", reason=why)
        withdraw_lane_pr(repo, keep, why, dry_run, runner, result)
        return result

    resolved_at = choose_resolved_at(keep, pinned, resolved_now)
    result["resolvedAt"] = resolved_at

    automerge, automerge_reason = automerge_allowed(renovate_json)
    if automerge and not automerge_enabled:
        automerge, automerge_reason = False, "auto-merge is switched off for this lane"

    with tempfile.TemporaryDirectory(prefix="resync-") as tmp:
        work = os.path.join(tmp, "repo")
        os.makedirs(work)
        git(["init", "-q", "-b", BASE_BRANCH], work, runner)
        git(["remote", "add", "origin", f"https://github.com/{repo}"], work, runner)
        git(
            ["fetch", "-q", "--depth", "1", "origin", f"refs/heads/{BASE_BRANCH}"],
            work,
            runner,
        )
        git(["checkout", "-q", "-B", BASE_BRANCH, "FETCH_HEAD"], work, runner)
        base_sha = git(["rev-parse", "HEAD"], work, runner).strip()
        step(f"Cloned latest `{BASE_BRANCH}` at {base_sha[:12]}.")

        rc, out, err = run_bootstrap(work, pinned, resolved_at)
        manifest = gate.parse_manifest(out)
        if rc != 0 or manifest is None:
            result.update(
                action="error", reason=f"bootstrap exited {rc}: {(err or out)[-400:]}"
            )
            return result
        if manifest.get("skipped"):
            result.update(
                action="skipped", reason="bootstrap reported the repo as out of scope"
            )
            withdraw_lane_pr(
                repo, keep, "bootstrap reports the repo out of scope", dry_run, runner, result
            )
            return result

        lost = gate.stage_like_the_lane(work, manifest, runner)
        touched = gate.safe_touched(manifest, pathlib.Path(work))
        staged = git(["diff", "--cached", "--name-only"], work, runner).split()
        result["files"] = staged
        step(
            f"Ran bootstrap at {pinned}: {len(touched)} path(s) touched, "
            f"{len(staged)} differ from main."
        )

        if lost:
            step(f"HELD — settings would be lost: {lost}.")
            result.update(
                action="held",
                reason="resync would drop per-repo settings: "
                + "; ".join(f"{p}: {', '.join(m)}" for p, m in sorted(lost.items())),
            )
            withdraw_lane_pr(
                repo, keep, "the resync would drop per-repo settings", dry_run, runner, result
            )
            return result

        if diffs_dir is not None and staged:
            diffs_dir.mkdir(parents=True, exist_ok=True)
            patch = git(["diff", "--cached", "--binary"], work, runner)
            (diffs_dir / f"{repo.split('/', 1)[1]}.patch").write_text(patch)
            result["diffStat"] = git(
                ["diff", "--cached", "--shortstat"], work, runner
            ).strip()

        if not staged:
            if keep and not dry_run:
                close_pr(
                    repo,
                    keep,
                    f"Closing: `{BASE_BRANCH}` already matches the conformance {pinned} scaffolds.",
                    runner,
                    delete_branch=True,
                )
            step("No diff against latest main → in sync.")
            result.update(
                action="in_sync",
                reason="no diff against latest main",
                closed=(keep or {}).get("number"),
            )
            return result

        name, email = identity
        git(
            [
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-q",
                "-m",
                f"{pr_title(pinned)}\n\nFND-2848",
            ],
            work,
            runner,
        )

        remote_line = git(
            ["ls-remote", "origin", f"refs/heads/{gate.RESYNC_BRANCH}"], work, runner
        ).strip()
        remote_sha = remote_line.split()[0] if remote_line else ""
        same_content = False
        if remote_sha and keep:
            git(
                [
                    "fetch",
                    "-q",
                    "--depth",
                    "1",
                    "origin",
                    f"refs/heads/{gate.RESYNC_BRANCH}",
                ],
                work,
                runner,
            )
            same_content = pr_matches_render(repo, keep["number"], staged, work, runner)
            if same_content and not lane_commits_ok(
                repo, keep["number"], remote_sha, runner
            ):
                same_content = False
            if same_content:
                detail = gh_json(
                    ["api", f"repos/{repo}/pulls/{keep['number']}"],
                    runner,
                    what="checking mergeability",
                )
                if isinstance(detail, dict) and detail.get("mergeable") is False:
                    same_content = False

        if dry_run:
            step(
                f"{would}{'leave the PR alone' if same_content and keep else 'push and open/update the PR'}."
            )
            result.update(
                action="would-leave-unchanged"
                if (same_content and keep)
                else "would-update"
                if keep
                else "would-open"
            )
            return result

        if not (same_content and keep):
            lease = f"--force-with-lease=refs/heads/{gate.RESYNC_BRANCH}:{remote_sha}"
            git(
                [
                    "push",
                    "-q",
                    lease,
                    "origin",
                    f"HEAD:refs/heads/{gate.RESYNC_BRANCH}",
                ],
                work,
                runner,
            )
            pushed_this_run = True
            step("Force-pushed the fresh render onto the lane branch.")
        else:
            step(
                f"PR #{keep['number']} already carries identical content — left alone."
            )

    title = pr_title(pinned)
    body = render_pr_body(
        suite_version=pinned,
        resolved_at=resolved_at,
        touched=staged,
        lost={},
        automerge=automerge,
        automerge_reason=automerge_reason,
    )
    if keep:
        if keep.get("title") != title or keep.get("body") != body:
            gh_json(
                [
                    "api",
                    f"repos/{repo}/pulls/{keep['number']}",
                    "-X",
                    "PATCH",
                    "-f",
                    f"title={title}",
                    "-f",
                    f"body={body}",
                ],
                runner,
                what="updating lane PR",
            )
        pr = gh_json(
            ["api", f"repos/{repo}/pulls/{keep['number']}"],
            runner,
            what="reading lane PR",
        )
        action = "pr_unchanged" if not pushed_this_run else "pr_updated"
    else:
        pr = gh_json(
            [
                "api",
                f"repos/{repo}/pulls",
                "-X",
                "POST",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
                "-f",
                f"head={gate.RESYNC_BRANCH}",
                "-f",
                f"base={BASE_BRANCH}",
            ],
            runner,
            what="opening lane PR",
        )
        action = "pr_opened"
    pr = pr if isinstance(pr, dict) else {}
    if pr.get("number") is not None:
        _run(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr['number']}/labels",
                "-X",
                "POST",
                "-f",
                f"labels[]={RESYNC_LABEL}",
            ],
            runner,
        )
    result.update(
        action=action,
        pr=pr.get("html_url"),
        automerge=set_automerge(repo, pr["number"], automerge, runner)
        if pr.get("number")
        else None,
        automergeReason=automerge_reason,
    )
    _maybe_dispatch(
        repo,
        pr if pushed_this_run else keep,
        dry_run,
        runner,
        result,
        skip_if_pushed=pushed_this_run,
    )
    return result


def _maybe_dispatch(
    repo: str,
    pr: dict | None,
    dry_run: bool,
    runner: Runner,
    result: dict,
    *,
    skip_if_pushed: bool = False,
) -> None:
    """Approval-dispatch pass: only for a PR this run left untouched."""
    if skip_if_pushed or not pr or pr.get("state") != "open":
        return
    number = pr.get("number")
    if not number:
        return
    checks_ok = checks_all_green(repo, number, runner)
    reviews = _flatten(
        gh_json(
            ["api", f"repos/{repo}/pulls/{number}/reviews", "--paginate", "--slurp"],
            runner,
            what="listing reviews",
        )
    )
    if should_dispatch_approval(pr, checks_ok, reviews):
        if not dry_run:
            failure = dispatch_approval(repo, number, runner)
            if failure:
                result["trace"].append(
                    f"could not dispatch {APPROVE_WORKFLOW} for PR #{number}: {failure}"
                )
                result["approvalDispatchError"] = failure
                return
        result["trace"].append(
            f"{'would dispatch' if dry_run else 'dispatched'} {APPROVE_WORKFLOW} for PR #{number} "
            "(checks green, not yet approved, head unchanged this run)."
        )
        result["approvalDispatched"] = not dry_run


# ── Driver ────────────────────────────────────────────────────────────────


def _summary(results: list[dict], dry_run: bool) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.get("action", "?")] = counts.get(r.get("action", "?"), 0) + 1
    lines = [
        f"## Conformance resync{' (dry run)' if dry_run else ''}",
        "",
        " · ".join(f"**{k}** {v}" for k, v in sorted(counts.items())),
        "",
        "| Repo | Action | Pin | PR | Auto-merge | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        notes = [r.get("reason") or ""]
        if r.get("approvalDispatched"):
            notes.append("approval dispatched")
        if r.get("duplicatesClosed"):
            notes.append(
                "closed duplicates: "
                + ", ".join(f"#{n}" for n in r["duplicatesClosed"])
            )
        note = "; ".join(n for n in notes if n).replace("|", "\\|")
        lines.append(
            f"| `{r['repo']}` | {r.get('action', '?')} | {r.get('pinned') or ''} | "
            f"{r.get('pr') or ''} | {r.get('automerge') or ''} | {note} |"
        )
    table = "\n".join(lines) + "\n"
    if not dry_run:
        return table
    blocks = ["", "### Per-repo trace", ""]
    for r in results:
        trace = r.get("trace") or []
        if not trace:
            continue
        head = f"<code>{r['repo']}</code> — {r.get('action', '?')}"
        blocks.append(f"<details><summary>{head}</summary>\n")
        blocks += [f"{i}. {t}" for i, t in enumerate(trace, 1)]
        blocks.append("\n</details>\n")
    return table + "\n".join(blocks) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--repos",
        default="",
        help="Comma-separated owner/repo scope (default: full fleet)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "true").lower() == "true",
    )
    p.add_argument("--out", default="resync-results.json")
    p.add_argument("--diffs-dir", default="resync-diffs")
    args = p.parse_args()

    if not os.environ.get("GH_TOKEN"):
        print("GH_TOKEN is required", file=sys.stderr)
        return 2

    raw_repos = args.repos or os.environ.get("REPOS", "")
    scoped: list[str] = []
    stripped = raw_repos.strip()
    if stripped:
        if stripped.startswith("["):
            try:
                scoped = [r for r in json.loads(stripped) if isinstance(r, str)]
            except ValueError:
                print(f"REPOS is not valid JSON: {stripped!r}", file=sys.stderr)
                return 2
        else:
            scoped = [r.strip() for r in stripped.split(",") if r.strip()]
    bad = [r for r in scoped if not _REPO_RE.match(r)]
    if bad:
        print(f"refusing malformed repo names: {bad}", file=sys.stderr)
        return 2

    runner = subprocess.run
    automerge_enabled = os.environ.get("AUTOMERGE_ENABLED", "false").lower() == "true"
    identity = (
        (gate.RESYNC_AUTHOR, f"{gate.RESYNC_AUTHOR}@users.noreply.github.com")
        if args.dry_run
        else bot_identity(gate.RESYNC_AUTHOR, runner)
    )
    try:
        roster = discover_roster(scoped, runner)
    except discover.DiscoveryError as exc:
        print(f"::error::fleet discovery failed: {exc}", file=sys.stderr)
        return 1

    diffs_dir = pathlib.Path(args.diffs_dir) if args.dry_run else None
    resolved_now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    results: list[dict] = []
    for repo in roster:
        try:
            r = process_repo(
                repo,
                identity=identity,
                resolved_now=resolved_now,
                dry_run=args.dry_run,
                automerge_enabled=automerge_enabled,
                diffs_dir=diffs_dir,
                runner=runner,
            )
        except Exception as e:  # noqa: BLE001 — one repo never sinks the fleet run
            r = {"repo": repo, "action": "error", "reason": str(e)[:400]}
        print(json.dumps(r))
        results.append(r)

    pathlib.Path(args.out).write_text(
        json.dumps(
            {
                "dryRun": args.dry_run,
                "ranAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "results": results,
            },
            indent=2,
        )
    )
    summary = _summary(results, args.dry_run)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary)
    errors = sum(1 for r in results if r.get("action") == "error")
    return 1 if errors and errors == len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
