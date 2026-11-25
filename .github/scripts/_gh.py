"""The one way a CI script calls the ``gh`` CLI.

Before this module, every script that shelled out to ``gh`` carried its own
wrapper, and each one re-decided timeout, retry, pagination and error shape
(FND-4924). Use this instead of a new ``subprocess.run(["gh", ...])``:

* ``gh(args)`` runs ``gh <args>`` with a timeout and returns stdout. A non-zero
  exit raises ``GhError``, which carries the HTTP status when gh printed one.
* ``gh_json(args)`` is ``gh`` plus ``json.loads``.
* ``gh_paginated(path, key=...)`` reads every page of a REST listing with
  ``gh api --paginate --slurp`` and flattens it in Python, so callers never run
  a ``--jq`` filter per page (see docs/standards/ci.md).
* ``is_transient(status, stderr)`` is the single rule for "worth retrying".

Retries: a transient failure (5xx, rate limit, network error, timeout) is
retried with exponential backoff. A call that is not idempotent — a POST that
creates something — passes ``idempotent=False``; it is then retried only on a
rate limit, which GitHub rejects before doing the write. A 5xx or a timeout on
such a call may already have landed, so it is raised rather than repeated.

Tests stub ``_gh.run`` (the subprocess seam) and get the real retry,
classification and parsing logic.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Sequence

# Long enough for a multi-page --paginate read, short enough that a stalled gh
# fails the step in minutes instead of running to the job timeout.
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_RETRIES = 2
BACKOFF_SECONDS = 2.0

JSON = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]

# gh api prints e.g. "gh: Not Found (HTTP 404)" on stderr for a non-2xx answer.
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
# Lower-cased stderr fragments of failures that never reached GitHub's
# application layer, or were refused for load: retrying is the right answer.
_NETWORK_MARKERS = (
    "connection reset",
    "connection refused",
    "i/o timeout",
    "tls handshake timeout",
    "timeout awaiting response headers",
    "unexpected eof",
    "no such host",
    "temporary failure in name resolution",
    "server misbehaving",
)
_RATE_LIMIT_MARKERS = ("rate limit", "abuse detection")


class GhError(Exception):
    """``gh`` exited non-zero, or did not finish within its timeout.

    ``status`` is the HTTP status gh reported, or ``None`` when there was none
    (a transport failure, a timeout, or a non-``api`` subcommand).
    """

    def __init__(
        self,
        args: Sequence[str],
        status: int | None,
        stderr: str,
        *,
        timed_out: bool = False,
    ) -> None:
        self.command = list(args)
        self.status = status
        self.stderr = stderr
        self.timed_out = timed_out
        what = (
            "timed out"
            if timed_out
            else f"failed (HTTP {status})"
            if status
            else "failed"
        )
        super().__init__(f"gh {' '.join(self.command[:3])} {what}: {stderr.strip()}")


def run(
    cmd: list[str], *, input: str | None, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Single subprocess seam: tests stub this, not ``subprocess.run``."""
    return subprocess.run(
        cmd, input=input, capture_output=True, text=True, check=False, timeout=timeout
    )


def parse_status(stderr: str) -> int | None:
    """The HTTP status in gh's error output, if it printed one."""
    matches = _HTTP_STATUS.findall(stderr)
    return int(matches[-1]) if matches else None


def is_rate_limited(status: int | None, stderr: str) -> bool:
    """A rate limit, as opposed to a permission 403. Both can arrive as 403."""
    if status == 429:
        return True
    lowered = stderr.lower()
    return status in (403, None) and any(m in lowered for m in _RATE_LIMIT_MARKERS)


def is_transient(status: int | None, stderr: str) -> bool:
    """Would the same call plausibly succeed if repeated?

    True for 5xx, a rate limit (429, or a 403 that says so), and a transport
    failure with no status. False for every other 4xx: a 404 or a permission
    403 will answer the same way next time.
    """
    if status is not None and status >= 500:
        return True
    if is_rate_limited(status, stderr):
        return True
    if status is None:
        lowered = stderr.lower()
        return any(m in lowered for m in _NETWORK_MARKERS)
    return False


def gh(
    args: Sequence[str],
    *,
    input: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    idempotent: bool = True,
) -> str:
    """Run ``gh <args>`` and return its stdout; raise ``GhError`` on failure."""
    cmd = ["gh", *args]
    attempt = 0
    while True:
        try:
            result = run(cmd, input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            error = GhError(
                args, None, f"no answer within {timeout:g}s", timed_out=True
            )
            # A timed-out write may still have happened on GitHub's side.
            retryable = idempotent
        else:
            if result.returncode == 0:
                return result.stdout
            stderr = result.stderr or ""
            status = parse_status(stderr)
            error = GhError(args, status, stderr)
            retryable = (
                is_transient(status, stderr)
                if idempotent
                else is_rate_limited(status, stderr)
            )
        if not retryable or attempt >= retries:
            raise error
        attempt += 1
        delay = BACKOFF_SECONDS * 2 ** (attempt - 1)
        print(
            f"::warning::{error} — retrying in {delay:g}s "
            f"(attempt {attempt + 1}/{retries + 1})",
            flush=True,
        )
        time.sleep(delay)


def gh_json(
    args: Sequence[str],
    *,
    input: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    idempotent: bool = True,
) -> JSON:
    """``gh`` whose stdout is one JSON document."""
    out = gh(args, input=input, timeout=timeout, retries=retries, idempotent=idempotent)
    try:
        parsed: JSON = json.loads(out)
    except json.JSONDecodeError as exc:
        raise GhError(args, None, f"stdout is not JSON: {exc}") from exc
    return parsed


def flatten_pages(pages: JSON, key: str | None = None) -> list[dict[str, JSON]]:
    """Join the pages ``gh api --paginate --slurp`` returns into one list.

    Each page is either a JSON array (``/pulls``) or an object holding the array
    under ``key`` (``/check-runs`` → ``check_runs``), and every item is a JSON
    object, as it is for every GitHub REST listing. Anything else is an error,
    not an empty page: silently dropping it would under-count.
    """
    if not isinstance(pages, list):
        raise ValueError(f"expected a list of pages, got {type(pages).__name__}")
    items: list[dict[str, JSON]] = []
    for page in pages:
        if key is None:
            entries = page
        else:
            entries = page.get(key) if isinstance(page, dict) else None
        if not isinstance(entries, list):
            raise ValueError(
                f"page has no {key!r} array" if key else "expected an array page"
            )
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"expected an object item, got {type(entry).__name__}")
            items.append(entry)
    return items


def gh_paginated(
    path: str,
    *,
    key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
) -> list[dict[str, JSON]]:
    """Every item of a paginated REST listing at ``path``.

    ``key`` names the array inside each page when the endpoint wraps it in an
    object (``check_runs``, ``workflow_runs``, ``jobs``); omit it for endpoints
    that return a bare array.
    """
    args = ["api", "--paginate", "--slurp", path]
    pages = gh_json(args, timeout=timeout, retries=retries)
    try:
        return flatten_pages(pages, key)
    except ValueError as exc:
        raise GhError(args, None, f"unexpected page shape: {exc}") from exc
