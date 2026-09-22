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

NOTE (drift): ``e2e-tenant-lease/e2e_tenant_lease.py`` still carries its own copy
of these primitives — a composite action is checked out in isolation and cannot
import this module at runtime, so its migration onto this module is a tracked
fast-follow, not part of the PR that introduced this file.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime

# Transport retry budget (transient curl / 5xx). Kept identical to the lease.
_TRANSPORT_ATTEMPTS = 3
_TRANSPORT_BACKOFF_SECONDS = 2

_COMPLETED = "completed"

_SAFE_SLUG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def slug(value: str, *, default: str = "default") -> str:
    """Reduce a free-text key to one safe git ref path component."""
    cleaned = "".join(
        char if char in _SAFE_SLUG_CHARS else "-" for char in value.strip().lower()
    )
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
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


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
    run_started = _timestamp(body.get("created_at"))
    if run_started is not None and holder.acquired_at < run_started:
        print(
            f"::warning::the lease record for run {holder.run_id} claims an "
            "acquisition time before that run existed, so it is not trustworthy; "
            "ignoring the TTL and treating the lease as held."
        )
        return True
    if now - holder.acquired_at > ttl_seconds:
        print(
            f"::warning::run {holder.run_id} has held the lease for "
            f"{int(now - holder.acquired_at)}s, past the {ttl_seconds}s TTL, and "
            f"still reports '{body.get('status')}' — breaking it."
        )
        return False
    return True
