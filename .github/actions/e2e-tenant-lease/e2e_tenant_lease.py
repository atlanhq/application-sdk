#!/usr/bin/env python3
"""A real ``(app, cloud)`` tenant lease for e2e runs — a queue, not a one-deep
waiting room.

Why this exists rather than a ``concurrency:`` group
---------------------------------------------------
A tenant is a shared *mutable* resource: ``prepare-tenant`` installs a version
onto it and the e2e legs then assert they are testing that version (FND-31). So
access has to be serialised across the whole span of a run, from the install to
the last leg. GitHub's ``concurrency:`` cannot express that, for two independent
reasons:

* It is per-job, not per-run. A group on ``prepare-tenant`` is released the
  moment that job ends, which is before any leg has started — the race it looks
  like it closes is still wide open. The existing comment on that job said as
  much: *"GitHub has no cross-job lease, so another run can still install
  between this job and the legs below"*.
* It holds at most ONE pending run per group. A third arrival does not queue —
  it cancels the run that was waiting, before that run is ever given a runner,
  producing no log output at all. That is FND-218: what looked like connector
  test failures across a merge-queue batch were evictions, and the callback
  mirrored them onto the dispatching PR as ``failure``, which reads as "your
  change broke the connector".

``cancel-in-progress: false`` is therefore a waiting room with one chair, not a
queue. This module is the queue.

Protocol
--------
Every run that wants a tenant creates a *ticket* ref in its own repository::

    refs/e2e-tenant-lease/<app>/<cloud>/<run_id>-<run_attempt>

The lease is held by whichever LIVE ticket sorts lowest by
``(run_id, run_attempt)``. Nothing is negotiated and there is no lock object to
leak: every participant derives the same holder from the same ref listing, so
there is no compare-and-swap, and no step whose failure strands the resource.
Two properties are what make that sound:

* ``run_id`` is assigned by GitHub and increases with run creation time within a
  repository, so ordering is *server-side arrival order*. No runner clock takes
  part, which removes the window where two runs both believe they are first
  because their wall clocks disagree. (Ordering by a timestamp written into the
  ref name would have exactly that bug, and it would be invisible until it lost
  a tenant.)
* A ticket's name is derived entirely from the run, so it is identical in every
  job of that run. The acquiring job and the releasing job compute the same name
  without passing state between them, and re-creating a ticket is idempotent —
  it restores the run's exact queue position instead of sending it to the back.

Liveness comes from the holder's *run*, not from a heartbeat: a ticket whose run
reports ``status: completed`` is dead, and any participant that notices deletes
it. This is deliberately not "release in an ``if: always()`` job", because that
does not cover the case that matters most — a cancelled run whose release job
never starts at all, since GitHub cancels queued jobs rather than running them.
The reaper makes a leaked ticket self-healing: the next contender clears it on
its first poll, so the worst case is one wasted poll interval, not a wedged
tenant. A hard TTL on run *age* is kept as a second backstop for a run the
Actions API will not report on.

Failure posture
---------------
* **No permission to write refs** (a repository or caller that does not grant
  ``contents: write``) is a ``::warning::`` and the run proceeds *without* a
  lease. The lease reduces contention; it is not the thing that keeps a
  wrong-version run from passing — every e2e leg re-asserts the installed
  version itself (FND-31). Making an ungrantable lease fail the run would turn a
  safety improvement into a new way for e2e to go red fleet-wide.
* **Not acquired within the wait budget** fails loudly by default, naming the
  holding run. That is strictly better than the behaviour it replaces (silent
  eviction with no log), and unlike fail-open it does not put two runs on one
  tenant expecting different versions.

Co-located with the composite action, and pinned ``@main`` by every consumer on
purpose: all contenders must agree on the ref layout and the ordering rule, so
the protocol must not vary by which ref a caller happens to have checked out.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# Ticket refs live outside refs/heads and refs/tags on purpose: a branch would
# fire `push` events (and show up in the branch list), a tag would pollute the
# release surface. A custom namespace is inert — nothing watches it.
REF_NAMESPACE = "e2e-tenant-lease"

# The only GitHub run status that means "over". Everything else — queued,
# in_progress, requested, waiting, pending — is treated as live, so an
# unfamiliar future status errs towards leaving a peer's lease alone.
_COMPLETED = "completed"

# Characters safe in a single git ref path component. Deliberately narrower than
# git's own rules: app names and cloud names come from workflow inputs, and a
# ref name is the one place where "mostly valid" turns into a 422 nobody
# expected.
_SAFE_SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")


def slug(value: str, *, default: str = "default") -> str:
    """Reduce a free-text key to one safe git ref path component.

    ``cloud`` is legitimately empty on the single-tenant path (the fallback
    matrix leg spells it as a defined-but-empty string), so an empty result maps
    to ``default`` rather than producing ``refs/<ns>/<app>//<ticket>`` — an
    empty component git rejects.
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


def lease_prefix(app: str, cloud: str) -> str:
    """The ref prefix every contender for one tenant shares (no ``refs/``)."""
    return f"{REF_NAMESPACE}/{slug(app, default='app')}/{slug(cloud)}"


@dataclass(frozen=True, order=True)
class Ticket:
    """One run's claim on a tenant.

    Ordered by ``(run_id, attempt)`` — declared in that field order so the
    dataclass's generated comparison *is* the queue order, leaving no second
    sort key to drift from it. A re-run keeps its original ``run_id`` and so
    keeps its place in line; the two attempts can never be live at once, since a
    re-run only starts after the previous attempt has finished.
    """

    run_id: int
    attempt: int

    @property
    def name(self) -> str:
        return f"{self.run_id}-{self.attempt}"

    def ref(self, prefix: str) -> str:
        return f"refs/{prefix}/{self.name}"

    def run_url(self, repo: str) -> str:
        return f"https://github.com/{repo}/actions/runs/{self.run_id}"


def parse_ticket(ref: str) -> Ticket | None:
    """Parse a ticket out of a full ref name, or None if it is not one.

    Unrecognised refs under the namespace are *ignored* rather than reaped: a
    ref this version does not understand is more likely to be a newer protocol
    than garbage, and ignoring it only costs the lease's mutual exclusion with
    whoever wrote it — deleting it would break them outright.
    """
    name = ref.rsplit("/", 1)[-1]
    run_id, _, attempt = name.partition("-")
    if not run_id.isdigit() or not attempt.isdigit():
        return None
    return Ticket(int(run_id), int(attempt))


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

    curl, not ``gh api``, for the same reason ``poll_check_runs_gate.py`` uses
    it: ``gh api`` treats every non-2xx as a command failure and prints its own
    diagnostic instead of the response, and here the non-2xx codes are load
    bearing. 422 means "someone else already holds this ticket" and 403/404 mean
    "this repository will not let us write refs" — two completely different
    outcomes that both have to be distinguished from a genuine error.
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


def create_ticket(repo: str, prefix: str, ticket: Ticket, sha: str) -> str:
    """Create this run's ticket. Returns "created", "exists" or "denied"."""
    status, body = gh_request(
        "POST",
        f"repos/{repo}/git/refs",
        {"ref": ticket.ref(prefix), "sha": sha},
    )
    if status in (200, 201):
        return "created"
    # Ours, from an earlier job of this run (acquire runs before the install, and
    # the release job re-derives the same name) or from a retried step.
    if status == 422 and "already exists" in _message(body).lower():
        return "exists"
    if status in (401, 403, 404):
        return "denied"
    raise SystemExit(
        f"::error::could not create the tenant lease ticket {ticket.ref(prefix)} "
        f"in {repo}: HTTP {status} {_message(body)!r}"
    )


def list_tickets(repo: str, prefix: str) -> list[Ticket]:
    """Every parseable ticket currently under the prefix, unordered."""
    status, body = gh_request("GET", f"repos/{repo}/git/matching-refs/{prefix}/")
    # An empty namespace answers 200 with [], but 404 is the documented shape for
    # "no matching refs" on some paths — both mean "nobody is queued".
    if status == 404:
        return []
    if status >= 400 or not isinstance(body, list):
        raise SystemExit(
            f"::error::could not list tenant lease tickets under refs/{prefix}/ "
            f"in {repo}: HTTP {status} {_message(body)!r}"
        )
    tickets = []
    for entry in body:
        if not isinstance(entry, dict):
            continue
        ticket = parse_ticket(str(entry.get("ref", "")))
        if ticket is not None:
            tickets.append(ticket)
    return tickets


def delete_ticket(repo: str, prefix: str, ticket: Ticket) -> bool:
    """Best-effort delete. False means it was already gone, which is not a fault:
    two contenders can reap the same dead ticket, and only one wins the race."""
    status, _body = gh_request(
        "DELETE", f"repos/{repo}/git/refs/{prefix}/{ticket.name}"
    )
    return status in (200, 204)


def run_is_live(
    repo: str,
    ticket: Ticket,
    *,
    ttl_seconds: int,
    now=None,
) -> bool:
    """Is the run holding `ticket` still going?

    Errs towards True. A transient API failure that read as "dead" would hand
    the tenant to a second run while the first is mid-install — the exact race
    the lease exists to prevent — whereas erring towards "live" only costs the
    waiter another poll interval.
    """
    status, body = gh_request("GET", f"repos/{repo}/actions/runs/{ticket.run_id}")
    if status == 404:
        # The run was deleted, or never existed. Nothing will ever release this.
        return False
    if status >= 400 or not isinstance(body, dict):
        print(
            f"::warning::could not read run {ticket.run_id} in {repo} "
            f"(HTTP {status}); treating its tenant lease as still held."
        )
        return True
    if body.get("status") == _COMPLETED:
        return False
    if ttl_seconds > 0:
        age = _run_age_seconds(body, now=now)
        if age is not None and age > ttl_seconds:
            print(
                f"::warning::run {ticket.run_id} in {repo} still reports "
                f"'{body.get('status')}' after {int(age)}s, past the "
                f"{ttl_seconds}s tenant lease TTL — breaking the lease. If that "
                "run is genuinely still installing, raise ttl-seconds."
            )
            return False
    return True


def _run_age_seconds(run_body: dict, *, now=None) -> float | None:
    """Seconds since the run was created, or None if the API did not say.

    Only the TTL backstop uses this, so clock skew between the runner and GitHub
    is immaterial at the hours-long scale the TTL operates on. Queue ordering
    deliberately does not depend on any clock — see the module docstring.
    """
    created = run_body.get("created_at")
    if not isinstance(created, str) or not created:
        return None
    try:
        stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    current = now() if now is not None else datetime.now(timezone.utc)
    return (current - stamp).total_seconds()


def acquire(
    repo: str,
    prefix: str,
    ticket: Ticket,
    sha: str,
    *,
    wait_seconds: int,
    poll_seconds: int,
    ttl_seconds: int,
    sleep=time.sleep,
    now=None,
) -> tuple[str, Ticket | None]:
    """Take the lease, or report why not.

    Returns ("acquired" | "disabled" | "timeout", blocking_ticket_or_None).
    """
    outcome = create_ticket(repo, prefix, ticket, sha)
    if outcome == "denied":
        print(
            "::warning::this repository will not let the run write git refs, so "
            "the (app, cloud) tenant lease is disabled for this run and e2e "
            "proceeds unserialised. Grant the lease job 'contents: write' to "
            "enable it. Each e2e leg still asserts the installed version "
            "independently (FND-31), so a wrong-version run cannot pass "
            "silently — it just fails less informatively."
        )
        return "disabled", None
    print(f"Ticket {ticket.ref(prefix)} {outcome}.")

    # Attempt-counted rather than deadline-checked, so the budget needs no clock
    # (and tests need no clock stub). Ceiling division: a budget that is not an
    # exact multiple of the interval still gets its final full attempt.
    max_attempts = max(1, math.ceil(wait_seconds / poll_seconds))

    blocker: Ticket | None = None
    for attempt in range(1, max_attempts + 1):
        tickets = list_tickets(repo, prefix)
        if ticket not in tickets:
            # Either replica lag, or a peer reaped us after misreading this run
            # as finished. Re-creating is safe and position-preserving: the name
            # is derived from the run, so we land back in the same place in line.
            if create_ticket(repo, prefix, ticket, sha) == "denied":
                print(
                    "::warning::lost the tenant lease ticket and cannot recreate "
                    "it; proceeding unserialised."
                )
                return "disabled", None
            tickets.append(ticket)

        ordered = sorted(tickets)
        blocker = None
        for candidate in ordered:
            if candidate == ticket:
                break
            if run_is_live(repo, candidate, ttl_seconds=ttl_seconds, now=now):
                blocker = candidate
                break
            print(
                f"Reaping the tenant lease ticket of run {candidate.run_id} "
                f"(attempt {candidate.attempt}) — that run is over."
            )
            delete_ticket(repo, prefix, candidate)

        if blocker is None:
            print(f"Tenant lease acquired: refs/{prefix}/{ticket.name}")
            return "acquired", None

        ahead = ordered.index(blocker) + 1
        print(
            f"[{attempt}/{max_attempts}] waiting for the {prefix} tenant lease — "
            f"held by run {blocker.run_id} ({blocker.run_url(repo)}), "
            f"{ahead} ticket(s) ahead of this run."
        )
        if attempt < max_attempts:
            sleep(poll_seconds)

    return "timeout", blocker


def release(repo: str, prefix: str, ticket: Ticket) -> bool:
    """Give up this run's ticket. Never fails the job — a missed release is
    reaped by the next contender, which is what makes a cancelled run (whose
    release job never starts) harmless."""
    deleted = delete_ticket(repo, prefix, ticket)
    if deleted:
        print(f"Tenant lease released: refs/{prefix}/{ticket.name}")
    else:
        print(
            f"No tenant lease ticket to release at refs/{prefix}/{ticket.name} "
            "— already reaped."
        )
    return deleted


def write_outputs(outputs: dict[str, str], path: str | None) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Acquire or release a tenant lease.")
    parser.add_argument("--mode", required=True, choices=("acquire", "release"))
    parser.add_argument("--repo", required=True, help="owner/repo the tickets live in.")
    parser.add_argument("--app", required=True, help="App under test (lease key part).")
    parser.add_argument(
        "--cloud", default="", help="Cloud (lease key part); empty = single tenant."
    )
    parser.add_argument("--run-id", required=True, type=int, help="github.run_id")
    parser.add_argument(
        "--run-attempt", required=True, type=int, help="github.run_attempt"
    )
    parser.add_argument(
        "--sha", default="", help="Commit the ticket ref points at (acquire only)."
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=5400,
        help="How long to queue for the tenant before giving up (default 90min).",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="Gap between queue checks. Two API calls per poll, so this also sets "
        "the load on the 1000/hour GITHUB_TOKEN budget (default 30s).",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=14400,
        help="Age at which a still-'in_progress' holder is treated as wedged and "
        "its lease broken. Must exceed the longest legitimate run (default 4h, "
        "against a 40min install plus a 120min leg ceiling).",
    )
    parser.add_argument(
        "--on-timeout",
        choices=("fail", "warn"),
        default="fail",
        help="fail (default): the job goes red naming the holder. warn: proceed "
        "unserialised.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    prefix = lease_prefix(args.app, args.cloud)
    ticket = Ticket(args.run_id, args.run_attempt)

    if args.mode == "release":
        release(args.repo, prefix, ticket)
        return 0

    if not args.sha:
        raise SystemExit("::error::--sha is required to acquire a lease")

    state, blocker = acquire(
        args.repo,
        prefix,
        ticket,
        args.sha,
        wait_seconds=args.wait_seconds,
        poll_seconds=args.poll_seconds,
        ttl_seconds=args.ttl_seconds,
    )
    write_outputs(
        {
            "acquired": "true" if state == "acquired" else "false",
            "state": state,
            "lease-ref": ticket.ref(prefix),
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
            f"::warning::gave up waiting for the {prefix} tenant lease after "
            f"{args.wait_seconds}s and is proceeding unserialised.{held_by}"
        )
        return 0
    print(
        f"::error::could not get the {prefix} tenant lease within "
        f"{args.wait_seconds}s.{held_by} Nothing is wrong with this change — the "
        "tenant is busy. Re-run this job once that run finishes, or raise "
        "wait-seconds if queueing this deep is expected."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
