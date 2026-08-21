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

One run, several tenants: ordered acquisition
--------------------------------------------
A cross-CSP run needs one tenant per cloud, and it needs all of them for its
whole life. Taking them in PARALLEL and holding each while blocking on the rest
is hold-and-wait, and it deadlocked in production the first time two runs queued
behind one holder (FND-646): the holder released all three leases, the two
waiters raced for the freed set and *split* it, and each then blocked on what the
other held for the full wait budget. With ≥2 runs queued behind a holder that
split is the expected outcome, not a rare interleaving.

So a run that wants several tenants takes them one at a time, in a fixed order
derived only from the cloud names (``acquire_ordered``, ``--clouds``). Resource
ordering makes a cycle impossible, so the worst case degrades from mutual
blocking to plain serialisation, and nothing is ever cancelled.

Note what this is NOT: it is not the ordered ticket queue rejected above. That
attempt replaced the CAS with an ordering and got fairness instead of exclusion.
The CAS below is untouched and remains the only thing that grants a lease; the
ordering governs only the sequence in which ONE run takes several of them.

API budget
----------
``GITHUB_TOKEN`` allows 1000 requests/hour **per repository**, and every matrix
leg of a run shares that one budget. Ordered acquisition helps here too: polling
one lease at a time rather than three concurrently divides a waiting run's
request rate by the number of clouds. A waiting poll therefore has to be cheap or
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
* **No permission to write refs** fails the acquire, naming the grant that is
  missing.

  This used to be a ``::warning::`` that returned ``disabled`` and exited 0, on
  the reasoning that "making an ungrantable lease fail the run would turn a
  safety improvement into a new way for e2e to go red fleet-wide". That
  fail-open never existed (FND-702). ``prepare-tenant``'s first step runs this
  same driver in ``--mode verify``, which needs only ``contents: read``, finds
  no lease ref, and exits 1 under ``set -euo pipefail`` — so every
  ``Prepare tenant`` leg failed, the e2e legs were skipped, and the gate went
  red anyway. The choice was not being made; it was being *reversed* two jobs
  later, at the cost of an error that said "re-run this job" when re-running
  could not possibly help.

  So the posture is now the one the code always had, said where it can be
  explained: a lease that cannot be taken at all is a configuration error in the
  caller's ``permissions:`` block, and it is reported at the acquire, by name.

  Proceeding unserialised was the alternative and was rejected on its merits,
  not merely because it was more work. The FND-31 version assert catches a
  *wrong-version* run, not two runs installing the *same* version and fighting
  over one tenant — two dispatches of one commit do exactly that, and it
  surfaces as a worker/task-queue failure rather than a version mismatch. That
  is also why a rate limit must never be allowed to reach this path.
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

# A waiting lease is the longest-running step in the whole e2e run, and its only
# outward sign of life is the per-attempt "waiting for ..." line. Python
# block-buffers stdout when it is a pipe, which an Actions log always is, so
# without this NOTHING appears until the process exits: a run that queued for ten
# minutes showed a blank step for ten minutes and then printed all ten minutes of
# progress at once. That is indistinguishable from a wedged step to anyone
# watching, and it is why the queue got read as a deadlock (FND-696).
#
# hasattr rather than a bare call: this runs at import, and a stdout that some
# other harness has replaced with a plain writable object would otherwise make
# the whole lease unimportable — trading an invisible wait for an unserialised
# tenant, which is the failure this module exists to prevent.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

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

# Transport-level retries (curl could not complete the request, or GitHub answered
# 5xx). Deliberately few and short: the acquire loop already retries at a much
# coarser grain, so this only has to absorb a blip, not an outage.
_TRANSPORT_ATTEMPTS = 3
_TRANSPORT_BACKOFF_SECONDS = 2

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


def gh_request(
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    sleep=time.sleep,
) -> Response:
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
        # Read the Authorization header from stdin (-K -) rather than -H so the
        # token never appears in curl's argv, where anything on the same runner
        # could read it from /proc/<pid>/cmdline while the request runs. Only
        # the credential goes through the pipe; the non-secret headers stay in
        # argv, so the tests stubbing this seam still see the method and URL.
        #
        # Re-supplied on every retry below, because stdin is consumed per
        # process: a retry that reused the argv without the config would send an
        # unauthenticated request and read the 401 as a permission denial.
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
            # curl could not complete the request at all: DNS, connect, timeout,
            # TLS. Seen in production as `curl: (60) SSL certificate problem` on a
            # single runner, which failed the lease job outright and — through the
            # matrix aggregate — skipped the install on every OTHER cloud whose
            # lease had been taken successfully. One blip must not cost a run its
            # tenants, so these are retried.
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
            # 5xx is GitHub having a moment, not an answer. Retried for the same
            # reason: the callers treat an unreadable response conservatively
            # (assume the lease is held), which is safe but wastes a poll.
            if response.status < 500 or last:
                return response
            print(
                f"::warning::{label} returned HTTP {response.status} "
                f"(attempt {transport_attempt}/{_TRANSPORT_ATTEMPTS}) — retrying."
            )

        sleep(_TRANSPORT_BACKOFF_SECONDS * 2 ** (transport_attempt - 1))

    # Unreachable: the final attempt either returns or raises above.
    raise SystemExit(f"::error::exhausted transport attempts for {label}")


def _rate_limited(response: Response) -> bool:
    """Is this a rate limit rather than a permission problem?

    Both arrive as 403, and conflating them is how the lease used to switch
    itself OFF exactly when it was needed: several legs queueing for tenants is
    also when the shared per-repository budget runs out, and the (then
    nominally fail-open) denial path let every waiting run give up on the lease.
    The denial path fails the job now (FND-702), so the same conflation would
    turn a busy hour into a red run blaming a permission the caller has.

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
    lease abandon itself under contention.
    """

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


def create_identity_blob(
    repo: str, run_id: int, attempt: int, now: float
) -> str | None:
    """Write the holder record the lease ref will point at.

    Returns the blob sha, or None if writes are not permitted — which the caller
    turns into a red job naming the missing grant, not into a lease-less run.
    Raises ``RateLimited`` when the API is merely busy.

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


# Printed at the ONE place that can explain the failure. Reaching the install
# without a lease is not survivable — `prepare-tenant` verifies its own tenant's
# lease before installing and fails when there is none — so the choice here is
# not "red or green", it is "red here with the fix, or red two jobs later with
# 'Re-run this job'" (FND-702).
_DENIED_ERROR = (
    "::error::this run may not write git refs, so the (app, cloud) tenant lease "
    "cannot be taken. This is a permissions problem in the CALLING workflow, not "
    "a problem with the change under test, and re-running will not fix it: give "
    "the job that calls tests-reusable.yaml `contents: write` and `actions: "
    "read`. A `permissions:` block is exhaustive, not additive — every scope it "
    "omits becomes `none` — so a caller that declares one must declare the whole "
    "set the reusable's jobs use (see the block in the scaffolded tests.yaml). A "
    "caller with NO block at all gets this only where the repository default is "
    "read-only. Proceeding without the lease is not the fallback: two runs "
    "installing the SAME version onto one tenant surface as a worker/task-queue "
    "failure, which the FND-31 per-leg version assert does not catch."
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

    Returns ("acquired" | "denied" | "timeout" | "rate-limited",
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
                    print(_DENIED_ERROR)
                    return "denied", None
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
                print(_DENIED_ERROR)
                return "denied", None
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


@dataclass(frozen=True)
class OrderedOutcome:
    """The result of an ordered multi-cloud acquisition.

    ``ref`` is the one the outcome hinged on — the last lease taken when the
    whole set was acquired, the one that blocked otherwise — so a caller has one
    specific tenant to name in an error instead of guessing which of the set it
    was.
    """

    state: str
    blocker: Holder | None
    held: tuple[str, ...]
    ref: str


def acquire_ordered(
    repo: str,
    app: str,
    clouds: list[str],
    run_id: int,
    attempt: int,
    *,
    wait_seconds: int,
    poll_seconds: int,
    ttl_seconds: int,
    sleep=time.sleep,
    clock=time.time,
) -> OrderedOutcome:
    """Take EVERY cloud's lease, in a fixed order, under one total budget.

    ``state`` is "acquired" | "denied" | "timeout" | "rate-limited", and
    "acquired" is all-or-nothing: any other state hands back whatever was taken
    on the way, so the caller either holds the whole set or nothing.

    Why ordered, and why one job
    ----------------------------
    ``lease-tenant`` used to be a per-cloud matrix that acquired each cloud's
    lease in PARALLEL and held it for the whole run, with the install gated on
    the matrix aggregate. That is textbook hold-and-wait: a run that wins a
    subset sits on those tenants while blocking on the rest. It deadlocked in
    production the moment two runs queued behind one holder (FND-646) — the
    holder released all three leases, the two waiters raced for the freed set
    and *split* it (one took aws + azure, the other gcp), and each then blocked
    on what the other held for the full wait budget.

    That split is the EXPECTED outcome of a parallel matrix whenever ≥2 runs are
    queued behind a holder, not a rare interleaving. Acquiring in a fixed global
    order makes it structurally impossible: two runs can never hold locks the
    other needs out of order, so the worst case degrades from mutual blocking to
    plain serialisation. No run is ever cancelled — a queued run waits and then
    proceeds.

    The order is ``sorted()`` over the cloud names, and it MUST stay a pure
    function of the names. Deriving it from anything per-run (run id, arrival
    order, the caller's list order) is what breaks the guarantee, because two
    contenders would then disagree about which lock comes first.

    It sorts the ``slug()`` of each name — the same canonical form ``lease_ref``
    keys the lease on — rather than the raw token, because the order has to be a
    pure function of the *lock*, not of its spelling. Sorting raw tokens would let
    ``"aws, gcp"`` and ``"aws,gcp"`` (or a difference in case) take the very same
    pair of locks in opposite orders, which is exactly the disagreement that
    reopens the FND-646 cycle. Canonicalizing first also makes the dedup real:
    two spellings of one cloud collapse to the one lease they name.

    Every contender
    also computes it over its OWN resolved cloud list, which may be a subset —
    that is fine and is why sorting works: any two runs still take their common
    subset in the same relative order.

    This is NOT the ordered ticket queue the module docstring rejects. That
    attempt replaced the atomic CAS with a run_id ordering and got fairness
    instead of exclusion. Here the CAS is untouched and still the only thing that
    grants a lease; the ordering governs only the sequence in which ONE run takes
    several of them.

    ``wait_seconds`` is a TOTAL budget across the set, not per cloud, so the job
    timeout still bounds the whole thing. It also cuts the API cost: polling one
    lease at a time instead of three concurrently divides the per-run request
    rate by the number of clouds, and that rate is the binding constraint (see
    the module docstring's API budget section).
    """
    order = sorted({slug(cloud) for cloud in clouds})
    held: list[str] = []
    deadline = clock() + wait_seconds
    ref = lease_ref(app, order[0])

    for cloud in order:
        ref = lease_ref(app, cloud)
        remaining = int(deadline - clock())
        if remaining <= 0:
            print(
                f"::error::the {wait_seconds}s tenant-lease budget ran out before "
                f"{ref} could be taken."
            )
            release_all(repo, held, run_id, attempt)
            return OrderedOutcome("timeout", None, (), ref)

        state, blocker = acquire(
            repo,
            ref,
            run_id,
            attempt,
            wait_seconds=remaining,
            poll_seconds=poll_seconds,
            ttl_seconds=ttl_seconds,
            sleep=sleep,
            clock=clock,
        )
        if state == "acquired":
            held.append(ref)
            continue

        # Hand back everything taken on the way in. release-tenant would do it
        # too, but only after this job's own failure has propagated — and holding
        # tenants across that gap is the hold-and-wait this function exists to
        # remove.
        if held:
            print(
                f"Giving back {len(held)} lease(s) already held, because the "
                f"whole set could not be taken: {', '.join(held)}"
            )
        release_all(repo, held, run_id, attempt)
        return OrderedOutcome(state, blocker, (), ref)

    print(f"All {len(held)} tenant lease(s) acquired in order: {', '.join(held)}")
    return OrderedOutcome("acquired", None, tuple(held), ref)


def release_all(repo: str, refs: list[str], run_id: int, attempt: int) -> None:
    """Release several leases, ownership-checked one at a time.

    Reverse order, so the lock taken last is given back first. It makes no
    difference to correctness — releases cannot block — but it keeps the log
    reading as the inverse of the acquisition.

    Every ref is attempted even if one release exhausts its transport retries and
    raises: this is the unwind path for a partial hold, so abandoning the rest at
    the first failure would leave earlier leases held until their TTL expires —
    the hold-and-wait ``acquire_ordered`` exists to remove. The first failure is
    re-raised once the whole set has been attempted, so the job still goes red.
    """
    failure: SystemExit | None = None
    for ref in reversed(refs):
        try:
            release(repo, ref, run_id, attempt)
        except SystemExit as exc:
            print(f"::warning::could not release {ref}; continuing with the rest.")
            failure = failure or exc
    if failure is not None:
        raise failure


def _sleep_for_rate_limit(sleep, poll_seconds: int, retry_after: int | None) -> None:
    """Back off after a rate limit, honouring Retry-After within reason.

    Never returns "denied" to the caller: a rate limit is a reason to wait, not
    evidence that the caller lacks the grant. Reporting it as a denial would do
    so precisely when several legs are contending — which is both when the budget
    runs out and when the lease matters most — and would send whoever read the
    log to fix a `permissions:` block that was already correct.
    """
    delay = poll_seconds
    if retry_after is not None:
        delay = max(poll_seconds, min(retry_after, _MAX_RETRY_AFTER))
    print(
        "::warning::rate-limited by the GitHub API while taking the tenant lease; "
        f"backing off {delay}s and retrying. This is NOT a permission problem — "
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


def verify_held(repo: str, ref: str, run_id: int, attempt: int) -> bool:
    """Does this run hold `ref` right now?

    Exists because a matrix job cannot depend on its own leg of an upstream matrix
    job: ``needs.lease-tenant.result`` is the AGGREGATE across clouds, so a gate
    on it says "some cloud's lease succeeded", never "mine did". Observed live —
    one cloud's acquire failed on a transient TLS error and the aggregate then
    skipped the install for the two clouds whose leases had been taken
    successfully.

    So the install gates on the lease job having RUN, and each leg confirms its
    own tenant here. Two API calls to make the install's precondition true per
    cloud instead of approximately true across clouds.
    """
    holder = read_holder(repo, ref)
    if holder is None:
        print(
            f"::error::this run does not hold the tenant lease {ref} — it is "
            "unheld or its holder record is unreadable, so installing now could "
            "race another run. Read the 'Tenant lease' job's log before "
            "re-running: if the acquire itself failed, its error says why and "
            "re-running this job cannot help."
        )
        return False
    if not holder.is_me(run_id, attempt):
        print(
            f"::error::this run does not hold the tenant lease {ref}: it is held "
            f"by run {holder.run_id} ({holder.run_url(repo)}), attempt "
            f"{holder.attempt}. Installing now would race that run. Acquisition "
            "is all-or-nothing across the cloud set, so this normally means the "
            "lease was broken by its TTL, or reaped after this run was misread "
            "as finished. Re-run this job."
        )
        return False
    print(f"Tenant lease confirmed held by this run: {ref}")
    return True


def write_outputs(outputs: dict[str, str], path: str | None) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acquire or release a tenant lease.")
    parser.add_argument(
        "--mode", required=True, choices=("acquire", "verify", "release")
    )
    parser.add_argument("--repo", required=True, help="owner/repo the lease lives in.")
    parser.add_argument("--app", required=True, help="App under test (lease key part).")
    parser.add_argument(
        "--cloud", default="", help="Cloud (lease key part); empty = single tenant."
    )
    parser.add_argument(
        "--clouds",
        default="",
        help="Comma-separated clouds to lease as ONE ordered set (acquire and "
        "release only). Empty falls back to --cloud, so a caller that always "
        "passes both needs no conditional shell and the single-tenant path — "
        "where the resolved cloud list is legitimately empty — keeps working. "
        "Acquisition is sorted by cloud name and --wait-seconds becomes a TOTAL "
        "budget across the set; see acquire_ordered for why the order must be a "
        "pure function of the names.",
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

    # Canonicalize to the same form `lease_ref` keys the lease on, so the
    # acquisition order is a function of the lock rather than of its spelling:
    # "aws, gcp" and "aws,gcp" name one pair of locks and must take them in one
    # order. Blank tokens are dropped on the raw text first, because `slug("")`
    # is the single-tenant "default" rather than nothing.
    clouds = [slug(cloud) for cloud in args.clouds.split(",") if cloud.strip()]
    if clouds and args.cloud.strip():
        # Silently preferring one would make the OTHER one's tenant unserialised
        # while the job still went green.
        raise SystemExit(
            f"::error::--cloud {args.cloud!r} and --clouds {args.clouds!r} were "
            "both given; pass one. --clouds leases an ordered set, --cloud leases "
            "one tenant, and an empty --clouds falls back to --cloud."
        )

    ref = lease_ref(args.app, args.cloud)

    if args.mode == "release":
        # Ordered acquisition means a single job may hold several leases, so the
        # release side has to be able to give back the same set. Ownership is
        # checked per ref, so a set this run only partly holds is safe.
        release_all(
            args.repo,
            [lease_ref(args.app, cloud) for cloud in sorted(clouds)] or [ref],
            args.run_id,
            args.run_attempt,
        )
        return 0

    if args.mode == "verify":
        # Deliberately single-cloud only. Verify exists because a matrix job
        # cannot depend on its own leg of an upstream matrix, so each install leg
        # confirms ITS OWN tenant; a --clouds form here would invite the install
        # to check the aggregate again, which is the exact approximation this mode
        # was added to replace.
        if clouds:
            raise SystemExit(
                "::error::--mode verify takes --cloud, not --clouds: each install "
                "leg must confirm its own tenant rather than the set."
            )
        held = verify_held(args.repo, ref, args.run_id, args.run_attempt)
        return 0 if held else 1

    if clouds:
        outcome = acquire_ordered(
            args.repo,
            args.app,
            clouds,
            args.run_id,
            args.run_attempt,
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
            ttl_seconds=args.ttl_seconds,
        )
        state, blocker, held_refs = outcome.state, outcome.blocker, list(outcome.held)
        # The ref the outcome hinged on, so the errors below name one specific
        # tenant. `lease-refs` carries the whole set.
        ref = outcome.ref
    else:
        held_refs = []
        state, blocker = acquire(
            args.repo,
            ref,
            args.run_id,
            args.run_attempt,
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
            ttl_seconds=args.ttl_seconds,
        )
        if state == "acquired":
            held_refs = [ref]
    write_outputs(
        {
            "acquired": "true" if state == "acquired" else "false",
            "state": state,
            "lease-ref": ref,
            "lease-refs": ",".join(held_refs),
            "clouds": ",".join(sorted(clouds)) if clouds else args.cloud,
            "holder-run-id": str(blocker.run_id) if blocker else "",
        },
        os.environ.get("GITHUB_OUTPUT"),
    )

    if state == "acquired":
        return 0

    if state == "denied":
        # The message was printed at the point of denial, where the specific call
        # that was refused is still in hand. Exiting non-zero here is the whole of
        # FND-702: the run cannot install without a lease either way, so this
        # moves the red to the one job that can say what to grant.
        return 1

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
