"""Release Gate: decide whether a connector app pull request may merge.

Run by the bootstrap-managed ``release-gate.yaml`` in every connector repo,
from a checkout of THIS repository at ``main`` (FND-5321). Bootstrap used to
vendor this file into each app, so the pull request's own copy ran and a pull
request could edit the gate it was judged by. Now the app keeps only the shim,
and the shim runs on ``pull_request_target``, so GitHub always takes it from
the default branch: a pull request can edit neither the script nor the shim
that judges it. Nothing of the pull request is checked out or run; the two
files the pin check needs are read at the head through the API, as data. The
shim passes everything it knows straight through (per docs/standards/ci.md:
no branching shell in ``run:``)::

    python release_gate.py gate --repo "$REPO" --pr "$PR" \
      --head-sha "$HEAD_SHA" --head-ref "$HEAD_REF" --labels "$LABELS" \
      --config-dir .github/connector-reviewer

and, on a ``status`` event for the reviewer's context::

    python release_gate.py rerun --repo "$REPO" --sha "$SHA" \
      --context "$CONTEXT" --creator "$CREATOR" --at "$UPDATED_AT"

The gate has two duties. A pull request must satisfy both.

**1. A release PR has passed its release gate** (FND-3839, unchanged). A
release PR is a ``bump-version*`` branch or carries the ``release`` label. It
passes only on a ``success`` ``gate-check`` status on its head that
``github-actions[bot]`` posted (FND-3947: anyone with write access can post
that context, and on a bump PR this gate is the only blocker). ``pending``
is still running, ``failure``/``error`` failed, none means comment
``/gate-check``. Before the status is read, a release PR pinned to an
unreleased Application SDK is refused (a git/path/URL source in ``uv.lock``, a
pre-release version, or a ``[tool.uv.sources]`` redirect): a release built on
it ships SDK code no SDK release contains.

**2. The connector reviewer's verdict is green on the head** (FND-5321). Every
pull request the reviewer would review needs the ``connector-reviewer`` commit
status on its head to be ``success``: a review with every finding fixed or
dismissed, or a ``/review waive`` for this head, or ``/review`` answering
``exempt``. Only a status the reviewer's App posted counts
(``connector_reviewer.github.bot_login``), for the same reason as above.
``pending`` is a review still running, ``error`` a review that failed or
could not read everything, ``failure`` open findings, and none means nobody
asked. A push moves the head to a commit with no status, so the gate blocks
again until the new head is reviewed or waived.

A pull request with no status passes when the reviewer would not review it:
the classifier is the reviewer's own (``connector_reviewer.review.exemption``,
with ``config.toml``'s ``[exempt]`` lists), applied only while the reviewer has
never reviewed the pull request, exactly as ``/review`` does. A version bump is
one of those classes, so a release PR needs its ``gate-check`` but no review.

On a failure the gate lists the reviewer's open findings and suggestions,
read from the state block in the reviewer's own summary comment (only the
App's comment is trusted), in the job summary and as an error annotation on the run. The gate posts no
PR comment: the reason is in the Release Gate check itself.

Anything that cannot be read fails the gate: a release that skips its gate
is worse than a re-run of a two-minute check.

``rerun`` is the other half of reading a verdict that lands after the gate ran:
the reviewer posts its status with the App token, which fires a ``status``
event, and the shim calls this to re-run the newest Release Gate run on that
commit (``gate_check_verdict.rerun_release_gate``, the same re-run a
``gate-check`` verdict triggers).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from connector_reviewer.exempt import Exemption
from connector_reviewer.findings import PRState

RELEASE_LABEL = "release"
BUMP_BRANCH_PREFIX = "bump-version"
VERDICT_CREATOR = "github-actions[bot]"
GATE_CHECK_CONTEXT = "gate-check"
REVIEW_CONTEXT = "connector-reviewer"

# Every distribution the SDK repo publishes starts with this.
SDK_PREFIX = "atlan-application-sdk"
# A final release, optionally a post-release: 3.57.0, 3.57.0.post1.
RELEASED_VERSION = re.compile(r"\d+(\.\d+)*(\.post\d+)?")
PIN_HINT = "Pin a released SDK version, re-lock, then /gate-check."
LOCK_UNREADABLE = "uv.lock could not be parsed, so the SDK pin is unknown."
PINS_UNREADABLE = "uv.lock or pyproject.toml could not be read at the head"

MISSING_HINT = "Release PR CI is opt-in. Comment /gate-check to run it."
PENDING_HINT = "The release gate is still running on this commit."
FAILED_HINT = "The release gate failed on this commit. Fix it, then /gate-check."
PASSED = "The release gate passed on this commit."

REVIEW_MISSING = "No connector review on this commit: comment `/review`."
REVIEW_PENDING = "The connector review is still running on this commit."
REVIEW_ERROR = "The connector review failed on this commit: re-run `/review`."
REVIEW_UNREADABLE = (
    "The connector-reviewer status could not be read, so the gate fails closed: "
    "re-run this check."
)
# How many open findings the one-line message names; the summary lists all.
NAMED_FINDINGS = 6
SUGGESTION_KEEP = 300


# ---- release PRs (unchanged from the vendored gate) ------------------------


def parse_labels(raw: str) -> list[str]:
    """Return label names from ``toJson(...labels.*.name)``, else ``[]``."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [label for label in payload if isinstance(label, str)]


def is_release_pr(head_ref: str, labels: list[str]) -> bool:
    """Return whether this is a release PR: a bump branch or ``release`` label.

    The branch is the reliable signal: the label is added after the PR is
    opened, so the ``opened`` event's payload can lack it.
    """
    return head_ref.startswith(BUMP_BRANCH_PREFIX) or RELEASE_LABEL in labels


def _table(value: object) -> dict[str, Any]:
    """``value`` when it is a TOML table or JSON object, else ``{}``."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _array(value: object) -> list[Any]:
    """``value`` when it is a TOML array or JSON list, else ``[]``."""
    return cast("list[Any]", value) if isinstance(value, list) else []


def _loads(text: str | None) -> dict[str, Any] | None:
    """Return parsed TOML, ``{}`` for an absent file (None), ``None`` if unparseable."""
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _read_file(path: str) -> str | None:
    """A local file's text, or None when there is no such file."""
    file = pathlib.Path(path)
    if not path or not file.is_file():
        return None
    try:
        return file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "\x00 unreadable"  # not TOML: reads as unparseable


def _is_sdk(name: object) -> bool:
    """Return whether ``name`` is a distribution the SDK repo publishes."""
    return isinstance(name, str) and name.lower().startswith(SDK_PREFIX)


def _locked_pins(lock: dict[str, Any]) -> list[str]:
    """Return one finding per SDK package ``uv.lock`` pins unreleased."""
    found: list[str] = []
    for entry in _array(lock.get("package")):
        package = _table(entry)
        name = package.get("name")
        if not _is_sdk(name):
            continue
        source = package.get("source")
        if not isinstance(source, dict) or "registry" not in source:
            kind = "/".join(sorted(_table(source))) if isinstance(source, dict) else "?"
            found.append(f"{name} is locked from a {kind} source")
            continue
        version = str(package.get("version", ""))
        if not RELEASED_VERSION.fullmatch(version):
            found.append(f"{name} is locked to pre-release {version}")
    return found


def _is_named_index(source: object) -> bool:
    """Return whether ``source`` only names an index, optionally per marker."""
    return isinstance(source, dict) and set(_table(source)) <= {"index", "marker"}


def _source_pins(pyproject: dict[str, Any]) -> list[str]:
    """Return one finding per SDK package ``[tool.uv.sources]`` redirects."""
    sources = _table(_table(_table(pyproject.get("tool")).get("uv")).get("sources"))
    found: list[str] = []
    for name, source in sources.items():
        if not _is_sdk(name):
            continue
        entries = _array(source) if isinstance(source, list) else [source]
        if entries and all(_is_named_index(entry) for entry in entries):
            continue
        found.append(f"{name} is redirected by [tool.uv.sources]")
    return found


def pins_from_text(lock_text: str | None, pyproject_text: str | None) -> list[str]:
    """Return why the head is pinned to an unreleased SDK, else ``[]``.

    ``None`` is a file the head does not have. The SDK repo itself (a project
    named ``atlan-application-sdk*``) locks its own packages from the
    workspace, so it is never a finding.
    """
    pyproject = _loads(pyproject_text) or {}
    if _is_sdk(_table(pyproject.get("project")).get("name")):
        return []
    lock = _loads(lock_text)
    if lock is None:
        return [LOCK_UNREADABLE]
    return _locked_pins(lock) + _source_pins(pyproject)


def unreleased_sdk_pins(lock_path: str, pyproject_path: str) -> list[str]:
    """``pins_from_text`` for two local files (absent reads as absent)."""
    return pins_from_text(_read_file(lock_path), _read_file(pyproject_path))


def head_pins(read: Callable[[str], str | None]) -> list[str]:
    """``pins_from_text`` for the head's ``uv.lock`` and ``pyproject.toml``.

    ``read(path)`` returns the file's text at the head, None when the head
    has no such file, and raises when it cannot be read: that fails the gate,
    never passes it.
    """
    try:
        lock, pyproject = read("uv.lock"), read("pyproject.toml")
    except Exception as error:  # noqa: BLE001 - any read failure fails closed
        return [f"{PINS_UNREADABLE} ({type(error).__name__}: {error})"[:300]]
    return pins_from_text(lock, pyproject)


def newest_status(
    rows: Sequence[dict[str, Any]], context: str, creator: str
) -> dict[str, Any] | None:
    """The newest status row for ``context`` that ``creator`` posted, or None.

    ``rows`` is the per-commit statuses list, newest first: only that list
    carries each status's ``creator``. A row from anyone else is skipped, so a
    forged newer status can neither pass nor mask the real one.
    """
    for row in rows:
        if row.get("context") != context:
            continue
        if (_table(row.get("creator")).get("login") or "") == creator:
            return row
    return None


def decide_release(
    head_ref: str, labels: list[str], state: str, pins: list[str]
) -> tuple[bool, str] | None:
    """(passes, message) for a release PR's gate-check, or None for any other PR."""
    if not is_release_pr(head_ref, labels):
        return None
    if pins:
        return False, f"Unreleased SDK pin: {'; '.join(pins)}. {PIN_HINT}"
    if state == "success":
        return True, PASSED
    if state == "pending":
        return False, PENDING_HINT
    if state in {"failure", "error"}:
        return False, FAILED_HINT
    return False, MISSING_HINT


# ---- the connector reviewer's verdict (FND-5321) ---------------------------


@dataclass(frozen=True)
class OpenFinding:
    """What the gate shows of one open finding from the reviewer's state."""

    id: str
    severity: str
    title: str
    principle: str = ""
    path: str = ""
    line: int = 0
    suggestion: str = ""


@dataclass(frozen=True)
class ReviewFacts:
    """What the gate read about the connector reviewer on this head.

    ``state``/``description``/``url`` are the reviewer App's newest
    ``connector-reviewer`` status on the head (``state`` is ``""`` when there
    is none). ``unreadable`` is set when the statuses could not be read.
    ``reviewed`` is whether the reviewer has ever reviewed this pull request
    (its state's round), which makes it never exempt again. ``exemption`` is
    ``(class, why)`` from the reviewer's classifier, or None.
    """

    state: str = ""
    description: str = ""
    url: str = ""
    unreadable: str = ""
    reviewed: bool = False
    exemption: tuple[str, str] | None = None
    findings: tuple[OpenFinding, ...] = ()
    advisories: tuple[str, ...] = ()
    waiver: dict[str, Any] = field(default_factory=dict)


def _named(findings: Sequence[OpenFinding]) -> str:
    ids = ", ".join(f.id for f in findings[:NAMED_FINDINGS])
    return ids + (", ..." if len(findings) > NAMED_FINDINGS else "")


def decide_review(facts: ReviewFacts, head_sha: str) -> tuple[bool, str]:
    """(passes, message) for the reviewer's verdict on ``head_sha``."""
    if facts.unreadable:
        return False, f"{REVIEW_UNREADABLE} ({facts.unreadable})"
    if facts.state == "success":
        waiver = facts.waiver if facts.waiver.get("head") == head_sha else {}
        if waiver:
            return True, (
                f"Waived by @{waiver.get('by', '?')} for this head: "
                f"{waiver.get('reason', '')}".rstrip(": ")
            )
        if facts.description.startswith("waived by"):
            return True, f"Connector review {facts.description} (this head)."
        if facts.description.startswith("exempt"):
            return True, f"Connector review: {facts.description}."
        return True, "The connector review is green on this commit."
    if facts.state == "pending":
        return False, REVIEW_PENDING
    if facts.state == "error":
        why = f" ({facts.description})" if facts.description else ""
        return False, f"{REVIEW_ERROR}{why}"
    if facts.state == "failure":
        if facts.findings:
            return False, (
                f"The connector review has {len(facts.findings)} open finding(s). "
                f"Fix or dismiss the open findings: {_named(facts.findings)}. "
                "Then comment `/review`."
            )
        why = f" ({facts.description})" if facts.description else ""
        return False, f"The connector review is red on this commit{why}."
    if facts.exemption is not None and not facts.reviewed:
        kind, why = facts.exemption
        return True, f"Exempt from connector review ({kind}): {why}."
    return False, REVIEW_MISSING


@dataclass(frozen=True)
class Outcome:
    """The verdict: one line per duty, and which of those lines failed."""

    passes: bool
    lines: tuple[str, ...]
    failed: tuple[str, ...] = ()


def decide(
    head_ref: str,
    labels: list[str],
    gate_check_state: str,
    pins: list[str],
    review: ReviewFacts,
    head_sha: str,
) -> Outcome:
    """Both duties: a release PR's gate-check, then the reviewer's verdict."""
    lines: list[str] = []
    failed: list[str] = []
    duties: list[tuple[str, tuple[bool, str]]] = []
    release = decide_release(head_ref, labels, gate_check_state, pins)
    if release is not None:
        duties.append(("Release gate", release))
    duties.append(("Connector review", decide_review(review, head_sha)))
    for name, (ok, message) in duties:
        line = f"{name}: {message}"
        lines.append(line)
        if not ok:
            failed.append(line)
    return Outcome(not failed, tuple(lines), tuple(failed))


def _cell(text: str, keep: int = 200) -> str:
    """One markdown table cell: one line, no pipes, capped."""
    flat = " ".join(str(text).split()).replace("|", "\\|")
    return flat if len(flat) <= keep else flat[: keep - 3] + "..."


def render_summary(outcome: Outcome, review: ReviewFacts) -> str:
    """The job summary: the verdict, then the reviewer's open findings."""
    head = "Release Gate passed" if outcome.passes else "Release Gate failed"
    out = [f"## {'✅' if outcome.passes else '❌'} {head}", ""]
    out += [
        f"- {'❌' if line in outcome.failed else '✅'} {line}" for line in outcome.lines
    ]
    if review.url:
        out += ["", f"Reviewer summary: {review.url}"]
    if review.findings and review.state != "success":
        out += [
            "",
            f"### Open findings ({len(review.findings)})",
            "",
            "| Id | Severity | Title | Principle | Where |",
            "|---|---|---|---|---|",
        ]
        for f in review.findings:
            where = f"{f.path}:{f.line}" if f.line else f.path
            out.append(
                f"| {f.id} | {f.severity} | {_cell(f.title)} | "
                f"{_cell(f.principle or '-')} | {_cell(where or '-')} |"
            )
        suggested = [f for f in review.findings if f.suggestion.strip()]
        if suggested:
            out += ["", "### Suggestions", ""]
            out += [
                f"- **{f.id}**: {_cell(f.suggestion, SUGGESTION_KEEP)}"
                for f in suggested
            ]
        out += [
            "",
            "Fix a finding and comment `/review`; `/review dismiss F-... <reason>` "
            "closes a medium or low one (a critical or high one needs someone "
            "other than the author); `/review waive <reason>` sets the verdict "
            "aside for this head.",
        ]
    if review.advisories and review.state != "success":
        out += ["", "### Reminders", ""]
        out += [f"- {_cell(a, SUGGESTION_KEEP)}" for a in review.advisories]
    return "\n".join(out) + "\n"


# ---- reading GitHub (the only I/O) -----------------------------------------


def review_facts(
    *,
    rows: Sequence[dict[str, Any]],
    reviewer_login: str,
    load_state: Callable[[], PRState | None],
    classify: Callable[[], Exemption | None],
) -> ReviewFacts:
    """Assemble ``ReviewFacts`` from the head's status rows, the reviewer's
    state (``load_state() -> PRState | None``) and its classifier
    (``classify() -> Exemption | None``). The classifier is only asked when
    there is no verdict: it costs a diff."""
    row = newest_status(rows, REVIEW_CONTEXT, reviewer_login)
    state = load_state()
    findings: tuple[OpenFinding, ...] = ()
    advisories: tuple[str, ...] = ()
    waiver: dict[str, Any] = {}
    reviewed = False
    if state is not None:
        reviewed = state.round > 0
        findings = tuple(
            OpenFinding(
                id=f.id,
                severity=f.severity,
                title=f.title,
                principle=f.rule_id,
                path=f.path,
                line=f.line or f.head_line,
                suggestion=f.suggestion,
            )
            for f in state.open_findings()
        )
        advisories = tuple(state.advisories)
        waiver = dict(state.waiver)
    exemption: tuple[str, str] | None = None
    if row is None and not reviewed:
        found = classify()
        if found is not None:
            exemption = (found.kind, found.why)
    return ReviewFacts(
        state=str(_table(row).get("state") or ""),
        description=str(_table(row).get("description") or ""),
        url=str(_table(row).get("target_url") or ""),
        reviewed=reviewed,
        exemption=exemption,
        findings=findings,
        advisories=advisories,
        waiver=waiver,
    )


def gather(
    repo: str,
    number: int,
    head_sha: str,
    config_dir: str,
) -> tuple[list[dict[str, Any]], ReviewFacts]:  # pragma: no cover - network glue
    """Read the head's statuses, the reviewer's state and its classifier."""
    from connector_reviewer import review as reviewer  # noqa: PLC0415
    from connector_reviewer.config import load_config  # noqa: PLC0415
    from connector_reviewer.github import GitHub as ReviewerGitHub  # noqa: PLC0415
    from connector_reviewer.github import bot_login  # noqa: PLC0415
    from pr_test_command import GitHub, GitHubError  # noqa: PLC0415

    # `status_rows` returns an untyped list (pr_test_command predates typing).
    client: Any = GitHub(repo)
    try:
        rows = cast("list[dict[str, Any]]", client.status_rows(head_sha))
    except GitHubError as error:
        return [], ReviewFacts(unreadable=str(error)[:200])
    gh = ReviewerGitHub(repo)

    def load_state() -> PRState | None:
        state, _ = reviewer.find_state(gh, number)
        return state

    def classify() -> Exemption | None:
        pr = gh.pr(number)
        if pr["head"]["sha"] != head_sha:
            # The pull request moved on; this run's head is not its head.
            return None
        cfg = load_config(pathlib.Path(config_dir))
        return reviewer.exemption(
            gh, number, pr, gh.diff(pr["base"]["sha"], head_sha), cfg
        )

    try:
        facts = review_facts(
            rows=rows,
            reviewer_login=bot_login(),
            load_state=load_state,
            classify=classify,
        )
    except Exception as error:  # noqa: BLE001 - any read failure fails closed
        return rows, ReviewFacts(unreadable=f"{type(error).__name__}: {error}"[:200])
    return rows, facts


def run_gate(args: argparse.Namespace) -> int:  # pragma: no cover - CLI glue
    from connector_reviewer.github import GitHub as ReviewerGitHub  # noqa: PLC0415

    labels = parse_labels(args.labels)
    gh = ReviewerGitHub(args.repo)
    pins = (
        head_pins(lambda path: read_at(gh, path, args.head_sha))
        if is_release_pr(args.head_ref, labels)
        else []
    )
    rows, review = gather(args.repo, args.pr, args.head_sha, args.config_dir)
    gate_row = newest_status(rows, GATE_CHECK_CONTEXT, VERDICT_CREATOR)
    outcome = decide(
        args.head_ref,
        labels,
        str(_table(gate_row).get("state") or ""),
        pins,
        review,
        args.head_sha,
    )
    return emit(outcome, review, args.summary_file)


def read_at(gh: Any, path: str, sha: str) -> str | None:  # pragma: no cover - glue
    """A file's text at ``sha`` through the contents API (raw, so a ``uv.lock``
    over 1 MB reads too); None when the head has no such file. Any other
    failure raises, so the pin check fails closed."""
    from connector_reviewer.github import GitHubError  # noqa: PLC0415

    try:
        text = gh._call(
            "GET",
            f"/repos/{gh.repo}/contents/{path}?ref={sha}",
            accept="application/vnd.github.raw",
        )
    except GitHubError as error:
        if "HTTP 404" in str(error):
            return None
        raise
    return text if isinstance(text, str) else None


def emit(outcome: Outcome, review: ReviewFacts, summary_file: str) -> int:
    """Print the verdict, write the job summary, and return the exit code."""
    # Only a failing duty is an error annotation: a release PR blocked on its
    # gate-check still shows its review duty as passed, not as a second error.
    for line in outcome.lines:
        print(f"::error title=Release Gate::{line}" if line in outcome.failed else line)
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as fh:
                fh.write(render_summary(outcome, review))
        except OSError:
            pass
    return 0 if outcome.passes else 1


def run_explain(args: argparse.Namespace) -> int:  # pragma: no cover - CLI glue
    """Read-only: what the gate decides for one real pull request, and the
    reviewer's exemption classifier's answer whatever the status says. Posts
    nothing, writes nothing; one JSON line on stdout."""
    from connector_reviewer import review as reviewer  # noqa: PLC0415
    from connector_reviewer.config import load_config  # noqa: PLC0415
    from connector_reviewer.github import GitHub as ReviewerGitHub  # noqa: PLC0415

    gh = ReviewerGitHub(args.repo)
    pr = gh.pr(args.pr)
    head, head_ref = pr["head"]["sha"], pr["head"]["ref"]
    labels = [str(_table(x).get("name")) for x in _array(pr.get("labels"))]
    from connector_reviewer.advisory import changed_paths  # noqa: PLC0415
    from connector_reviewer.diff import parse_unified_diff  # noqa: PLC0415

    diff = gh.diff(pr["base"]["sha"], head)
    found = reviewer.exemption(
        gh, args.pr, pr, diff, load_config(pathlib.Path(args.config_dir))
    )
    paths = changed_paths(parse_unified_diff(diff))
    pins = (
        head_pins(lambda path: read_at(gh, path, head))
        if is_release_pr(head_ref, labels)
        else []
    )
    rows, review = gather(args.repo, args.pr, head, args.config_dir)
    gate_row = newest_status(rows, GATE_CHECK_CONTEXT, VERDICT_CREATOR)
    outcome = decide(
        head_ref,
        labels,
        str(_table(gate_row).get("state") or ""),
        pins,
        review,
        head,
    )
    print(
        json.dumps(
            {
                "pr": f"{args.repo}#{args.pr}",
                "author": _table(pr.get("user")).get("login"),
                "branch": head_ref,
                "files": sorted(paths)[:8] + (["..."] if len(paths) > 8 else []),
                "classifier": found.kind if found else "reviewed",
                "why": found.why if found else "",
                "reviewer_status": review.state or "none",
                "reviewed_before": review.reviewed,
                "gate": "pass" if outcome.passes else "block",
                "gate_lines": list(outcome.lines),
            }
        )
    )
    return 0


def _ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def should_rerun(context: str, creator: str, reviewer_login: str) -> bool:
    """Only the reviewer App's own ``connector-reviewer`` status re-runs the gate."""
    return context == REVIEW_CONTEXT and creator == reviewer_login


def run_rerun(args: argparse.Namespace) -> int:  # pragma: no cover - CLI glue
    from connector_reviewer.github import bot_login  # noqa: PLC0415
    from gate_check_verdict import rerun_release_gate  # noqa: PLC0415
    from pr_test_command import GitHub, GitHubError  # noqa: PLC0415

    if not should_rerun(args.context, args.creator, bot_login()):
        print(f"{args.context} status from {args.creator}: nothing to re-run")
        return 0
    try:
        rerun_release_gate(GitHub(args.repo), args.sha, _ts(args.at))
    except GitHubError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("gate", help="decide the gate for one pull request")
    gate.add_argument("--repo", required=True)
    gate.add_argument("--pr", type=int, required=True)
    gate.add_argument("--head-sha", required=True)
    # Both decide whether the release duty applies, so neither has a default:
    # a call that leaves one out must fail, never read as "not a release PR".
    gate.add_argument("--head-ref", required=True, help="the PR's head branch")
    gate.add_argument(
        "--labels", required=True, help="the PR's label names as a JSON array"
    )
    gate.add_argument("--config-dir", default="", help="the reviewer's config dir")
    gate.add_argument(
        "--summary-file",
        default=os.environ.get("GITHUB_STEP_SUMMARY", ""),
        help="where the job summary goes",
    )
    gate.set_defaults(func=run_gate)
    explain = sub.add_parser(
        "explain", help="read-only: the gate's decision for one real pull request"
    )
    explain.add_argument("--repo", required=True)
    explain.add_argument("--pr", type=int, required=True)
    explain.add_argument(
        "--config-dir", required=True, help="the reviewer's config dir"
    )
    explain.set_defaults(func=run_explain)
    rerun = sub.add_parser("rerun", help="re-run Release Gate on a new verdict")
    rerun.add_argument("--repo", required=True)
    rerun.add_argument("--sha", required=True)
    rerun.add_argument("--context", default="")
    rerun.add_argument("--creator", default="")
    rerun.add_argument("--at", default="", help="the status's updated_at")
    rerun.set_defaults(func=run_rerun)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
