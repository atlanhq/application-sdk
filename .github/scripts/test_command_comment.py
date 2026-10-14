#!/usr/bin/env python3
"""The sticky PR comment behind `/e2e` and `/gate-check` (FND-3839).

One comment per PR per command, found by a hidden marker and edited in place
through the request's life — never a new comment per request or per stage:

    requested → running → passed | failed | no verdict
                        ↘ not started (refused, with the reason)

It always describes the latest request only, on the commit that request was
made on; an earlier commit's verdict is replaced, not kept (its record is the
commit status and the run). Reactions on the triggering comment answer
instantly; this comment is the durable state.

Three writers share this module so the format cannot drift between them:
pr_test_command.py (requested / not started), tests-reusable.yaml's e2e path
(running / verdict, via the CLI below), and gate_check_verdict.py (the
`/gate-check` verdict). Rendering is pure and tested; I/O goes through
pr_test_command.GitHub.

Tested in tests/test_test_command_comment.py.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr_test_command import GitHub, GitHubError, _notice  # noqa: E402

E2E = "e2e"
GATE_CHECK = "gate-check"
COMMANDS = (E2E, GATE_CHECK)

REQUESTED = "requested"
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
NO_VERDICT = "no-verdict"
NOT_STARTED = "not-started"

_HEADLINE = {
    REQUESTED: ("⏳", "requested"),
    RUNNING: ("🏃", "running"),
    PASSED: ("✅", "passed"),
    FAILED: ("❌", "failed"),
    NO_VERDICT: ("⚠️", "no verdict"),
    NOT_STARTED: ("🚫", "not started"),
}

_RESULT_GLYPH = {
    "success": "✅",
    "failure": "❌",
    "cancelled": "🚫",
    "skipped": "⊘",
    "timed_out": "⏱️",
    "in_progress": "🏃",
    "queued": "⏳",
    "": "⏳",
}

_RERUN_HINT = {
    E2E: "Re-run: comment `/e2e` again after a push, or `/e2e force` on this commit.",
    GATE_CHECK: (
        "Re-run: comment `/gate-check` again after a push, or `/gate-check force` "
        "on this commit."
    ),
}


def marker(command: str) -> str:
    return f"<!-- test-commands:{command} -->"


@dataclass(frozen=True)
class Link:
    label: str
    url: str


@dataclass(frozen=True)
class CommentState:
    command: str
    stage: str
    sha: str
    actor: str = ""
    summary: str = ""
    links: tuple = ()
    #: (title, rows) — a table shown inside an expandable section.
    details_title: str = ""
    details: str = ""
    note: str = ""
    #: Markdown shown in full after the details, e.g. the e2e preview links.
    extra: str = ""


def glyph(result: str | None) -> str:
    return _RESULT_GLYPH.get(result or "", "⚠️")


def render(state: CommentState) -> str:
    """The whole comment body. Pure."""
    icon, word = _HEADLINE[state.stage]
    lines = [
        marker(state.command),
        f"### `/{state.command}` · {icon} {word} on `{state.sha[:7]}`",
    ]
    meta = [state.summary] if state.summary else []
    if state.actor:
        meta.append(f"requested by @{state.actor}")
    meta += [f"[{link.label}]({link.url})" for link in state.links]
    if meta:
        lines.append(" · ".join(meta))
    if state.note:
        lines += ["", state.note]
    if state.details:
        lines += [
            "",
            f"<details><summary>{state.details_title or 'Details'}</summary>",
            "",
            state.details,
            "",
            "</details>",
        ]
    if state.extra.strip():
        lines += ["", state.extra.strip()]
    if state.stage in (PASSED, FAILED, NO_VERDICT, NOT_STARTED):
        lines += ["", _RERUN_HINT[state.command]]
    return "\n".join(lines) + "\n"


# ── e2e: suite × cloud from the run's own jobs ────────────────────────────

_LEG = re.compile(r"End-to-end \((?P<leg>.+)\)$")


def legs_from_jobs(jobs: list, clouds: list) -> list:
    """(suite, cloud, conclusion) per e2e leg job. ``cloud`` is "" when the
    run had no cloud dimension; a suite name may itself contain hyphens, so
    the cloud is matched as a known suffix rather than split off blindly."""
    legs = []
    for job in jobs:
        match = _LEG.search(str(job.get("name") or ""))
        if not match:
            continue
        leg = match.group("leg")
        suite, cloud = leg, ""
        for candidate in clouds:
            if leg.endswith(f"-{candidate}"):
                suite, cloud = leg[: -len(candidate) - 1], candidate
                break
        legs.append((suite, cloud, job.get("conclusion") or job.get("status") or ""))
    return legs


def e2e_table(legs: list, clouds: list) -> str:
    """Markdown grid: one row per suite, one column per cloud."""
    if not legs:
        return ""
    if not clouds or all(cloud == "" for _, cloud, _ in legs):
        rows = ["| suite | result |", "|---|---|"]
        rows += [f"| {suite} | {glyph(result)} |" for suite, _, result in legs]
        return "\n".join(rows)
    suites: dict = {}
    for suite, cloud, result in legs:
        suites.setdefault(suite, {})[cloud] = result
    rows = [
        "| suite | " + " | ".join(clouds) + " |",
        "|---|" + "---|" * len(clouds),
    ]
    for suite, by_cloud in suites.items():
        cells = [glyph(by_cloud[c]) if c in by_cloud else "–" for c in clouds]
        rows.append(f"| {suite} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def e2e_verdict_state(
    *, sha: str, verdict: str, legs: list, clouds: list, run_url: str, extra: str = ""
) -> CommentState:
    failed = sum(1 for _, _, r in legs if r not in ("success", "skipped"))
    stage = {"success": PASSED, "failure": FAILED}.get(verdict, NO_VERDICT)
    if stage == PASSED:
        summary = f"{len(legs)} leg(s) passed"
    elif stage == FAILED:
        summary = f"{failed} of {len(legs)} leg(s) failed"
    else:
        summary = (
            "the run produced no verdict (cancelled, tenant busy, or setup failed)"
        )
    return CommentState(
        command=E2E,
        stage=stage,
        sha=sha,
        summary=summary,
        links=(Link("run", run_url),),
        details_title="Results (suite × cloud)" if clouds else "Results",
        details=e2e_table(legs, clouds),
        extra=extra,
    )


#: The check runs the SDK's connector e2e fan-out completes on its PR head.
SDK_E2E_CHECK_PREFIX = "Connector E2E run / "


def sdk_e2e_state(*, sha: str, checks: list, run_url: str) -> CommentState:
    """application-sdk's `/e2e` verdict: one row per connector e2e check."""
    rows = sorted(
        (
            str(c.get("name"))[len(SDK_E2E_CHECK_PREFIX) :],
            c.get("conclusion") or c.get("status") or "",
            c.get("html_url") or "",
        )
        for c in checks
        if str(c.get("name") or "").startswith(SDK_E2E_CHECK_PREFIX)
    )
    failed = sum(1 for _, result, _ in rows if result != "success")
    if not rows:
        stage, summary = NO_VERDICT, "no connector e2e check reported"
    elif failed:
        stage, summary = FAILED, f"{failed} of {len(rows)} connector(s) failed"
    else:
        stage, summary = PASSED, f"{len(rows)} connector(s) passed"
    table = ["| connector | result |", "|---|---|"]
    table += [
        f"| {name} | [{glyph(result)}]({url}) |"
        if url
        else f"| {name} | {glyph(result)} |"
        for name, result, url in rows
    ]
    return CommentState(
        command=E2E,
        stage=stage,
        sha=sha,
        summary=summary,
        links=(Link("run", run_url),),
        details_title="Results",
        details="\n".join(table) if rows else "",
    )


def gate_check_table(rows: list) -> str:
    """rows: (workflow, conclusion, url)."""
    lines = ["| workflow | result |", "|---|---|"]
    for workflow, conclusion, url in rows:
        cell = f"[{glyph(conclusion)}]({url})" if url else glyph(conclusion)
        lines.append(f"| `{workflow}` | {cell} |")
    return "\n".join(lines)


# ── I/O ────────────────────────────────────────────────────────────────────

#: A comment body is capped at 65536 characters; the verdict itself must fit.
MAX_EXTRA_CHARS = 20000


def _read_extra(path: str) -> str:
    """The extra markdown, or "" when there is none. Its writer
    (render_preview_links.py) already validated every link and name in it."""
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return text if len(text) <= MAX_EXTRA_CHARS else ""


def upsert(github: GitHub, pr: int, state: CommentState) -> None:
    """Edit this command's comment on ``pr`` in place, or create it."""
    body = render(state)
    existing = github.find_comment(pr, marker(state.command))
    if existing:
        github.update_comment(existing, body)
    else:
        github.comment(pr, body)


def try_upsert(github: GitHub, pr: int, state: CommentState) -> None:
    """Best effort: the comment is a view of the verdict, not the verdict.
    A failed write must never fail the job that owns the commit status."""
    try:
        upsert(github, pr, state)
    except GitHubError as error:
        print(
            f"::warning::could not update the /{state.command} comment: {error}",
            file=sys.stderr,
        )


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, help="PR number; empty is a no-op")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-url", required=True)
    sub = parser.add_subparsers(dest="stage", required=True)
    running = sub.add_parser("e2e-running")
    running.add_argument("--legs", default="")
    verdict = sub.add_parser("e2e-verdict")
    verdict.add_argument("--run-id", required=True, type=int)
    verdict.add_argument("--verdict", required=True)
    verdict.add_argument("--clouds", default="")
    verdict.add_argument(
        "--extra-file",
        default="",
        help="markdown appended to the verdict (render_preview_links.py's "
        "output); a missing file is no extra",
    )
    sub.add_parser("sdk-e2e-verdict")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.pr.strip().isdigit():
        _notice("no PR number; the /e2e comment is not updated")
        return 0
    github = GitHub(args.repo)
    pr = int(args.pr)
    if args.stage == "e2e-running":
        legs = f"{args.legs} leg(s)" if args.legs.strip().isdigit() else ""
        state = CommentState(
            command=E2E,
            stage=RUNNING,
            sha=args.sha,
            summary=legs,
            links=(Link("run", args.run_url),),
        )
    elif args.stage == "sdk-e2e-verdict":
        try:
            checks = github.check_runs(args.sha)
        except GitHubError as error:
            print(f"::warning::could not read the check runs: {error}", file=sys.stderr)
            return 0
        state = sdk_e2e_state(sha=args.sha, checks=checks, run_url=args.run_url)
    else:
        clouds = [c.strip() for c in args.clouds.split(",") if c.strip()]
        try:
            jobs = github.run_jobs(args.run_id)
        except GitHubError as error:
            print(f"::warning::could not read the run's jobs: {error}", file=sys.stderr)
            jobs = []
        state = e2e_verdict_state(
            sha=args.sha,
            verdict=args.verdict,
            legs=legs_from_jobs(jobs, clouds),
            clouds=clouds,
            run_url=args.run_url,
            extra=_read_extra(args.extra_file),
        )
    try_upsert(github, pr, state)
    # Always 0: this step runs inside the required gate job.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
