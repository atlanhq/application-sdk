#!/usr/bin/env python3
"""Post a best-effort emoji reaction on the comment that triggered a workflow.

Why this is a script and not four lines of inline `github-script`
----------------------------------------------------------------
Every bot entry point in this repo opens by reacting to the triggering
`@mention` comment — 👀 "seen and running", 😕 "seen and declined", 🚀
"seen and dispatching". The reaction is pure UX: it tells a human their
comment registered, seconds before any real output exists.

It used to be the *first* API call of the dispatch job, unguarded. On
2026-08-17 GitHub returned `HTTP 503` from
`POST /repos/{repo}/issues/comments/{id}/reactions` for a few seconds, and
because an unhandled rejection in `actions/github-script` fails the step,
the whole `sdk-review-dispatch` job died before it ever fetched the PR head
or dispatched the mothership session. Two `@sdk-review` requests were lost
outright — no review, no comment, nothing on the PR to say why. A cosmetic
call had become a single point of failure for the review pipeline.

So the contract here is deliberately lopsided:

  * Transient failures are retried on a bounded backoff.
  * Every failure — transient or not — is surfaced as a `::warning::`.
  * The exit code is **always 0**. A missing emoji must never take down the
    load-bearing work that follows it in the job.

That "always 0" is the whole point, and it is why this is separate from
`sdk_review_approve.py`, which is the mirror image: its label write *is*
load-bearing, so it reports failure rather than swallowing it (a silently
skipped label there strands an approval permanently).

Configuration (all via env, matching the sibling scripts):

    REPO                    owner/repo, required
    COMMENT_ID              triggering comment id; blank/absent → no-op
    REACTION                one of GitHub's reaction contents, default `eyes`
    REACT_MAX_ATTEMPTS      total attempts including the first, default 3
    REACT_BACKOFF_SECONDS   base backoff, doubled per retry, default 2
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
import time
from typing import Callable, Protocol


class Runner(Protocol):
    def __call__(self, *args, **kwargs) -> subprocess.CompletedProcess: ...


# GitHub's fixed reaction vocabulary. A typo here is a 422 on every run of a
# workflow that may only fire a few times a week, so it is worth catching
# locally instead of discovering it in a job log.
VALID_REACTIONS = frozenset(
    {
        "+1",
        "-1",
        "laugh",
        "confused",
        "heart",
        "hooray",
        "rocket",
        "eyes",
    }
)

DEFAULT_REACTION = "eyes"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2.0

# Module-local seams, so a test can replace the sleep or the subprocess call
# without patching the global `time`/`subprocess` modules out from under
# everything else in the process.
#
# Both are read inside `react()` rather than used as default argument values.
# A default binds the object at import time, so patching the module attribute
# afterwards has no effect — and a test that patches `subprocess.run` and then
# watches the real `gh` fail still sees exit 0, passing for the wrong reason.
_SLEEP: Callable[[float], None] = time.sleep
_RUN: Runner = subprocess.run

_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")

# Failure text that is worth a second attempt. Split from the HTTP-status check
# because these arrive as transport errors with no status at all.
_TRANSIENT_PHRASES = (
    "connection reset",
    "connection refused",
    "no such host",
    "i/o timeout",
    "timeout awaiting",
    "unexpected eof",
    "tls handshake",
    "server error",
)


def http_status(stderr: str) -> int | None:
    """The HTTP status `gh` reported, if it reported one."""
    match = _HTTP_STATUS.search(stderr)
    return int(match.group(1)) if match else None


def is_transient(stderr: str) -> bool:
    """Whether a failed `gh` invocation is worth retrying.

    Retryable: any 5xx (the 503 that motivated this script), 429, a rate-limit
    message, and bare transport errors that carry no status.

    Not retryable: 401/403 (token lacks the scope), 404 (comment deleted mid
    run), 422 (bad reaction content). Retrying those burns the job's remaining
    budget to arrive at the same answer, so they warn once and stop.
    """
    lowered = stderr.lower()
    status = http_status(stderr)
    if status is not None:
        if status == 403 and "rate limit" in lowered:
            # Primary/secondary throttles are reported as 403, not 429, and
            # unlike a genuine permission 403 they do clear on their own.
            return True
        return status >= 500 or status == 429
    if "rate limit" in lowered or "abuse detection" in lowered:
        return True
    return any(phrase in lowered for phrase in _TRANSIENT_PHRASES)


def react(
    repo: str,
    comment_id: str,
    reaction: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    runner: Runner | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> bool:
    """POST the reaction, retrying transient failures. True iff it landed.

    `runner` and `sleeper` are injectable so tests can drive the retry path
    and assert the backoff schedule without issuing an API call or spending
    the wait. See `_RUN`/`_SLEEP` for why they resolve here.
    """
    if runner is None:
        runner = _RUN
    if sleeper is None:
        sleeper = _SLEEP
    args = [
        "gh",
        "api",
        f"repos/{repo}/issues/comments/{comment_id}/reactions",
        "-X",
        "POST",
        "-f",
        f"content={reaction}",
        "--silent",
    ]

    for attempt in range(1, max_attempts + 1):
        try:
            result = runner(args, capture_output=True, text=True, check=False)
        except Exception as exc:  # noqa: BLE001 — see below
            # A missing or non-executable `gh` raises FileNotFoundError before
            # any CompletedProcess exists, and nothing else in this file would
            # catch it — the exception would escape main() and fail the step,
            # which is the exact always-exit-0 contract this script exists to
            # hold. The boundary is deliberately broad: ANY raise from the
            # spawn path is a reason to warn and degrade, never to fail.
            print(
                f"::warning::could not react :{reaction}: on comment "
                f"{comment_id} (runner raised): {exc}"
            )
            return False
        if result.returncode == 0:
            print(f"Reacted :{reaction}: on comment {comment_id}")
            return True

        stderr = (result.stderr or "").strip()
        last = attempt == max_attempts
        if not is_transient(stderr):
            print(
                f"::warning::could not react :{reaction}: on comment "
                f"{comment_id} (not retryable): {stderr}"
            )
            return False
        if last:
            break
        delay = backoff_seconds * (2 ** (attempt - 1))
        print(
            f"::notice::reaction attempt {attempt}/{max_attempts} failed "
            f"({stderr}); retrying in {delay:g}s"
        )
        try:
            sleeper(delay)
        except Exception as exc:  # noqa: BLE001 — same contract as above
            print(
                f"::warning::could not react :{reaction}: on comment "
                f"{comment_id} (sleeper raised): {exc}"
            )
            return False

    print(
        f"::warning::could not react :{reaction}: on comment {comment_id} "
        f"after {max_attempts} attempts: {stderr}"
    )
    return False


def _positive_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"::warning::{name}={raw!r} is not an integer; using {default}")
        return default
    return value if value > 0 else default


def _positive_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"::warning::{name}={raw!r} is not a number; using {default}")
        return default
    # `float()` happily returns inf/nan, and `inf > 0` is True — which would
    # reach `time.sleep(inf)` and raise OverflowError, failing the step. That
    # is exactly the always-exit-0 contract this script exists to hold, broken
    # by its own configuration parsing. `_positive_int` needs no equivalent:
    # `int('inf')` is a ValueError, already handled above.
    if not math.isfinite(value):
        print(f"::warning::{name}={raw!r} is not finite; using {default}")
        return default
    return value if value > 0 else default


def main() -> int:
    repo = (os.environ.get("REPO") or "").strip()
    comment_id = (os.environ.get("COMMENT_ID") or "").strip()
    reaction = (os.environ.get("REACTION") or "").strip() or DEFAULT_REACTION

    if not comment_id:
        # workflow_dispatch and schedule triggers have no comment to react to.
        # Not an error — the caller is allowed to invoke this unconditionally.
        print("No COMMENT_ID; nothing to react to.")
        return 0
    if not repo:
        print("::warning::REPO is unset; skipping reaction.")
        return 0
    if reaction not in VALID_REACTIONS:
        print(
            f"::warning::{reaction!r} is not a GitHub reaction "
            f"({', '.join(sorted(VALID_REACTIONS))}); skipping."
        )
        return 0

    react(
        repo,
        comment_id,
        reaction,
        max_attempts=_positive_int("REACT_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
        backoff_seconds=_positive_float(
            "REACT_BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS
        ),
    )
    # Always 0. See the module docstring: the reaction is never allowed to be
    # the reason a job fails.
    return 0


if __name__ == "__main__":
    sys.exit(main())
