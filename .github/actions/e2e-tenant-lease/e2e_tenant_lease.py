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
tenant — the exact race this module exists to close. So it errs long.

Failure posture
---------------
* **No permission to write refs** is a ``::warning::`` and the run proceeds
  *without* a lease. The lease reduces contention; it is not what keeps a
  wrong-version run from passing — every leg re-asserts the installed version
  itself (FND-31). Making an ungrantable lease fail the run would turn a safety
  improvement into a new way for e2e to go red fleet-wide.
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


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the HTTP client."""
    return subprocess.run(cmd, **kwargs)


def _parse_http(raw: str, label: str) -> tuple[int, object | None]:
    """Split a ``curl -i`` response into (status_code, parsed_body_or_None)."""
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
    if not body.strip():
        return status_code, None
    try:
        return status_code, json.loads(body)
    except json.JSONDecodeError:
        # A non-JSON body (an HTML error page from a proxy) must not crash the
        # caller before it can report the status code, which is the part that
        # decides what happens next.
        return status_code, None


def gh_request(
    method: str, path: str, payload: dict | None = None
) -> tuple[int, object | None]:
    """Call the GitHub API, returning the status code rather than raising on 4xx.

    curl, not ``gh api``, for the same reason ``poll_check_runs_gate.py`` uses it:
    ``gh api`` treats every non-2xx as a command failure and prints its own
    diagnostic instead of the response. Here the non-2xx codes are the entire
    mechanism — 422 on ref creation IS the lock being held, and it has to be
    distinguished from 403 ("this repo will not let us write refs") and from a
    genuine error.
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


def _message(body: object | None) -> str:
    return body.get("message", "") if isinstance(body, dict) else ""


def _denied(status: int) -> bool:
    """GitHub 404s resources it will not admit exist, so it is a permission
    answer as much as 401/403 are."""
    return status in (401, 403, 404)


def create_identity_blob(
    repo: str, run_id: int, attempt: int, now: float
) -> str | None:
    """Write the holder record the lease ref will point at. None ⇒ denied.

    Created BEFORE the lease is taken, so it costs one wasted blob per failed
    attempt. That is the right way round: an unreferenced blob is garbage GitHub
    collects, whereas taking the lease first and describing it second would leave
    a window where the lease is held by nobody identifiable.
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
    status, body = gh_request(
        "POST",
        f"repos/{repo}/git/blobs",
        {"content": json.dumps(payload), "encoding": "utf-8"},
    )
    if status in (200, 201) and isinstance(body, dict) and body.get("sha"):
        return str(body["sha"])
    if _denied(status):
        return None
    raise SystemExit(
        f"::error::could not write the tenant lease holder record in {repo}: "
        f"HTTP {status} {_message(body)!r}"
    )


def try_acquire(repo: str, ref: str, blob_sha: str) -> str:
    """One atomic attempt. Returns "acquired", "occupied" or "denied".

    The 422 is not an error path bolted on — it is the lock. GitHub evaluates
    ref creation atomically, so of N simultaneous callers exactly one sees 201.
    """
    status, body = gh_request(
        "POST", f"repos/{repo}/git/refs", {"ref": ref, "sha": blob_sha}
    )
    if status in (200, 201):
        return "acquired"
    if status == 422 and "already exists" in _message(body).lower():
        return "occupied"
    if _denied(status):
        return "denied"
    raise SystemExit(
        f"::error::could not take the tenant lease {ref} in {repo}: "
        f"HTTP {status} {_message(body)!r}"
    )


def read_holder(repo: str, ref: str) -> Holder | None:
    """Who holds `ref`, or None if it is unheld or unreadable.

    None deliberately conflates "nobody holds it" with "we could not tell": both
    mean "retry the CAS", and the CAS is authoritative. Guessing at an
    unreadable holder is what would be dangerous.
    """
    # git/ref/<name> wants the ref without the leading "refs/".
    status, body = gh_request(
        "GET", f"repos/{repo}/git/ref/{ref.removeprefix('refs/')}"
    )
    if status == 404 or not isinstance(body, dict):
        return None
    if status >= 400:
        print(f"::warning::could not read the tenant lease {ref} (HTTP {status}).")
        return None

    target = body.get("object") or {}
    blob_sha = target.get("sha") if isinstance(target, dict) else None
    if not blob_sha:
        return None

    status, blob = gh_request("GET", f"repos/{repo}/git/blobs/{blob_sha}")
    if status >= 400 or not isinstance(blob, dict):
        print(
            f"::warning::the tenant lease {ref} exists but its holder record "
            f"({blob_sha}) is unreadable (HTTP {status})."
        )
        return None
    return _parse_holder(blob)


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
    status, _body = gh_request(
        "DELETE", f"repos/{repo}/git/refs/{ref.removeprefix('refs/')}"
    )
    return status in (200, 204)


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
    status, body = gh_request("GET", f"repos/{repo}/actions/runs/{holder.run_id}")
    if status == 404:
        # The run was deleted, or never existed. Nothing will ever release this.
        return False
    if status >= 400 or not isinstance(body, dict):
        print(
            f"::warning::could not read run {holder.run_id} in {repo} "
            f"(HTTP {status}); treating its tenant lease as still held."
        )
        return True
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

    Returns ("acquired" | "disabled" | "timeout", blocking_holder_or_None).
    """
    # Attempt-counted rather than deadline-checked so the budget needs no clock
    # comparison. Ceiling division: a budget that is not an exact multiple of the
    # interval still gets its final full attempt.
    max_attempts = max(1, math.ceil(wait_seconds / poll_seconds))

    blocker: Holder | None = None
    for attempt_number in range(1, max_attempts + 1):
        blob_sha = create_identity_blob(repo, run_id, attempt, clock())
        if blob_sha is None:
            print(
                "::warning::this repository will not let the run write git refs, "
                "so the (app, cloud) tenant lease is disabled for this run and "
                "e2e proceeds unserialised. Grant the lease job 'contents: write' "
                "to enable it. Each e2e leg still asserts the installed version "
                "independently (FND-31), so a wrong-version run cannot pass "
                "silently — it just fails less informatively."
            )
            return "disabled", None

        outcome = try_acquire(repo, ref, blob_sha)
        if outcome == "denied":
            print(
                "::warning::this repository will not let the run create the tenant "
                "lease ref; proceeding unserialised. See the note above."
            )
            return "disabled", None
        if outcome == "acquired":
            print(f"Tenant lease acquired: {ref}")
            return "acquired", None

        # Occupied. Find out by whom, and whether that claim is still good.
        blocker = read_holder(repo, ref)
        if blocker is None:
            # Either it was released between the CAS and the read, or we could
            # not read it. Either way the CAS on the next pass is authoritative.
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

    return "timeout", blocker


def release(repo: str, ref: str, run_id: int, attempt: int) -> bool:
    """Give up this run's lease, but only if it is actually ours.

    The ref name is shared by every contender for the tenant, so unlike a
    per-run name it is possible to delete somebody else's lease. Ownership is
    therefore checked first.

    The check-then-delete is not atomic (the API offers no conditional delete),
    but the window is closed by the liveness rule rather than by luck: this runs
    while our own run is still ``in_progress``, so no contender is entitled to
    reap us, and nobody else can be holding the lease for us to delete by
    mistake. The TTL could in principle break our lease while we are live, which
    is one more reason it errs long.
    """
    holder = read_holder(repo, ref)
    if holder is None:
        print(f"No tenant lease to release at {ref} — already reaped.")
        return False
    if not holder.is_me(run_id, attempt):
        print(
            f"::warning::not releasing {ref}: it is held by run {holder.run_id} "
            f"(attempt {holder.attempt}), not this run. Ours was most likely "
            "broken by the TTL, or reaped after this run was misread as finished."
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
        help="Gap between attempts. Each contended attempt costs a handful of API "
        "calls, so this also sets the draw on the 1000/hour GITHUB_TOKEN budget.",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=14400,
        help="How long a holder may HOLD the lease (measured from the acquisition "
        "time it recorded, not from run age) before a waiter treats it as wedged "
        "and breaks it. Must exceed the longest legitimate hold — the install plus "
        "the leg ceiling — because breaking a live holder's lease puts a second "
        "installer on the tenant. Default 4h against a 40min+120min hold. 0 "
        "disables the backstop.",
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

    if state != "timeout":
        return 0

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
