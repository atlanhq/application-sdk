"""Shared GitHub-ref transport: the CAS-on-a-ref primitives behind the CI leases.

Both the ``(app, cloud)`` tenant lease and the DataForge source refcount lock on
the same GitHub mechanism — ``POST /git/refs`` on an existing name returns 422,
an atomic test-and-set evaluated by GitHub — and both have to tell a genuine
permission 403 apart from a rate-limit 403 (FND-702: conflating them is how a
lease disables itself under contention, which is exactly when the shared
per-repository budget runs out). That transport is factored here so there is one
copy to fix, not several that drift.

curl, not ``gh api``: the non-2xx codes ARE the mechanism (422 = held), so the
caller needs the status, not a command failure. The token is fed on stdin so it
never reaches argv. Every read/write returns a status the caller interprets;
nothing here raises on a 4xx except a rate limit, which is a ``RateLimited`` so a
call site cannot forget to distinguish it from a denial.

Both callers — ``e2e_tenant_lease.py`` and ``dataforge_source_lifecycle.py`` —
import this module rather than carrying a copy (FND-2674). The lease used to be a
composite action, which is checked out in isolation and so could not import a
sibling script; it is now run off the same ``job.workflow_sha`` sparse checkout
its ``verify`` mode always used, which is what made the single copy possible.

What belongs here is the primitive — how a ref is created, read, listed and
deleted, and how liveness is decided. What does NOT belong here is what a caller
does with the answer: the lease treats a live holder as "wait", the DataForge
refcount treats it as "do not pause". Same question, different policy, and the
policy stays at the call site.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# Transport retry budget (transient curl / 5xx). Kept identical to the lease.
_TRANSPORT_ATTEMPTS = 3
_TRANSPORT_BACKOFF_SECONDS = 2

_COMPLETED = "completed"

# Characters safe in a single ref path component. Deliberately narrower than the
# rules the VCS itself applies: the keys come from workflow inputs, and a ref name
# is the one place where "mostly valid" turns into a 422 nobody expected.
#
# The two copies this module reconciles disagreed about ".", and the narrower set
# won (FND-2674) because it is the one that renames nothing: the DataForge pin id
# genuinely contains a dot today, while no app or cloud name does, so dropping "."
# leaves BOTH sides' ref names byte-identical and keeping it would have moved the
# refcount's prefix under live runs.
#
# That makes the ".." and ".lock" guards below unreachable for now. They stay
# because they guard the char SET, not this spelling of it: widening the set is a
# one-line change, and a ref component that is ".." or ends ".lock" is rejected
# outright — a failure mode worth keeping closed by construction rather than by
# remembering.
_SAFE_SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


def slug(value: str, *, default: str = "default") -> str:
    """Reduce a free-text key to one safe ref path component.

    An empty result maps to ``default`` rather than producing an empty component,
    which is rejected — the lease's single-tenant path spells its cloud as a
    defined-but-empty string and legitimately lands here.
    """
    cleaned = "".join(
        char if char in _SAFE_SLUG_CHARS else "-" for char in value.strip().lower()
    )
    # ".." is rejected, a leading "-" is hostile to CLI tooling, and a component
    # ending ".lock" is reserved.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "-")
    cleaned = cleaned.strip("-._")
    while cleaned.endswith(".lock"):
        cleaned = cleaned[: -len(".lock")].strip("-._")
    return cleaned or default


@dataclass(frozen=True)
class Holder:
    """Who holds a ref-lease, read back from the blob the ref points at."""

    run_id: int
    attempt: int
    acquired_at: float | None

    def is_me(self, run_id: int, attempt: int) -> bool:
        return self.run_id == run_id and self.attempt == attempt

    def run_url(self, repo: str) -> str:
        """Where to look at the holding run. Derived from its identity, so it is
        a property of the holder rather than of what a caller does with it."""
        return f"https://github.com/{repo}/actions/runs/{self.run_id}"


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: object | None

    @property
    def message(self) -> str:
        return self.body.get("message", "") if isinstance(self.body, dict) else ""


class RateLimited(Exception):
    """The API rate-limited us. Carries Retry-After when GitHub supplied one.

    An exception, not a return value, so every call site is forced to tell it
    apart from a permission denial — the FND-702 conflation.
    """

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single subprocess seam so tests can stub the HTTP client."""
    return subprocess.run(cmd, **kwargs)


def _parse_http(raw: str, label: str) -> Response:
    """Split a ``curl -i`` response into status, headers and parsed body."""
    text = raw.replace("\r\n", "\n")
    if "\n\n" not in text:
        raise SystemExit(f"::error::unexpected response for {label}: {text[:300]!r}")
    header_block, _, body = text.partition("\n\n")
    lines = header_block.splitlines()
    try:
        status_code = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise SystemExit(
            f"::error::could not parse HTTP status line for {label}: "
            f"{(lines[0] if lines else '')!r}"
        )
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    if not body.strip():
        return Response(status_code, headers, None)
    try:
        return Response(status_code, headers, json.loads(body))
    except json.JSONDecodeError:
        # A non-JSON body (an HTML proxy error page) must not crash the caller
        # before it can act on the status code.
        return Response(status_code, headers, None)


def gh_request(
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    sleep=time.sleep,
) -> Response:
    """Call the GitHub API, returning the status rather than raising on 4xx.

    Retries transport failures and 5xx (transient); a 4xx is an answer the caller
    interprets. The token is read from stdin (``-K -``) so it never appears in
    argv, where anything on the same runner could read it while the request runs.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("::error::GH_TOKEN (or GITHUB_TOKEN) must be set")

    cmd = [
        "curl",
        "-sS",
        "-i",
        "--max-time",
        "30",
        "-X",
        method,
        "-K",
        "-",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
    ]
    config = f'header = "Authorization: Bearer {token}"\n'
    if payload is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(payload)]
    cmd.append(f"https://api.github.com/{path}")

    label = f"{method} {path}"
    for transport_attempt in range(1, _TRANSPORT_ATTEMPTS + 1):
        result = run(cmd, input=config, capture_output=True, text=True, check=False)
        last = transport_attempt == _TRANSPORT_ATTEMPTS
        if result.returncode != 0:
            if last:
                raise SystemExit(
                    f"::error::curl failed for {label} after "
                    f"{_TRANSPORT_ATTEMPTS} attempts: {result.stderr.strip()}"
                )
            print(
                f"::warning::transport failure on {label} "
                f"(attempt {transport_attempt}/{_TRANSPORT_ATTEMPTS}): "
                f"{result.stderr.strip()} — retrying."
            )
        else:
            response = _parse_http(result.stdout, label)
            if response.status < 500 or last:
                return response
            print(
                f"::warning::{label} returned HTTP {response.status} "
                f"(attempt {transport_attempt}/{_TRANSPORT_ATTEMPTS}) — retrying."
            )
        sleep(_TRANSPORT_BACKOFF_SECONDS * 2 ** (transport_attempt - 1))
    raise SystemExit(f"::error::exhausted transport attempts for {label}")


def _rate_limited(response: Response) -> bool:
    """Is this a rate limit rather than a permission problem?

    Both arrive as 403. Detected from headers first (``x-ratelimit-remaining: 0``
    or a ``retry-after``), message second, so it does not hinge on GitHub's prose.
    A 429 is a rate limit by definition.
    """
    if response.status == 429:
        return True
    if response.status != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    if "retry-after" in response.headers:
        return True
    message = response.message.lower()
    return "rate limit" in message or "abuse detection" in message


def _denied(response: Response) -> bool:
    """A genuine permission answer. GitHub 404s resources it will not admit
    exist, so 404 counts; a rate limit explicitly does not."""
    return response.status in (401, 403, 404) and not _rate_limited(response)


def _retry_after(response: Response) -> int | None:
    try:
        return max(0, int(response.headers["retry-after"]))
    except (KeyError, TypeError, ValueError):
        return None


def raise_if_rate_limited(response: Response) -> None:
    """Convert a rate-limit response into ``RateLimited`` (Retry-After carried)."""
    if _rate_limited(response):
        raise RateLimited(_retry_after(response))


# ── ref / blob primitives ─────────────────────────────────────────────────────


def write_blob(repo: str, content: str) -> str | None:
    """Write a blob, returning its sha, or None if writes are not permitted.

    Raises ``RateLimited`` when the API is merely busy — never conflated with a
    permission denial (FND-702).
    """
    response = gh_request(
        "POST", f"repos/{repo}/git/blobs", {"content": content, "encoding": "utf-8"}
    )
    if (
        response.status in (200, 201)
        and isinstance(response.body, dict)
        and response.body.get("sha")
    ):
        return str(response.body["sha"])
    raise_if_rate_limited(response)
    if _denied(response):
        return None
    raise SystemExit(
        f"::error::could not write a git blob in {repo}: "
        f"HTTP {response.status} {response.message!r}"
    )


def try_create_ref(repo: str, ref: str, sha: str) -> str:
    """One atomic ref creation. "acquired", "occupied" or "denied"; raises RateLimited.

    422 "already exists" IS the lock held by someone else — GitHub evaluates ref
    creation atomically, so of N simultaneous callers exactly one sees 201.
    """
    response = gh_request("POST", f"repos/{repo}/git/refs", {"ref": ref, "sha": sha})
    if response.status in (200, 201):
        return "acquired"
    if response.status == 422 and "already exists" in response.message.lower():
        return "occupied"
    raise_if_rate_limited(response)
    if _denied(response):
        return "denied"
    raise SystemExit(
        f"::error::could not create ref {ref} in {repo}: "
        f"HTTP {response.status} {response.message!r}"
    )


def read_ref_target(repo: str, ref: str) -> str | None:
    """The sha one named ref points at, or None if it is unheld or unreadable.

    One API call, and the cheap half of a waiting poll. None deliberately
    conflates "nobody holds it" with "we could not tell": for a caller racing a
    CAS both mean "try the CAS", and the CAS is the authority. A caller that
    cannot tolerate that conflation wants ``list_matching_refs``, which
    distinguishes an empty listing from a failed one.
    """
    # git/ref/<name> wants the ref without the leading "refs/".
    response = gh_request("GET", f"repos/{repo}/git/ref/{ref.removeprefix('refs/')}")
    if response.status == 404 or not isinstance(response.body, dict):
        return None
    if response.status >= 400:
        print(f"::warning::could not read the ref {ref} (HTTP {response.status}).")
        return None
    target = response.body.get("object") or {}
    sha = target.get("sha") if isinstance(target, dict) else None
    return str(sha) if sha else None


def delete_ref(repo: str, ref: str) -> bool:
    """Delete a ref. False ⇒ it was already gone, which is not a fault."""
    response = gh_request(
        "DELETE", f"repos/{repo}/git/refs/{ref.removeprefix('refs/')}"
    )
    return response.status in (200, 204)


class RefListError(Exception):
    """A ref listing could not be read (non-404 failure). Distinct from an empty
    listing so a caller can err safe instead of reading it as 'nothing there'."""


def list_matching_refs(repo: str, prefix: str) -> list[tuple[str, str]]:
    """Every ref under ``prefix`` as (ref, target-sha) pairs.

    A 404 is a genuine empty listing → ``[]``. ANY OTHER failure (401/403/429/an
    unexpected shape/a retry-exhausted 5xx) raises ``RefListError`` — it must NOT
    be conflated with 'no refs', because a caller that pauses/reaps on an empty
    listing would then act on a transient API failure.
    """
    response = gh_request(
        "GET", f"repos/{repo}/git/matching-refs/{prefix.removeprefix('refs/')}"
    )
    if response.status == 404:
        return []
    if response.status >= 400 or not isinstance(response.body, list):
        raise RefListError(
            f"could not list refs under {prefix} in {repo} (HTTP {response.status})"
        )
    pairs: list[tuple[str, str]] = []
    for item in response.body:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "")
        obj = item.get("object") or {}
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if ref and sha:
            pairs.append((ref, str(sha)))
    return pairs


def read_blob_json(repo: str, blob_sha: str) -> dict | None:
    """Decode a base64 JSON blob. None if unreadable — callers treat that as
    'record missing', which is safe for the acquired_at/TTL backstop."""
    response = gh_request("GET", f"repos/{repo}/git/blobs/{blob_sha}")
    if response.status >= 400 or not isinstance(response.body, dict):
        return None
    content = response.body.get("content")
    if not isinstance(content, str):
        return None
    try:
        return json.loads(base64.b64decode(content).decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _timestamp(value: object) -> float | None:
    """Parse an ISO-8601 API timestamp (or an epoch number) to a POSIX float.

    A naive timestamp is read as UTC rather than as runner-local time: the runner
    can sit in any zone, and reading a UTC instant as local would shift the run's
    start by hours — in the direction that makes a hold look longer than it was,
    which is the direction that breaks a live holder.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def holder_is_live(
    repo: str,
    holder: Holder,
    *,
    ttl_seconds: int,
    now: float,
) -> bool:
    """Is a ref-lease still legitimately held?

    Errs towards True: reading a transient API failure as 'dead' would break a
    live holder's lease, whereas erring live only costs the waiter/pauser one more
    cycle. A completed run, a deleted run, or a hold past the TTL backstop is dead.
    """
    response = gh_request("GET", f"repos/{repo}/actions/runs/{holder.run_id}")
    if response.status == 404:
        return False
    if response.status >= 400 or not isinstance(response.body, dict):
        print(
            f"::warning::could not read run {holder.run_id} in {repo} "
            f"(HTTP {response.status}); treating its lease as still held."
        )
        return True
    body = response.body
    if body.get("status") == _COMPLETED:
        return False
    if ttl_seconds <= 0 or holder.acquired_at is None:
        return True

    # Sanity-check the holder's own timestamp against its run before trusting it.
    # A ref cannot have been taken before the run that took it existed, so an
    # earlier acquired_at means the record is wrong — a clock far out of step, or
    # a corrupted write. Believing it would make the hold time enormous and break
    # a LIVE holder on the first poll, which is the expensive direction; erring
    # towards "live" costs a waiter one poll interval.
    #
    # A run object with no created_at at all gets the same treatment. It should
    # never happen, so it means something has changed underneath us, and the
    # holder's run status is then the only evidence worth acting on.
    run_started = _timestamp(body.get("created_at"))
    if run_started is None:
        return True
    if holder.acquired_at < run_started:
        print(
            f"::warning::the lease record for run {holder.run_id} claims an "
            "acquisition time before that run existed, so it is not trustworthy; "
            "ignoring the TTL and treating the lease as held. Its run status is "
            "still authoritative."
        )
        return True

    held_for = now - holder.acquired_at
    if held_for > ttl_seconds:
        print(
            f"::warning::run {holder.run_id} has held the lease for "
            f"{int(held_for)}s, past the {ttl_seconds}s TTL, and still reports "
            f"'{body.get('status')}' — breaking it. If that run is genuinely "
            "still working, raise ttl-seconds."
        )
        return False
    return True
