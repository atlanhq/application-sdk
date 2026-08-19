#!/usr/bin/env python3
"""Refuse an SDK-resolve push while an `@sdk-review` review is still in flight.

The problem
-----------
`@sdk-review` (read-only) and `@sdk-resolve` (the only writer) run in separate
mothership sandboxes, and the resolver *triggers* the reviewer then blocks for
its reply (`.mothership/pr-resolve/ORCHESTRATION.md` Phase 3a -> 3b). The two
lanes are therefore concurrent by design, and neither has a notion of a shared
round. When the resolver pushes inside the trigger -> verdict window, the review
in flight is reviewing a sha that no longer exists as HEAD:

* the verdict lands stamped `REVIEWED_HEAD: <old sha>`;
* `sdk_review_approve.py` compares that stamp against the live head, sees the
  mismatch, and declines every stamp ("head moved past the verdict");
* the round produced nothing, and the resolver spends another of its
  `MAX_ROUNDS` re-asking for the same review.

A shared GitHub Actions concurrency group across `sdk-review.yml` and
`sdk-resolve.yml` cannot fix this: the resolve job holds the group for the whole
loop while blocking on a review that would queue behind it, so every run would
end at `stopped_reason: review-timeout`. The invariant has to be enforced at the
push, which is what this script does.

What counts as "in flight"
--------------------------
Comment state on the PR, read fresh from the API. A review is in flight when the
newest `@sdk-review` **round** is unanswered and still plausibly alive:

* the round carries no `confused` reaction — `sdk-review.yml`'s gate job reacts
  😕 to a trigger it consciously declines (an unchanged-HEAD bot re-trigger), and
  deliberately posts no comment. Without reading that reaction a declined
  trigger would look unanswered forever, and
* no verdict comment *answers* it (below), and
* it is younger than `--max-inflight-seconds`. A reviewer sandbox that died
  mid-run must not wedge the resolver; the default matches the resolver's own
  Phase 3b wait, so this guard can never block longer than the wait the resolver
  already tolerates.

A **round** is one or more trigger comments posted within
`--burst-window-seconds` of each other, collapsed. Duplicate `@sdk-review` bursts
seconds apart are one logical request, and the reviewer's own dedupe guard
answers such a burst with a single verdict — counting them as two rounds would
leave one permanently unanswered and hold every later push for the full stale
window.

Answered, and why a timestamp alone cannot decide it
----------------------------------------------------
"A verdict landed after the trigger" is not sufficient. A reviewer sandbox runs
for up to 2h while the resolver's per-round wait is 40 min, and a human can
re-tag mid-review — so two reviews can be outstanding at once. The older one's
verdict then lands *after* the newer trigger, and a timestamp rule reads it as
the newer round's answer: the guard clears, the push lands, and the review that
is still running has its verdict stranded as stale. That is the same bug this
script exists to prevent, one round further along.

So the reviewer echoes the trigger it is answering as
`<!-- ANSWERS_TRIGGER: <comment id> -->` (`.mothership/pr-review/ORCHESTRATION.md`
§3e), and a round counts as answered when a verdict either:

* echoes one of that round's trigger comment ids — precise, or
* carries no `ANSWERS_TRIGGER` stamp at all and is newer than the round —
  the timestamp fallback, for verdicts predating the stamp, `workflow_dispatch`
  reviews that have no trigger comment, and the case where the reviewer simply
  omits the marker. Degrading to the old rule exactly where correlation is
  missing keeps a missing stamp from turning into a 40-minute false hold.

Fail-open, deliberately
-----------------------
Any failure to read comment state — `gh` non-zero, unparseable JSON, an
unparseable timestamp — reports "clear to push" with a `::warning::`. A guard
that cannot see the state must not be the thing that stops the resolver from
making progress; the consequence of a missed hold is one wasted round, while the
consequence of a false hold is a resolver that can never push at all. This
matches `sdk_review_gate.py`'s stance for the same reason.

Usage (from the resolver sandbox, cwd = the cloned repo)::

    # block until the in-flight review answers, then push
    python3 .github/scripts/sdk_resolve_push_guard.py --pr "$PR_NUMBER" --wait
    git push

    # or ask once and decide for yourself
    python3 .github/scripts/sdk_resolve_push_guard.py --pr "$PR_NUMBER"

Environment:
    REPO        owner/repo; defaults to atlanhq/application-sdk
    GH_TOKEN    consumed by `gh` for auth (not read here directly)

Exit status:
    0   clear to push (no review in flight, or the wait cleared it)
    10  a review is in flight (`--wait`: still in flight at the deadline)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_REPO = "atlanhq/application-sdk"

SUMMARY_MARKER = "<!-- SDK_REVIEW -->"
# Accepted alongside the current marker for PRs that predate the promotion out
# of the `test-sdk-review-*` prefix; kept in sync with sdk_review_approve.py.
LEGACY_SUMMARY_MARKER = "<!-- TEST_SDK_REVIEW -->"
SUMMARY_MARKERS = (SUMMARY_MARKER, LEGACY_SUMMARY_MARKER)

VERDICT_AUTHOR = "mothership-ai[bot]"

# A trigger comment *starts with* the mention — the same test `sdk-review.yml`'s
# job-level `if:` applies (`startsWith(github.event.comment.body, '@sdk-review')`).
# Anchoring here rather than searching anywhere in the body keeps prose that
# merely names the reviewer (a resolve status comment, a human explaining the
# loop) from being mistaken for a trigger.
TRIGGER_RE = re.compile(r"^\s*@sdk-review\b", re.IGNORECASE)

# The reaction `sdk-review.yml`'s gate leaves on a trigger it declined.
DECLINED_REACTION = "confused"

# The reviewer's echo of the trigger comment id its verdict answers. Written by
# .mothership/pr-review/ORCHESTRATION.md §3e; absent on verdicts that predate it
# and on workflow_dispatch reviews, which have no trigger comment.
ANSWERS_TRIGGER_RE = re.compile(r"<!--\s*ANSWERS_TRIGGER:\s*(\d+)\s*-->")

# Triggers this close together are one logical request, not two rounds. The
# duplicate bursts seen in practice land ~11s apart; the reviewer answers such a
# burst once, so treating them as two rounds would leave one unanswerable.
DEFAULT_BURST_WINDOW_SECONDS = 120

# Slightly past the reviewer's typical 5–15 min, and equal to the resolver's own
# Phase 3b per-round wait: a review that has not answered in this long is not
# coming back, and holding the push any longer only burns the resolver's budget.
DEFAULT_MAX_INFLIGHT_SECONDS = 2400
DEFAULT_DEADLINE_SECONDS = 1800
DEFAULT_INTERVAL_SECONDS = 30

EXIT_CLEAR = 0
EXIT_IN_FLIGHT = 10

Runner = Callable[..., subprocess.CompletedProcess]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fetch_comments(
    repo: str, pr_number: str, runner: Runner = subprocess.run
) -> list[dict] | None:
    """Every comment on the PR, or None when the state could not be read.

    None and [] are different answers and the caller treats them differently: []
    is "read it, there are no comments" (clear to push), None is "could not read
    it" (fail open, but say so). Collapsing them would make an API outage
    indistinguishable from a quiet PR.

    `--slurp` returns one array per page rather than a flat stream, and is used
    instead of `--jq` deliberately: the naive `--jq '.[] | .body'` form collapses
    a multiline body to its first line, which for a verdict comment is the HTML
    marker alone.
    """
    result = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/{pr_number}/comments",
            "--paginate",
            "--slurp",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"::warning::sdk-resolve push guard: could not list PR comments "
            f"(exit {result.returncode}) — not holding the push."
        )
        return None
    try:
        pages = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        print(
            f"::warning::sdk-resolve push guard: could not parse PR comments "
            f"({e}) — not holding the push."
        )
        return None
    comments: list[dict] = []
    for page in pages:
        # A single un-paginated response is a bare array; --slurp wraps it in one
        # more level. Tolerate both shapes.
        if isinstance(page, list):
            comments.extend(c for c in page if isinstance(c, dict))
        elif isinstance(page, dict):
            comments.append(page)
    return comments


def parse_timestamp(raw: str | None) -> datetime | None:
    """An ISO-8601 GitHub timestamp as an aware datetime, or None."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive value would raise on every later comparison; treat it as UTC,
    # which is what the API documents it to be.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_trigger(comment: dict) -> bool:
    """A comment that would fire `sdk-review.yml`."""
    return bool(TRIGGER_RE.match(comment.get("body") or ""))


def is_verdict(comment: dict) -> bool:
    """A review summary posted by the reviewer bot."""
    if (comment.get("user") or {}).get("login") != VERDICT_AUTHOR:
        return False
    body = comment.get("body") or ""
    return any(marker in body for marker in SUMMARY_MARKERS)


def was_declined(comment: dict) -> bool:
    """True when `sdk-review.yml`'s gate reacted 😕 — seen and declined."""
    reactions = comment.get("reactions")
    if not isinstance(reactions, dict):
        return False
    try:
        return int(reactions.get(DECLINED_REACTION) or 0) > 0
    except (TypeError, ValueError):
        return False


def extract_answers_trigger(body: str) -> str | None:
    """The trigger comment id a verdict says it answers, if it stamps one."""
    match = ANSWERS_TRIGGER_RE.search(body)
    return match.group(1) if match else None


def _dated(
    comments: list[dict], predicate: Callable[[dict], bool]
) -> list[tuple[datetime, dict]]:
    """Matching comments as (timestamp, comment), oldest first.

    The API returns comments oldest-first, but sorting on the timestamp rather
    than trusting the order keeps this correct if that ever changes — and a
    comment whose timestamp will not parse is dropped rather than silently
    sorting to the front.
    """
    dated: list[tuple[datetime, dict]] = []
    for comment in comments:
        if not predicate(comment):
            continue
        stamp = parse_timestamp(comment.get("created_at"))
        if stamp is not None:
            dated.append((stamp, comment))
    dated.sort(key=lambda pair: pair[0])
    return dated


@dataclass(frozen=True)
class Round:
    """One logical `@sdk-review` request: a burst of triggers, collapsed."""

    ids: frozenset[str]
    first_at: datetime
    last_at: datetime
    declined: bool


def logical_rounds(
    comments: list[dict],
    burst_window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS,
) -> list[Round]:
    """Trigger comments grouped into rounds, oldest round first.

    A round is declined only when *every* trigger in it was declined: if the gate
    let any of them through, a review ran.
    """
    rounds: list[Round] = []
    group: list[tuple[datetime, dict]] = []

    def flush() -> None:
        if not group:
            return
        rounds.append(
            Round(
                ids=frozenset(
                    str(comment["id"]) for _, comment in group if comment.get("id")
                ),
                first_at=group[0][0],
                last_at=group[-1][0],
                declined=all(was_declined(comment) for _, comment in group),
            )
        )
        group.clear()

    for stamp, comment in _dated(comments, is_trigger):
        if group and (stamp - group[-1][0]).total_seconds() > burst_window_seconds:
            flush()
        group.append((stamp, comment))
    flush()
    return rounds


def answering_verdict(
    round_: Round, verdicts: list[tuple[datetime, dict]]
) -> tuple[datetime, dict] | None:
    """The verdict that answers `round_`, or None.

    Newest first, so a correlated answer wins over an older fallback match.
    """
    for stamp, comment in reversed(verdicts):
        echoed = extract_answers_trigger(comment.get("body") or "")
        if echoed is not None:
            # Stamped verdicts are self-describing: one that names a different
            # round is NOT this round's answer, however recent it is. That is the
            # whole point — an earlier round's late verdict must not clear a
            # review that is still running.
            if echoed in round_.ids:
                return stamp, comment
            continue
        if stamp > round_.last_at:
            return stamp, comment
    return None


def assess(
    comments: list[dict],
    now: datetime,
    max_inflight_seconds: float = DEFAULT_MAX_INFLIGHT_SECONDS,
    burst_window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS,
) -> tuple[bool, str, str]:
    """Return (in_flight, reason, message) for the PR's comment state."""
    rounds = logical_rounds(comments, burst_window_seconds)
    if not rounds:
        return False, "no-trigger", "No `@sdk-review` trigger on this PR."

    current = rounds[-1]
    if current.declined:
        return (
            False,
            "trigger-declined",
            "The newest `@sdk-review` trigger was declined by the review gate "
            "(😕) — no review is running for it.",
        )

    answer = answering_verdict(current, _dated(comments, is_verdict))
    if answer is not None:
        stamp, comment = answer
        correlated = extract_answers_trigger(comment.get("body") or "") is not None
        return (
            False,
            "verdict-answered",
            f"The newest `@sdk-review` round was answered at {stamp.isoformat()} "
            f"({'correlated' if correlated else 'by timestamp'}) — clear to push.",
        )

    waited = (now - current.last_at).total_seconds()
    if waited >= max_inflight_seconds:
        return (
            False,
            "trigger-stale",
            f"The newest `@sdk-review` round is {int(waited)}s old with no "
            f"verdict of its own (cap {int(max_inflight_seconds)}s) — treating "
            f"the review as dead rather than holding the push further.",
        )

    return (
        True,
        "review-in-flight",
        f"An `@sdk-review` review has been in flight for {int(waited)}s with no "
        f"verdict of its own yet. Pushing now would move HEAD out from under it "
        f"and the verdict would be discarded as stale.",
    )


def check(
    repo: str,
    pr_number: str,
    runner: Runner = subprocess.run,
    now: Clock = _utcnow,
    max_inflight_seconds: float = DEFAULT_MAX_INFLIGHT_SECONDS,
    burst_window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS,
) -> tuple[bool, str, str]:
    """One assessment against live comment state. Fails open."""
    comments = fetch_comments(repo, pr_number, runner)
    if comments is None:
        return False, "state-unreadable", "Could not read PR comments — failing open."
    return assess(comments, now(), max_inflight_seconds, burst_window_seconds)


def wait_until_clear(
    repo: str,
    pr_number: str,
    runner: Runner = subprocess.run,
    now: Clock = _utcnow,
    sleeper: Sleeper = time.sleep,
    max_inflight_seconds: float = DEFAULT_MAX_INFLIGHT_SECONDS,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    burst_window_seconds: float = DEFAULT_BURST_WINDOW_SECONDS,
) -> tuple[bool, str, str]:
    """Poll until no review is in flight, or the deadline elapses.

    A heartbeat line per iteration keeps bytes flowing on the sandbox's stream so
    neither mothership's idle timeout nor the dispatch read watchdog fires while
    this blocks — the same reason ORCHESTRATION Phase 3b prints one.

    Bounded two ways on purpose. The wall clock is the real budget, but a clock
    that does not advance — a frozen fake in a test, a sandbox whose clock is
    stepped by its host — would make the elapsed check unfalsifiable and spin
    this loop forever, which for a guard whose whole job is "let the resolver
    push eventually" is the worst available failure. The poll count is the
    backstop that cannot stall.
    """
    started = now()
    polls = 0
    max_polls = (
        max(1, int(deadline_seconds // interval_seconds)) if interval_seconds > 0 else 1
    )
    while True:
        in_flight, reason, message = check(
            repo, pr_number, runner, now, max_inflight_seconds, burst_window_seconds
        )
        if not in_flight:
            return in_flight, reason, message

        elapsed = (now() - started).total_seconds()
        polls += 1
        if elapsed + interval_seconds > deadline_seconds or polls > max_polls:
            return (
                True,
                "wait-deadline",
                f"Still in flight after {int(elapsed)}s / {polls} polls "
                f"(deadline {int(deadline_seconds)}s): {message}",
            )
        print(
            f"[push-guard] review in flight, waiting … "
            f"{int(elapsed)}s/{int(deadline_seconds)}s elapsed"
        )
        sleeper(interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Refuse an SDK-resolve push while an @sdk-review review is in flight."
        )
    )
    parser.add_argument("--pr", required=True, help="pull request number")
    parser.add_argument(
        "--wait",
        action="store_true",
        help="block until the in-flight review answers instead of reporting once",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_DEADLINE_SECONDS,
        help=f"--wait budget (default {DEFAULT_DEADLINE_SECONDS})",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"--wait poll interval (default {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--max-inflight-seconds",
        type=float,
        default=DEFAULT_MAX_INFLIGHT_SECONDS,
        help=(
            "age at which an unanswered trigger is treated as a dead review "
            f"(default {DEFAULT_MAX_INFLIGHT_SECONDS})"
        ),
    )
    parser.add_argument(
        "--burst-window-seconds",
        type=float,
        default=DEFAULT_BURST_WINDOW_SECONDS,
        help=(
            "triggers this close together count as one round "
            f"(default {DEFAULT_BURST_WINDOW_SECONDS})"
        ),
    )
    return parser


def main(
    argv: list[str] | None = None,
    runner: Runner = subprocess.run,
    now: Clock = _utcnow,
    sleeper: Sleeper = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    repo = os.environ.get("REPO") or DEFAULT_REPO

    if args.wait:
        in_flight, reason, message = wait_until_clear(
            repo,
            args.pr,
            runner,
            now,
            sleeper,
            args.max_inflight_seconds,
            args.deadline_seconds,
            args.interval_seconds,
            args.burst_window_seconds,
        )
    else:
        in_flight, reason, message = check(
            repo,
            args.pr,
            runner,
            now,
            args.max_inflight_seconds,
            args.burst_window_seconds,
        )

    if in_flight:
        print(f"::warning::sdk-resolve push guard: HOLD ({reason}) — {message}")
        return EXIT_IN_FLIGHT
    print(f"::notice::sdk-resolve push guard: clear ({reason}) — {message}")
    return EXIT_CLEAR


if __name__ == "__main__":
    raise SystemExit(main())
