"""Fetch a pinned GitHub release asset into a local cache, atomically.

Shared by the two dev-tooling binaries the SDK downloads on first use —
``daprd`` (``application_sdk.dev._dapr``) and ``pkl``
(``application_sdk.dev.pkl``). Each keeps its own platform-name map (Dapr
spells 64-bit ARM ``arm64``, pkl ``aarch64``) and its own reporting; this
module owns the part that must not drift between them:

* a socket timeout, so a stalled download fails instead of hanging the caller;
* bounded retries that skip a 4xx — a missing asset for a wrong pin will not
  appear on retry;
* a Content-Length check, so a body cut short is retried rather than cached;
* staging beside the target and ``os.replace``-ing into place, so an
  interrupted download can never leave a truncated binary at the cache path.
"""

from __future__ import annotations

import http.client
import os
import shutil
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_ATTEMPTS = 3

# Network failures a fresh request can fix. ``HTTPError`` is a ``URLError``, so
# it lands here too and is narrowed by status in ``is_retryable_network_error``.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    urllib.error.URLError,
    http.client.IncompleteRead,
    ConnectionError,
    TimeoutError,
    # A peer that drops the connection mid-body without a TLS close_notify
    # raises this from the read, not wrapped in ``URLError``.
    ssl.SSLEOFError,
)


def is_retryable_network_error(exc: BaseException) -> bool:
    """True for a failure another attempt can fix: 5xx, 429, or a network error."""
    if isinstance(exc, urllib.error.HTTPError):
        # 4xx (a missing asset for this version/platform) will not change on retry.
        return exc.code >= 500 or exc.code == 429
    return isinstance(exc, NETWORK_ERRORS)


def fetch_to_file(url: str, dest: Path, *, timeout: float) -> None:
    """Stream *url* into *dest*, bounded by a socket timeout.

    Raises ``http.client.IncompleteRead`` when the body is shorter than the
    response's ``Content-Length``: ``HTTPResponse`` ends a length-delimited body
    quietly when the peer closes early, so without this check a cut-short
    download would be published as the binary.
    """
    with (
        urllib.request.urlopen(url, timeout=timeout) as resp,
        dest.open("wb") as out,
    ):
        shutil.copyfileobj(resp, out)
        declared = resp.headers.get("Content-Length")
    if declared is not None and declared.isdigit():
        expected = int(declared)
        actual = dest.stat().st_size
        if actual < expected:
            raise http.client.IncompleteRead(b"", expected - actual)


def _reserve_sibling(dest: Path, suffix: str) -> Path:
    """Create an empty, uniquely named file beside *dest* and return its path."""
    fd, name = tempfile.mkstemp(dir=dest.parent, prefix=f"{dest.name}.", suffix=suffix)
    os.close(fd)
    return Path(name)


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _publish(staged: Path, dest: Path) -> None:
    """``os.replace`` *staged* onto *dest*, tolerating a concurrent publisher.

    On Windows, replacing a file another writer is replacing at the same moment
    fails with ``PermissionError`` (access denied) for the loser; POSIX renames
    atomically and never does. Every writer fetched the same pinned asset, so
    when the loser finds *dest* in place the outcome is the one it wanted.
    """
    try:
        os.replace(staged, dest)
    except PermissionError:
        if os.name != "nt" or not dest.exists():
            raise


def fetch_release_asset(
    url: str,
    dest: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff_seconds: float,
    retryable: Callable[[BaseException], bool] = is_retryable_network_error,
    fetch: Callable[[str, Path], None] | None = None,
    extract: Callable[[Path, Path], None] | None = None,
    on_retry: Callable[[int, int, BaseException], None] | None = None,
) -> None:
    """Download *url* and publish it, executable, at *dest*.

    Args:
        url: The release asset to fetch.
        dest: Where the binary is published. Nothing is written there until the
            whole download (and *extract*, if given) has succeeded.
        timeout: Socket timeout for each attempt, in seconds.
        attempts: Total attempts, including the first.
        backoff_seconds: Sleep before attempt *n + 1* is ``backoff_seconds * n``.
        retryable: Classifies a failure; anything it rejects is re-raised at once.
        fetch: ``(url, path)`` that writes the asset to *path*. Defaults to
            :func:`fetch_to_file` with *timeout*; a caller passing its own is
            responsible for bounding it.
        extract: ``(downloaded, staged)`` that turns the downloaded asset into
            the binary at *staged* — for an archive. Without it the asset is the
            binary. Runs inside the retry loop, so a truncated archive is
            retried when *retryable* accepts the error it raises.
        on_retry: ``(attempt, attempts, exc)`` called before each retry, so the
            caller reports it through its own channel.

    Raises:
        The last failure, unchanged, once it is not retryable or *attempts* are
        exhausted.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call, not per process: two threads filling the same missing
    # cache entry must not share (and unlink) each other's staging files.
    downloaded = _reserve_sibling(dest, ".download")
    staged = _reserve_sibling(dest, ".tmp") if extract else downloaded
    try:
        for attempt in range(1, attempts + 1):
            try:
                if fetch is None:
                    fetch_to_file(url, downloaded, timeout=timeout)
                else:
                    fetch(url, downloaded)
                if extract is not None:
                    extract(downloaded, staged)
                break
            except Exception as exc:
                if attempt == attempts or not retryable(exc):
                    raise
                if on_retry is not None:
                    on_retry(attempt, attempts, exc)
                # conformance: ignore[P060] synchronous single-process dev tooling (pinned binary download) — one caller per machine, so no fleet retry storm; retry_async is async-only
                time.sleep(backoff_seconds * attempt)
        _make_executable(staged)
        _publish(staged, dest)
    finally:
        downloaded.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
