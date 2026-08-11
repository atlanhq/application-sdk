#!/usr/bin/env python3
"""A real ``(app, cloud)`` tenant lease for e2e runs — mutual exclusion via an
atomic compare-and-swap on a single git ref.

Why this exists rather than a ``concurrency:`` group
---------------------------------------------------
A tenant is a shared *mutable* resource: ``prepare-tenant`` installs a version
onto it and every e2e leg then asserts it is testing that version (FND-31). So
exclusive access has to span from the install to the last leg, and
``concurrency:`` structurally cannot do that:

* It is per-JOB. A group on ``prepare-tenant`` is released the moment that job
  ends, which is before any leg has started.
* It holds exactly ONE pending run per group. A third arrival does not queue —
  it cancels the run that was waiting, before that run is ever given a runner,
  with no log output at all. That is FND-218.

The protocol
------------
One ref per tenant, with a FIXED name::

    refs/e2e-tenant-lease/<app>/<cloud>/holder

``POST /git/refs`` on a name that already exists returns 422. That is an atomic
test-and-set evaluated by GitHub, and it is the whole of the mutual exclusion:
exactly one contender can get 201 for a given ref, no matter how many try at
once or in what order. 422 means somebody else holds the tenant.

The ref points at a **blob** holding the holder's identity (run id, attempt, and
the time it acquired). A ref can target a blob — verified against the API — which
is what lets a single atomic creation both take the lease and record who took it.
A waiter reads that blob to find out whose lease it is, so it can tell a live
holder from a dead one.

Why not an ordered queue (and the bug that taught us)
----------------------------------------------------
The first version of this module was a queue: every run created a ticket ref
named after itself, and the lease belonged to whichever live ticket sorted lowest
by ``(run_id, run_attempt)``. That was wrong, and it failed on its first
concurrent run — both contenders acquired and both installed onto the same
tenants:

* ``run_id`` increases with run *creation*, which is not the order runs reach
  the lease job. In the observed failure the lower-id run got there 15s later.
* A total order gives FIFO *fairness*; it does not give *exclusion*. A run that
  only checks the tickets ordered ahead of it never notices that a run behind it
  already holds the lease — so the late lower-id arrival took a lease that a live
  run was already holding.

Ordering-based exclusion only works if every contender's ticket exists before any
contender decides, which nothing enforces. Exclusion needs an atomic primitive,
so this version uses one and derives nothing from ids.

The cost is fairness: acquisition is a scramble, not a queue, so a waiter can in
principle be repeatedly beaten. That is bounded by the wait budget, which fails
loudly and names the holder — a run that starves gets a red job saying the tenant
was busy, not silence. Correctness first; FIFO was the property that turned out
to be unaffordable.

API budget
----------
``GITHUB_TOKEN`` allows 1000 requests/hour **per repository**, and every matrix
leg of a run shares that one budget. A waiting poll therefore has to be cheap or
the lease starves itself of API calls precisely when several legs are queueing —
and a rate-limit 403 looks exactly like a permission 403, which used to fail the
lease open. So:

* A waiting poll costs **two** calls: read the lease ref, and check the holding
  run's status. The holder record is memoised by the ref's target sha, so it is
  only re-read when the lease actually changes hands.
* The identity blob and the CAS are only paid when the ref *looks* free, which
  makes the acquisition path 4 calls and the steady-state waiting path 2. The
  pre-read is purely an optimisation: the CAS is still the authority, and losing
  a race after the ref looked free is handled like any other 422.
* Rate limiting is detected separately from permission denial and never
  fail-opens. See ``_rate_limited``.

Liveness, not heartbeats
------------------------
A lease is breakable when the holder's *run* is over. GitHub reports run status
directly, so there is nothing to heartbeat and no lock to strand. This is also
the part ``if: always()`` cannot cover: GitHub *cancels* queued jobs rather than
running them, so a cancelled run's release job never starts at all. Any later
contender reaps the dead holder, which makes a leaked lease self-healing.

A TTL on lease-HOLD time (from the ``acquired_at`` the holder wrote into its own
blob, not from run age) is the backstop for a run the API keeps reporting as live
forever. The two directions are not symmetric: breaking a lease too late costs a
blocked tenant that a waiter is already complaining loudly about, whereas breaking
one too early reaps a LIVE mid-install holder and puts a second installer on the
tenant — the exact race this module exists to close. So it errs long, and that
sizing is load-bearing for release safety as well as for acquisition: see
``release``.

Failure posture
---------------
* **No permission to write refs** is a ``::warning::`` and the run proceeds
  *without* a lease. The lease reduces contention; it is not what keeps a
  wrong-version run from passing — every leg re-asserts the installed version
  itself (FND-31). Making an ungrantable lease fail the run would turn a safety
  improvement into a new way for e2e to go red fleet-wide.

  Note the limit of that backstop, which is easy to overstate: the version assert
  catches a *wrong-version* run, not two runs installing the *same* version and
  fighting over one tenant. Two dispatches of one commit do exactly that, and it
  surfaces as a worker/task-queue failure rather than a version mismatch. So
  fail-open is a real reduction in protection, not a no-op — which is why a rate
  limit must never be allowed to trigger it.
* **Not acquired within the wait budget** fails loudly, naming the holding run.

Co-located with the composite action, and pinned ``@main`` by every consumer on
purpose: all contenders must agree on the ref name, so the protocol must not vary
with whichever ref a caller happens to have checked out.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# The lease ref lives outside refs/heads and refs/tags on purpose: a branch would
# fire `push` events (and show up in the branch list), a tag would pollute the
# release surface. A custom namespace is inert — nothing watches it.
REF_NAMESPACE = "e2e-tenant-lease"

# The only GitHub run status that means "over". Everything else — queued,
# in_progress, requested, waiting, pending — is treated as live, so an unfamiliar
# future status errs towards leaving a peer's lease alone.
_COMPLETED = "completed"

# Ceiling on how long a Retry-After will be honoured, so a long secondary-limit
# backoff cannot swallow the whole wait budget in one sleep.
_MAX_RETRY_AFTER = 300

# Characters safe in a single git ref path component. Deliberately narrower than
# git's own rules: app and cloud names come from workflow inputs, and a ref name
# is the one place where "mostly valid" turns into a 422 nobody expected.
_SAFE_SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


def slug(value: str, *, default: str = "default") -> str:
    """Reduce a free-text key to one safe git ref path component.

    ``cloud`` is legitimately empty on the single-tenant path (the fallback
    matrix leg spells it as a defined-but-empty string), so an empty result maps
    to ``default`` rather than producing an empty component, which git rejects.
    """
    cleaned = "".join(
        char if char in _SAFE_SLUG_CHARS else "-" for char in value.strip().lower()
    )
    # git rejects "..", a leading "-" is hostile to CLI tooling, and a component
    # ending ".lock" is reserved.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "-")
    cleaned = cleaned.strip("-._")
    while cleaned.endswith(".lock"):
        cleaned = cleaned[: -len(".lock")].strip("-._")
    return cleaned or default


def lease_ref(app: str, cloud: str) -> str:
    """The single ref every contender for one tenant races to create.

    Fixed for a given tenant — that is the point. The name carries no per-run
    component, because a name only one run can guess is a name that cannot
    collide, and collision is precisely the signal this design needs.
    """
    return f"refs/{REF_NAMESPACE}/{slug(app, default='app')}/{slug(cloud)}/holder"


@dataclass(frozen=True)
class Holder:
    """Who holds a lease, read back from the blob the lease ref points at."""

    run_id: int
    attempt: int
    acquired_at: float | None

    def run_url(self, repo: str) -> str:
        return f"https://github.com/{repo}/actions/runs/{self.run_id}"

    def is_me(self, run_id: int, attempt: int) -> bool:
        return self.run_id == run_id and self.attempt == attempt


@dataclass(frozen=True)
class Response:
    """One API answer. Headers are carried because rate-limit detection needs
    them, and mistaking a rate limit for a permission denial fails the lease
    open under contention."""

    status: int
    headers: dict[str, str]
    body: object | None

    @property
    def message(self) -> str:
        return self.body.get("message", "") if isinstance(self.body, dict) else ""


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the HTTP client."""
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
        # A non-JSON body (an HTML error page from a proxy) must not crash the
        # caller before it can report the status code, which is the part that
        # decides what happens next.
        return Response(status_code, headers, None)


def gh_request(method: str, path: str, payload: dict | None = None) -> Response:
    """Call the GitHub API, returning the status rather than raising on 4xx.

    curl, not ``gh api``, for the same reason ``poll_check_runs_gate.py`` uses it:
    ``gh api`` treats every non-2xx as a command failure and prints its own
    diagnostic instead of the response. Here the non-2xx codes are the entire
    mechanism — 422 on ref creation IS the lock being held, and it has to be
    told apart from 403-permission and 403-rate-limit, which mean opposite things.
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
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        "-H",
        f"Authorization: Bearer {token}",
    ]
    if payload is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(payload)]
    cmd.append(f"https://api.github.com/{path}")

    result = run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"::error::curl failed for {method} {path}: {result.stderr}")
    return _parse_http(result.stdout, f"{method} {path}")


def _rate_limited(response: Response) -> bool:
    """Is this a rate limit rather than a permission problem?

    Both arrive as 403, and conflating them is how the lease used to switch
    itself OFF exactly when it was needed: several legs queueing for tenants is
    also when the shared per-repository budget runs out, and the fail-open path
    then let every waiting run proceed onto the tenant unserialised.

    Detected from the headers first (``x-ratelimit-remaining: 0``, or a
    ``retry-after`` on a secondary limit) and the message second, so it does not
    hinge on GitHub's exact prose.
    """
    # 429 means this by definition, whatever else the body says.
    if response.status == 429:
        return True
    if response.status != 403:
        return False
    # A 403 is ambiguous, so it needs evidence. Headers first, message second, so
    # detection does not hinge on GitHub's exact prose.
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    if "retry-after" in response.headers:
        return True
    message = response.message.lower()
    return "rate limit" in message or "abuse detection" in message


def _denied(response: Response) -> bool:
    """A genuine permission answer. GitHub 404s resources it will not admit
    exist, so 404 counts, but a rate limit explicitly does not."""
    return response.status in (401, 403, 404) and not _rate_limited(response)


def _retry_after(response: Response) -> int | None:
    try:
        return max(0, int(response.headers["retry-after"]))
    except (KeyError, TypeError, ValueError):
        return None


class RateLimited(Exception):
    """The API rate-limited us. Carries Retry-After when GitHub supplied one.

    An exception rather than a return value so every call site cannot forget to
    distinguish it from a permission denial — which is the mistake that made the
    lease fail open under contention.
    """

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


def create_identity_blob(
    repo: str, run_id: int, attempt: int, now: float
) -> str | None:
    """Write the holder record the lease ref will point at.

    Returns the blob sha, or None if ref writes are not permitted. Raises
    ``RateLimited`` when the API is merely busy.

    Written immediately before the CAS, not once per acquire, because the record
    carries the acquisition timestamp: a blob minted when the run first started
    queueing would date the lease from before it was held, and the TTL would then
    measure a hold that had not happened yet. It is only paid on attempts where
    the ref actually looks free, so a long wait does not pay for it repeatedly.
    """
    payload = {
        "run_id": run_id,
        "attempt": attempt,
        # Written by the acquirer at acquisition, which is what makes the TTL a
        # measure of lease-HOLD time rather than of run age. A run reaches this
        # job several jobs in (discovery, image build, manifest merge) and may
        # have queued for runners first, none of which is lease-hold time.
        "acquired_at": now,
    }
    response = gh_request(
        "POST",
        f"repos/{repo}/git/blobs",
        {"content": json.dumps(payload), "encoding": "utf-8"},
    )
    if (
        response.status in (200, 201)
        and isinstance(response.body, dict)
        and response.body.get("sha")
    ):
        return str(response.body["sha"])
    if _rate_limited(response):
        raise RateLimited(_retry_after(response))
    if _denied(response):
        return None
    raise SystemExit(
        f"::error::could not write the tenant lease holder record in {repo}: "
        f"HTTP {response.status} {response.message!r}"
    )


def try_acquire(repo: str, ref: str, blob_sha: str) -> str:
    """One atomic attempt. "acquired", "occupied" or "denied"; raises RateLimited.

    The 422 is not an error path bolted on — it is the lock. GitHub evaluates
    ref creation atomically, so of N simultaneous callers exactly one sees 201.
    """
    response = gh_request(
        "POST", f"repos/{repo}/git/refs", {"ref": ref, "sha": blob_sha}
    )
    if response.status in (200, 201):
        return "acquired"
    if response.status == 422 and "already exists" in response.message.lower():
        return "occupied"
    if _rate_limited(response):
        raise RateLimited(_retry_after(response))
    if _denied(response):
        return "denied"
    raise SystemExit(
        f"::error::could not take the tenant lease {ref} in {repo}: "
        f"HTTP {response.status} {response.message!r}"
    )


def read_lease_target(repo: str, ref: str) -> str | None:
    """The sha the lease ref points at, or None if unheld or unreadable.

    One API call, and the cheap half of a waiting poll. None deliberately
    conflates "nobody holds it" with "we could not tell": both mean "try the
    CAS", and the CAS is the authority.
    """
    # git/ref/<name> wants the ref without the leading "refs/".
    response = gh_request("GET", f"repos/{repo}/git/ref/{ref.removeprefix('refs/')}")
    if response.status == 404 or not isinstance(response.body, dict):
        return None
    if response.status >= 400:
        print(
            f"::warning::could not read the tenant lease {ref} (HTTP {response.status})."
        )
        return None
    target = response.body.get("object") or {}
    sha = target.get("sha") if isinstance(target, dict) else None
    return str(sha) if sha else None


def read_holder_record(repo: str, blob_sha: str) -> Holder | None:
    """Decode the holder record a lease ref points at. One API call."""
    response = gh_request("GET", f"repos/{repo}/git/blobs/{blob_sha}")
    if response.status >= 400 or not isinstance(response.body, dict):
        print(
            f"::warning::a tenant lease holder record ({blob_sha}) is unreadable "
            f"(HTTP {response.status})."
        )
        return None
    return _parse_holder(response.body)


def read_holder(repo: str, ref: str) -> Holder | None:
    """Who holds `ref`, or None if unheld/unreadable. Two API calls."""
    target = read_lease_target(repo, ref)
    if target is None:
        return None
    return read_holder_record(repo, target)


def _parse_holder(blob: dict) -> Holder | None:
    content = blob.get("content")
    if not isinstance(content, str):
        return None
    try:
        decoded = base64.b64decode(content)
        record = json.loads(decoded)
    except (ValueError, TypeError):
        print("::warning::the tenant lease holder record is not valid JSON.")
        return None
    if not isinstance(record, dict):
        return None
    try:
        run_id = int(record["run_id"])
        attempt = int(record["attempt"])
    except (KeyError, TypeError, ValueError):
        print("::warning::the tenant lease holder record names no run.")
        return None
    acquired_at = record.get("acquired_at")
    return Holder(
        run_id=run_id,
        attempt=attempt,
        acquired_at=float(acquired_at)
        if isinstance(acquired_at, (int, float))
        else None,
    )


def release_ref(repo: str, ref: str) -> bool:
    """Delete the lease ref. False ⇒ it was already gone, which is not a fault."""
    response = gh_request(
        "DELETE", f"repos/{repo}/git/refs/{ref.removeprefix('refs/')}"
    )
    return response.status in (200, 204)


def holder_is_live(
    repo: str,
    holder: Holder,
    *,
    ttl_seconds: int,
    now: float,
) -> bool:
    """Is the lease still legitimately held?

    Errs towards True. Reading a transient API failure as "dead" would break a
    live holder's lease and hand the tenant to a second installer — the race
    this module exists to close — whereas erring towards "live" only costs the
    waiter another poll interval.
    """
    response = gh_request("GET", f"repos/{repo}/actions/runs/{holder.run_id}")
    if response.status == 404:
        # The run was deleted, or never existed. Nothing will ever release this.
        return False
    if response.status >= 400 or not isinstance(response.body, dict):
        print(
            f"::warning::could not read run {holder.run_id} in {repo} "
            f"(HTTP {response.status}); treating its tenant lease as still held."
        )
        return True
    body = response.body
    if body.get("status") == _COMPLETED:
        return False
    if ttl_seconds <= 0 or holder.acquired_at is None:
        return True

    # Sanity-check the holder's own timestamp against its run before trusting it.
    # A lease cannot have been acquired before the run that took it existed, so an
    # earlier acquired_at means the record is wrong — a clock far out of step, or a
    # corrupted write. Believing it would make the hold time enormous and break a
    # LIVE holder's lease on the first poll, which is the expensive direction.
    # Erring towards "live" costs a waiter one poll interval.
    #
    # A run object with no created_at at all gets the same treatment. It should
    # never happen, so it means something has changed underneath us, and the
    # holder's run status is then the only evidence worth acting on.
    run_started = _timestamp(body.get("created_at"))
    if run_started is None:
        return True
    if holder.acquired_at < run_started:
        print(
            f"::warning::the tenant lease record for run {holder.run_id} claims an "
            "acquisition time before that run existed, so it is not trustworthy; "
            "ignoring the TTL and treating the lease as held. Its run status is "
            "still authoritative."
        )
        return True

    held_for = now - holder.acquired_at
    if held_for > ttl_seconds:
        print(
            f"::warning::run {holder.run_id} has held the tenant lease for "
            f"{int(held_for)}s, past the {ttl_seconds}s TTL, and still reports "
            f"'{body.get('status')}' — breaking it. If that run is genuinely "
            "still working, raise ttl-seconds."
        )
        return False
    return True


def _timestamp(value: object) -> float | None:
    """Parse an ISO-8601 GitHub timestamp to a POSIX float, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


_DISABLED_WARNING = (
    "::warning::this repository will not let the run write git refs, so the "
    "(app, cloud) tenant lease is disabled for this run and e2e proceeds "
    "unserialised. Grant the lease job 'contents: write' to enable it. Each e2e "
    "leg still asserts the installed version independently (FND-31), so a "
    "WRONG-version run cannot pass silently — but two runs installing the SAME "
    "version will still fight over the tenant, so this is a real loss of "
    "protection, not a no-op."
)


def acquire(
    repo: str,
    ref: str,
    run_id: int,
    attempt: int,
    *,
    wait_seconds: int,
    poll_seconds: int,
    ttl_seconds: int,
    sleep=time.sleep,
    clock=time.time,
) -> tuple[str, Holder | None]:
    """Take the lease, or report why not.

    Returns ("acquired" | "disabled" | "timeout" | "rate-limited",
    blocking_holder_or_None).

    Each waiting pass costs two API calls: read the ref, and check the holding
    run. The identity blob and the CAS are only paid when the ref looks free.
    That pre-read is an optimisation, not a decision — the CAS remains the
    authority, and losing a race after the ref looked free is just another 422.
    """
    # Attempt-counted rather than deadline-checked so the budget needs no clock
    # comparison. Ceiling division: a budget that is not an exact multiple of the
    # interval still gets its final full attempt.
    max_attempts = max(1, math.ceil(wait_seconds / poll_seconds))

    # Memoised by the ref's target sha: while a lease does not change hands, the
    # record behind it cannot change either, so re-reading it every poll would
    # spend a call per poll to learn what we already know.
    records: dict[str, Holder | None] = {}
    blocker: Holder | None = None
    rate_limited = False

    for attempt_number in range(1, max_attempts + 1):
        target = read_lease_target(repo, ref)

        if target is None:
            # Looks free — mint an identity and race for it.
            try:
                blob_sha = create_identity_blob(repo, run_id, attempt, clock())
                if blob_sha is None:
                    print(_DISABLED_WARNING)
                    return "disabled", None
                outcome = try_acquire(repo, ref, blob_sha)
            except RateLimited as limited:
                rate_limited = True
                if attempt_number < max_attempts:
                    _sleep_for_rate_limit(sleep, poll_seconds, limited.retry_after)
                continue

            if outcome == "acquired":
                print(f"Tenant lease acquired: {ref}")
                return "acquired", None
            if outcome == "denied":
                print(_DISABLED_WARNING)
                return "disabled", None
            # "occupied": somebody won the race between our read and our CAS.
            # Re-read next pass; the CAS was the authority, as intended.
            print("Lost the race for the tenant lease; it is held again.")
            if attempt_number < max_attempts:
                sleep(poll_seconds)
            continue

        rate_limited = False
        if target not in records:
            records[target] = read_holder_record(repo, target)
        blocker = records[target]

        if blocker is None:
            # Unreadable record. The CAS on the next pass is authoritative rather
            # than a guess, so just wait for it.
            print("The tenant lease is held but its holder is unreadable; retrying.")
        elif blocker.is_me(run_id, attempt):
            # A retried step, or a re-run of this same attempt. Already ours.
            print(f"Tenant lease already held by this run: {ref}")
            return "acquired", None
        elif not holder_is_live(repo, blocker, ttl_seconds=ttl_seconds, now=clock()):
            print(
                f"Reaping the tenant lease of run {blocker.run_id} "
                f"(attempt {blocker.attempt}) — that run is over."
            )
            release_ref(repo, ref)
            # Straight back to the CAS rather than sleeping: the tenant is free
            # now, and any other waiter is racing us for it on equal terms.
            continue
        else:
            print(
                f"[{attempt_number}/{max_attempts}] waiting for {ref} — held by "
                f"run {blocker.run_id} ({blocker.run_url(repo)})."
            )

        if attempt_number < max_attempts:
            sleep(poll_seconds)

    return ("rate-limited" if rate_limited else "timeout"), blocker


def _sleep_for_rate_limit(sleep, poll_seconds: int, retry_after: int | None) -> None:
    """Back off after a rate limit, honouring Retry-After within reason.

    Never returns "disabled" to the caller: a rate limit is a reason to wait, not
    evidence that the lease cannot be taken. Switching the lease off here would
    do it precisely when several legs are contending, which is both when the
    budget runs out and when the lease matters most.
    """
    delay = poll_seconds
    if retry_after is not None:
        delay = max(poll_seconds, min(retry_after, _MAX_RETRY_AFTER))
    print(
        "::warning::rate-limited by the GitHub API while taking the tenant lease; "
        f"backing off {delay}s and retrying. The lease is NOT being disabled — "
        "the per-repository budget is shared by every matrix leg, so this is "
        "expected under contention."
    )
    sleep(delay)


def release(repo: str, ref: str, run_id: int, attempt: int) -> bool:
    """Give up this run's lease, but only if it is actually ours.

    The ref name is shared by every contender for the tenant, so unlike a
    per-run name it is possible to delete somebody else's lease. Ownership is
    therefore checked first, and the ref's target is re-read immediately before
    the delete so a lease that changed hands in between is left alone.

    That narrows the window to a single DELETE round-trip; it does not close it,
    because the API offers no conditional delete. The interleaving that remains
    is worth naming rather than waving at: if the TTL breaks *our* lease while we
    are still live and another run acquires between our last read and our DELETE,
    we would delete the replacement holder's lease and put a second installer on
    the tenant. That cannot happen while the TTL exceeds the longest legitimate
    hold — which is exactly why the TTL errs long and why its sizing is pinned by
    a test. The TTL is load-bearing for release safety, not only for acquisition.
    """
    target = read_lease_target(repo, ref)
    if target is None:
        print(f"No tenant lease to release at {ref} — already reaped.")
        return False

    holder = read_holder_record(repo, target)
    if holder is None:
        print(
            f"::warning::not releasing {ref}: its holder record is unreadable, so "
            "ownership cannot be confirmed. The next contender will reap it if "
            "this run is over."
        )
        return False
    if not holder.is_me(run_id, attempt):
        print(
            f"::warning::not releasing {ref}: it is held by run {holder.run_id} "
            f"(attempt {holder.attempt}), not this run. Ours was most likely "
            "broken by the TTL, or reaped after this run was misread as finished."
        )
        return False

    # Re-read the target rather than trusting the one we validated against: if the
    # lease changed hands while we were reading the record, the sha moves, and
    # deleting would take the new holder's lease rather than ours.
    if read_lease_target(repo, ref) != target:
        print(
            f"::warning::not releasing {ref}: it changed hands while this run was "
            "confirming ownership, so the lease being held is no longer ours."
        )
        return False

    if release_ref(repo, ref):
        print(f"Tenant lease released: {ref}")
        return True
    print(f"No tenant lease to release at {ref} — already reaped.")
    return False


def write_outputs(outputs: dict[str, str], path: str | None) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acquire or release a tenant lease.")
    parser.add_argument("--mode", required=True, choices=("acquire", "release"))
    parser.add_argument("--repo", required=True, help="owner/repo the lease lives in.")
    parser.add_argument("--app", required=True, help="App under test (lease key part).")
    parser.add_argument(
        "--cloud", default="", help="Cloud (lease key part); empty = single tenant."
    )
    parser.add_argument("--run-id", required=True, type=int, help="github.run_id")
    parser.add_argument(
        "--run-attempt", required=True, type=int, help="github.run_attempt"
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=5400,
        help="How long to keep trying for the tenant before giving up (default 90min).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="Gap between attempts. A waiting pass costs two API calls (read the "
        "lease ref, check the holding run), and four on the pass that takes it. "
        "GITHUB_TOKEN allows 1000/hour per REPOSITORY and every matrix leg shares "
        "it, so raise this rather than lower it when many clouds contend.",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=14400,
        help="How long a holder may HOLD the lease (measured from the acquisition "
        "time it recorded, not from run age) before a waiter treats it as wedged "
        "and breaks it. Must exceed the longest legitimate hold — the install plus "
        "the leg ceiling — because breaking a live holder's lease puts a second "
        "installer on the tenant, and because release safety depends on it too. "
        "Default 4h against a 40min+120min hold. 0 disables the backstop.",
    )
    parser.add_argument(
        "--on-timeout",
        choices=("fail", "warn"),
        default="fail",
        help="fail (default): the job goes red naming the holder. warn: proceed "
        "unserialised.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Bounds-checked up front rather than left to fail at the point of use.
    # poll_seconds=0 is a ZeroDivisionError in the attempt-count arithmetic and a
    # negative one reaches sleep() as a ValueError — both loud rather than
    # silently wrong, but both arrive as a traceback several steps from the input
    # that caused them.
    if args.poll_seconds < 1:
        raise SystemExit(
            f"::error::--poll-seconds must be at least 1, got {args.poll_seconds}"
        )
    if args.wait_seconds < 0:
        raise SystemExit(
            f"::error::--wait-seconds cannot be negative, got {args.wait_seconds}"
        )
    if args.ttl_seconds < 0:
        raise SystemExit(
            f"::error::--ttl-seconds cannot be negative, got {args.ttl_seconds} "
            "(0 disables the TTL backstop)"
        )

    ref = lease_ref(args.app, args.cloud)

    if args.mode == "release":
        release(args.repo, ref, args.run_id, args.run_attempt)
        return 0

    state, blocker = acquire(
        args.repo,
        ref,
        args.run_id,
        args.run_attempt,
        wait_seconds=args.wait_seconds,
        poll_seconds=args.poll_seconds,
        ttl_seconds=args.ttl_seconds,
    )
    write_outputs(
        {
            "acquired": "true" if state == "acquired" else "false",
            "state": state,
            "lease-ref": ref,
            "holder-run-id": str(blocker.run_id) if blocker else "",
        },
        os.environ.get("GITHUB_OUTPUT"),
    )

    if state in ("acquired", "disabled"):
        return 0

    if state == "rate-limited":
        # Deliberately NOT fail-open: proceeding unserialised because the API was
        # busy is how two runs end up on one tenant.
        print(
            f"::error::could not get the tenant lease {ref} within "
            f"{args.wait_seconds}s because the GitHub API rate-limited this "
            "repository throughout. Nothing is wrong with this change. The "
            "GITHUB_TOKEN budget is 1000 requests/hour per repository and is "
            "shared by every matrix leg — re-run when the hour rolls over, or "
            "raise poll-seconds so a waiting leg costs fewer calls."
        )
        return 1

    held_by = (
        f" It is held by run {blocker.run_id} ({blocker.run_url(args.repo)})."
        if blocker
        else ""
    )
    if args.on_timeout == "warn":
        print(
            f"::warning::gave up waiting for {ref} after {args.wait_seconds}s and "
            f"is proceeding unserialised.{held_by}"
        )
        return 0
    print(
        f"::error::could not get the tenant lease {ref} within "
        f"{args.wait_seconds}s.{held_by} Nothing is wrong with this change — the "
        "tenant is busy. Re-run this job once that run finishes, or raise "
        "wait-seconds if queueing this deep is expected."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
