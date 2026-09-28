"""One triage run: ticket in, allowlist PR + bump PR + one ticket comment out."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import allowlist, bump, config, effects, report, scan
from .classify import Triage, UpstreamCheck, triage_ticket, version_key

# vuln-auto-merge.yml's workflow_run filter only wakes for these prefixes. A branch that
# drifts from them fails closed: no error, and no auto-merge.
ALLOWLIST_BRANCH = "chore/allowlist-"
BUMP_BRANCH = "fix/bump-"
VALIDATE_SCRIPT = ".github/scripts/validate_allowlist.py"


@dataclass
class Context:
    repo: str
    root: Path
    ticket: str
    severity: str = ""
    scan_run_id: str = ""
    scan_dir: Path | None = None  # pre-downloaded scan; skips the download
    run_url: str = ""
    run_id: str = ""
    dry_run: bool = False
    now: datetime = field(default_factory=lambda: datetime.now().astimezone())


@dataclass
class Deps:
    """The effects run() needs, swappable in tests."""

    runner: effects.Runner
    fetch_issue: Callable[[str], dict[str, Any]] = effects.fetch_issue
    comment: Callable[[str, str], None] = effects.comment
    upstream: UpstreamCheck | None = None


def _slug(ids: list[str], limit: int = 3) -> str:
    """A branch-name slug. When the list is truncated it ends in a short hash of the
    WHOLE list: two plans sharing their first ids must never share a branch, or the
    second would "reuse" the first one's PR without its own entries."""
    head = "-".join(i.lower() for i in ids[:limit])
    if len(ids) <= limit:
        return head
    digest = hashlib.sha256(",".join(ids).encode()).hexdigest()[:8]
    return f"{head}-and-{len(ids) - limit}-more-{digest}"


def _listing(items: list[str], limit: int = 3) -> str:
    """`a, b, c (+4 more)`: keeps a title short however many CVEs a ticket has (the
    full list is in the PR body). An over-long title would fail `gh pr create` after
    the push, before the ticket is told anything."""
    head = ", ".join(items[:limit])
    return head + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _uv_cause(stderr: str | None) -> str:
    """The lines of a failed `uv lock` that say why, without its trailing hints."""
    lines = [ln.strip(" ╰─▶×") for ln in (stderr or "").splitlines()]
    cause = [ln for ln in lines if ln and not ln.startswith("hint")][:3]
    return " / ".join(cause) or "(no output)"


MAX_BRANCH_CANDIDATES = 10


def _free_branch(
    ctx: Context, name: str, deps: Deps
) -> tuple[str, effects.OpenPR | None]:
    """(branch to push, the open PR already on it or None).

    Walks `name`, `name-r<run>`, `name-r<run>-2`, ... and stops at the first candidate
    that either has an open PR (reuse it) or does not exist on the remote (push to it).
    A leftover branch is never force-pushed over. Every candidate is checked for an
    open PR, so a re-run of the same job finds the suffixed PR its first attempt
    opened instead of pushing again onto that branch."""
    tag = ctx.run_id or ctx.now.strftime("%H%M%S")
    candidates = [name, f"{name}-r{tag}"] + [
        f"{name}-r{tag}-{i}" for i in range(2, MAX_BRANCH_CANDIDATES)
    ]
    for branch in candidates:
        pr = effects.open_pr(ctx.repo, branch, deps.runner)
        if pr:
            return branch, pr
        if not effects.remote_branch_exists(branch, deps.runner):
            return branch, None
    raise SystemExit(
        f"::error::no free branch name for {name} (tried {len(candidates)})"
    )


def _allowlist_pr(
    ctx: Context,
    cfg: config.Config,
    triages: list[Triage],
    detected,
    base: str,
    deps: Deps,
    out: report.Outcome,
) -> None:
    path = ctx.root / allowlist.ALLOWLIST_PATH
    data = json.loads(path.read_text())
    plan = allowlist.plan_entries(
        triages,
        data,
        detected=detected,
        today=ctx.now.date(),
        ticket=ctx.ticket,
        added_by=cfg.added_by,
    )
    out.allowlist_entries, out.allowlist_skipped = plan.entries, plan.skipped
    if not plan.entries or ctx.dry_run:
        return
    ids = sorted(plan.entries)
    branch, existing = _free_branch(ctx, ALLOWLIST_BRANCH + _slug(ids), deps)
    if existing:
        out.allowlist_pr = existing.url
        return
    effects.start_branch(branch, base, deps.runner)
    path.write_text(
        json.dumps(allowlist.apply(data, plan.entries, ctx.now.date()), indent=2) + "\n"
    )
    # The same validator CI runs; a failure here raises before anything is pushed.
    deps.runner(["python3", VALIDATE_SCRIPT], check=True)
    sevs = sorted({e["severity"] for e in plan.entries.values()})
    policy = allowlist.policy_of(data)
    sla = ", ".join(f"{s} {policy[s]}d" for s in sevs)
    title = f"chore(security): allowlist {_listing(ids)} ({sla} SLA)"
    effects.commit_and_push(branch, [allowlist.ALLOWLIST_PATH], title, deps.runner)
    lines = (
        [
            f"Starts the remediation SLA for {len(ids)} CVE(s) from {ctx.ticket}. "
            "Each entry expires on its SLA deadline, measured from first detection.",
            "",
            "| CVE | Package | Severity | Case | Expires |",
            "|---|---|---|---|---|",
        ]
        + [
            f"| `{c}` | `{e['package']}` | {e['severity']} | {e['case']} | {e['expires']} |"
            for c, e in sorted(plan.entries.items())
        ]
        + ["", f"Opened by the deterministic vuln triage: {ctx.run_url}"]
    )
    out.allowlist_pr = effects.create_pr(
        ctx.repo, branch, title, "\n".join(lines), [cfg.label], deps.runner
    )


def _bump_pr(
    ctx: Context,
    cfg: config.Config,
    triages: list[Triage],
    base: str,
    deps: Deps,
    out: report.Outcome,
) -> None:
    case1 = [t for t in triages if t.case == 1 and t.allowlistable]
    if not case1 or not cfg.bump_enabled:
        return
    targets: dict[str, str] = {}
    cves: dict[str, list[str]] = {}
    for t in case1:
        if t.package not in targets or version_key(t.bump_to) > version_key(
            targets[t.package]
        ):
            targets[t.package] = t.bump_to
        cves.setdefault(t.package, []).append(t.cve)
    pkgs = sorted(targets)
    summary = ", ".join(f"{p} to {targets[p]}" for p in pkgs)
    all_cves = sorted(c for cs in cves.values() for c in cs)
    if ctx.dry_run:
        out.bump_problem = f"(dry run) would bump {summary}."
        return
    branch, existing = _free_branch(
        ctx, BUMP_BRANCH + _slug(pkgs, 2) + "-" + ctx.ticket.lower(), deps
    )
    if existing:
        # Report what the open PR actually carries: one opened inside the cooldown has
        # no label and still needs a human.
        out.bump_pr = existing.url
        out.bump_labelled = cfg.label in existing.labels
        return

    effects.start_branch(branch, base, deps.runner)
    pyproject = ctx.root / "pyproject.toml"
    lock_path = ctx.root / "uv.lock"
    old_text = lock_path.read_text()
    old_lock = scan.load_lock(lock_path)
    try:
        pyproject.write_text(
            bump.set_constraints(
                pyproject.read_text(), {p: (targets[p], sorted(cves[p])) for p in pkgs}
            )
        )
    except bump.BumpError as e:
        out.bump_problem = str(e)
        effects.start_branch(branch, base, deps.runner)
        return
    res = effects.uv_lock_upgrade(pkgs, deps.runner)
    if res.returncode != 0:
        out.bump_problem = f"`uv lock` could not resolve {summary}: " + _uv_cause(
            res.stderr
        )
        effects.start_branch(branch, base, deps.runner)
        return
    new_text = lock_path.read_text()
    errors, fresh = bump.verify(
        old_lock=old_lock,
        new_lock=scan.load_lock(lock_path),
        old_text=old_text,
        new_text=new_text,
        targets=targets,
        now=ctx.now,
        cooldown_days=cfg.cooldown_days,
    )
    if errors:
        out.bump_problem = "; ".join(errors)
        effects.start_branch(branch, base, deps.runner)
        return
    # Titled chore(deps): the PR-title check requires chore/ci for a PR that touches
    # only dependency manifests (pr_title_convention.py, "deps").
    bumped = _listing([f"{p} to {targets[p]}" for p in pkgs], 2)
    title = f"chore(deps): bump {bumped} ({_listing(all_cves)})"
    effects.commit_and_push(branch, ["pyproject.toml", "uv.lock"], title, deps.runner)
    body = [
        f"Resolves {', '.join(all_cves)} from {ctx.ticket}.",
        "",
        "Adds a `constraint-dependencies` floor per package and upgrades only these "
        "packages (`uv lock --upgrade-package`), so no unrelated release moves.",
        "",
    ] + [f"- `{p}` → `{targets[p]}` ({', '.join(sorted(cves[p]))})" for p in pkgs]
    labels = [cfg.label]
    if fresh:
        labels = []
        body += [
            "",
            f"**Not auto-merged:** released inside the {cfg.cooldown_days}-day cooldown: "
            + ", ".join(fresh)
            + ". It is allowed because it fixes the CVE, but it needs a human review.",
        ]
    body += ["", f"Opened by the deterministic vuln triage: {ctx.run_url}"]
    out.bump_pr = effects.create_pr(
        ctx.repo, branch, title, "\n".join(body), labels, deps.runner
    )
    out.bump_labelled = bool(labels)


def _rooted(deps: Deps, root: Path) -> Deps:
    """Every command runs in the checkout, wherever the caller's cwd is."""
    inner = deps.runner

    def runner(cmd, **kw):
        kw.setdefault("cwd", root)
        return inner(cmd, **kw)

    return Deps(
        runner=runner,
        fetch_issue=deps.fetch_issue,
        comment=deps.comment,
        upstream=deps.upstream,
    )


def run(ctx: Context, deps: Deps) -> report.Outcome:
    deps = _rooted(deps, ctx.root)
    cfg = config.load(ctx.root)
    issue = deps.fetch_issue(ctx.ticket)
    cves = effects.ticket_cves(issue.get("description"))
    out = report.Outcome(dry_run=ctx.dry_run)
    if not cves:
        print(f"{ctx.ticket} has no vuln-ids marker; nothing to triage.")
        return out

    scan_dir = ctx.scan_dir or ctx.root / ".vuln-triage-scan"
    if ctx.scan_dir is None:
        rid = effects.download_scan(ctx.repo, ctx.scan_run_id, scan_dir, deps.runner)
        print(f"Read scan run {rid}.")
    try:
        findings = scan.load_findings(scan_dir)
    except scan.ScanIncomplete as e:
        raise SystemExit(f"::error::{e}; not triaging on a partial scan") from e
    lock = scan.load_lock(ctx.root / "uv.lock")
    upstream = deps.upstream or effects.pypi_upstream_check(
        cfg.stale_after_days, ctx.now
    )
    triages = triage_ticket(cves, findings, lock, upstream, ctx.severity)
    for t in triages:
        print(f"  {t.cve} {t.severity} case={t.case} {t.package} ({t.source})")

    detected = datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00")).date()
    base = ""
    if not ctx.dry_run:
        # Each PR starts from `git checkout --force` of the base commit, which discards
        # uncommitted edits to tracked files. CI checks out clean; a hand run may not.
        dirty = deps.runner(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if dirty:
            raise SystemExit(
                "::error::the checkout has uncommitted changes; commit them or use --dry-run"
            )
        base = deps.runner(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    try:
        _allowlist_pr(ctx, cfg, triages, detected, base, deps, out)
        _bump_pr(ctx, cfg, triages, base, deps, out)
    finally:
        if not ctx.dry_run:
            deps.runner(["git", "checkout", "--force", base], check=False)

    body = report.render(ctx.ticket, triages, out, ctx.run_url)
    if ctx.dry_run:
        print(body)
    else:
        deps.comment(issue["id"], body)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(body + "\n")
    return out
