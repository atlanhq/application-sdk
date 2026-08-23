#!/usr/bin/env python3
"""At most one LIVE connector e2e dispatch per ``(dispatching SHA, app)``, and
none at all for a SHA the PR has already moved past.

Why this exists
---------------
GitHub can emit several ``pull_request`` events for one head SHA. Observed on
PR #3306 (FND-646): ``opened`` at 01:17:18, then ``labeled e2e`` at 01:17:19 and
again at 01:17:20 — three events, one commit. Each spawned a full ``PR Checks``
run, each satisfied the connector gate independently, and each dispatched every
connector repo. One commit produced three ``Tests`` runs in
``atlan-openapi-app``, and those three runs then fought over the same three
cloud tenants: the winner ran the suite in ten minutes, and the two losers split
the freed leases between them and blocked on each other for the full 90-minute
wait budget.

The amplification is what this module removes, at source. The duplicate ``PR
Checks`` runs still happen — GitHub creates them and nothing in a workflow's
``if:`` can tell two identical ``labeled`` events apart — but only the first one
to get here reaches a tenant.

Why not ``concurrency:``
-----------------------
``cancel-in-progress`` was considered and rejected. *Once the dispatch has
fired* it changes nothing: the dispatch is fire-and-forget (``e2e-apps`` in
``wait-mode: callback`` creates the check run, dispatches, and exits), so
cancelling the SDK-side run does not cancel the connector run it already started
— it only orphans it, and the tenant contention is unchanged.

That is not the same as "cancellation could never have helped", and the
stale-head section below is the counter-example: a run spends ~8 minutes
building the base image before it reaches the dispatch, and a cancel landing in
that window would have stopped the fan-out outright. Two things still rule it
out. A run cancelled between creating its check run and dispatching leaves a
check that nothing will ever complete. And a queueing group is worse than
useless here: GitHub holds exactly ONE pending run per group, so a third arrival
cancels the waiter with no log output at all — FND-218, the failure the tenant
lease exists to replace, reintroduced in front of the dispatch. The head check
below buys the same tenant time with neither hazard.

Cancellation is also unsafe as a steady-state mechanism, which is worth stating
because it is the obvious first idea: ``prepare-tenant`` carries ``if:
always()``, so a cancelled connector run still finishes installing the app onto
the tenants it had already leased on its way out. Cancelling is an acceptable
manual escape hatch. It is not a design.

The protocol
------------
One ref per ``(app, sha)``, with a FIXED name::

    refs/e2e-dispatch/<app>/<sha>

``POST /git/refs`` on a name that already exists returns 422, and that 422 is an
atomic test-and-set evaluated by GitHub: of N simultaneous callers exactly one
sees 201, whatever the order. Same primitive as the ``(app, cloud)`` tenant
lease (``.github/actions/e2e-tenant-lease``), deliberately — the precedent and
the failure modes are already understood, and the alternative (probe the SHA for
an existing ``Connector E2E run / <app>`` check and skip if present) is
read-then-act with no atomicity. The observed gap between the duplicate dispatch
steps was 12 seconds, so a probe would have held here; it is simply not the
thing to build when a CAS is available.

The ref points at a **blob** recording who claimed it. A ref can target a blob,
which is what lets one atomic creation both take the slot and say who took it.

"At most one LIVE dispatch", not "one ever"
------------------------------------------
A claim that never expires would be a footgun: re-running the dispatch job (the
normal way to retry a transient dispatch failure) would silently no-op, and e2e
for that commit could never be re-run at all. So an existing claim is not
automatically final. A contender that finds the slot taken resolves it as:

* claimed by **this run** (any attempt) — proceed. This is a job re-run, and the
  operator asking for a re-run is asking for a re-dispatch.
* claimed by another run, and a ``Connector E2E run / <app>`` check run exists on
  this SHA that has **not concluded**, or whose newest attempt concluded as a
  PASS — **skip**. Either the dispatch is still being answered or it has been
  answered; its verdict is that check, and the connector gate on every one of the
  duplicate runs polls the same check and reports the same answer.
* claimed by another run, every check under that name has concluded, the NEWEST
  concluded non-passing, and the claiming run is **over** — reap the claim and
  re-dispatch. A failed leg is the one already-dispatched state with a question
  still worth asking, and re-adding the ``e2e`` label is how people ask it.
  Newest rather than any, because a SHA retried once keeps the old attempt's
  failed check and any-failure would re-trigger forever; every attempt concluded
  rather than just the newest, because an older one still running means its
  connector run is still at the tenant.
* same, but the claiming run is **still live** — skip. It may be mid-re-run of
  its own dispatch job (see the first case), and that plus a reclaim here is two
  dispatches contending for one tenant.
* claimed by another run with no such check, and that run is **still live** —
  skip. A peer is between its claim and its check creation, a window of one API
  call.
* claimed by another run with no such check, and that run is **over** — reap the
  claim and take it. The claimer died before dispatching, so without this the
  slot would stay taken by a run that will never dispatch, and this SHA would
  have no e2e at all.

The check run is the "did it dispatch" evidence rather than a second field in the
blob because it already exists, is created immediately before the dispatch by the
claimer, and is already swept when abandoned (``e2e-callback-watchdog.yml``).
Reusing it keeps the claim a single write.

Failure posture: fail OPEN
--------------------------
Every failure to operate the guard — no ref-write permission, a rate limit, an
unparseable answer — proceeds with the dispatch and prints a ``::warning::``.
That direction is deliberate and it is the opposite of the tenant lease's:

* The lease protects a shared mutable tenant, so proceeding unserialised risks
  two installers on one tenant. It fails open only on permissions, and never on
  a rate limit.
* This guard protects only against *duplicate* dispatches. Failing closed would
  drop the single dispatch that was supposed to happen, and a PR whose e2e
  silently never ran is far worse than a PR that pays for two e2e runs — which
  is, in any case, exactly the pre-existing behaviour.

So the worst case for a broken guard is the status quo.

The other duplication: one PR, two head SHAs
--------------------------------------------
The CAS keys on the SHA, so it is blind by construction to the *sequential*
duplicate: commit A's ``PR Checks`` run is still working its way towards the
dispatch when commit B lands, and both then dispatch — two different SHAs, two
uncontested claims, two full fan-outs. Nothing is duplicated in the CAS's terms
and everything is duplicated in the tenants' terms. The connector-side lease
behaves correctly and that is precisely the problem: it QUEUES, so commit B waits
out commit A's entire install-plus-legs cycle before touching a tenant. On PR
#3322 that cost 3m38s, 10m12s and 12m of pure lease wait across three connectors,
all of it spent behind a commit that was already history (FND-696).

The fix is one API call on the ``pull_request`` path: if ``--sha`` is no longer
the head of ``--pr-number``, skip. Not cancellation — the stale run has not
dispatched yet when this runs, so there is nothing to cancel, and cancelling a
connector run that HAS started is unsafe anyway for the ``prepare-tenant``
reason above. Unreadable answers dispatch, in keeping with the fail-open posture:
a stale run costs tenant time, a wrongly-skipped head commit costs the PR its
e2e.

It closes the window up to the dispatch and nothing past it. A push landing
later leaves a connector run already in flight, which is two cases, not one. If
that run has ACQUIRED a lease, there is nothing to do — cancelling it is unsafe
for the ``prepare-tenant`` reason above, so the queue is the right answer. If it
has dispatched but NOT yet leased — still building, unit-testing and
integration-testing, 2m30s on the openapi leg of PR #3322 — it holds nothing and
could stand down for free. That second case needs a check on the connector side,
immediately before the lease, and is tracked as FND-701.

Pruning
-------
Claim refs are per-SHA, so the namespace would otherwise grow without bound (the
merge queue dispatches on every SDK merge). On the claiming path only, this
prunes its own app's claims: a claim is deletable when its SHA is not the head of
any open PR *and* that SHA's ``Connector E2E run / <app>`` check is completed or
absent — i.e. when no dispatch for it can still be live. Bounded pages, a bounded
number of deletes per invocation, and every failure is a warning: pruning is
housekeeping and must never be able to fail a dispatch.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

# Outside refs/heads and refs/tags on purpose: a branch would fire `push` events
# and show up in the branch list, a tag would pollute the release surface. A
# custom namespace is inert — nothing watches it.
REF_NAMESPACE = "e2e-dispatch"

# The only GitHub run status that means "over". Everything else — queued,
# in_progress, requested, waiting, pending — is treated as live, so an
# unfamiliar future status errs towards leaving a peer's claim alone.
_COMPLETED = "completed"

# Transport-level retries (curl could not complete the request, or GitHub
# answered 5xx). Few and short: this runs once per dispatch, not in a poll loop,
# and every failure path here fails open anyway.
_TRANSPORT_ATTEMPTS = 3
_TRANSPORT_BACKOFF_SECONDS = 2

# Pruning budget. Five pages of 100 covers ~500 claims for one app, which is far
# past the steady state the prune itself maintains; the delete cap bounds the
# API cost of the first invocation after a long gap without it.
_PRUNE_MAX_PAGES = 5
_PRUNE_MAX_DELETES = 25
_OPEN_PR_MAX_PAGES = 5

# Pages of check runs on ONE commit. Ten (1000 checks) is far past what any SHA
# here carries; the cap exists so a pathological commit cannot spin, and hitting
# it is reported as "unreadable" rather than "no dispatch found" — the direction
# that fails open.
_CHECKS_MAX_PAGES = 10

# Conclusions that mean "this leg has an answer, do not re-dispatch it". Kept
# identical to poll_check_runs_gate.PASSING_CONCLUSIONS: the gate calls these a
# pass, so treating any of them as retryable here would re-run a leg the required
# check is already green on. Everything else — failure, cancelled, timed_out,
# action_required, stale — is a question still worth re-asking.
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})

_SAFE_SLUG_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
_SHA_RE = re.compile(r"\A[0-9a-f]{7,64}\Z")


def slug(value: str, *, default: str = "app") -> str:
    """Reduce a free-text key to one safe git ref path component.

    Deliberately narrower than git's own rules: the app name comes from a
    workflow input, and a ref name is the one place where "mostly valid" turns
    into a 422 nobody expected — and here a 422 *means something else*.
    """
    cleaned = "".join(
        char if char in _SAFE_SLUG_CHARS else "-" for char in value.strip().lower()
    )
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "-")
    cleaned = cleaned.strip("-._")
    while cleaned.endswith(".lock"):
        cleaned = cleaned[: -len(".lock")].strip("-._")
    return cleaned or default


def claim_ref(app: str, sha: str) -> str:
    """The single ref every contender for one ``(app, sha)`` dispatch races for.

    App first, SHA second, so ``git/matching-refs/e2e-dispatch/<app>`` lists one
    app's claims and the prune never has to walk the whole namespace.
    """
    return f"refs/{REF_NAMESPACE}/{slug(app)}/{sha}"


def sha_of(ref: str) -> str:
    """The SHA component of a claim ref, or "" if it does not look like one."""
    tail = ref.rsplit("/", 1)[-1].strip().lower()
    return tail if _SHA_RE.match(tail) else ""


@dataclass(frozen=True)
class Claimant:
    """Who claimed a dispatch slot, read back from the blob the ref points at."""

    run_id: int
    attempt: int
    claimed_at: float | None

    def run_url(self, repo: str) -> str:
        return f"https://github.com/{repo}/actions/runs/{self.run_id}"

    def is_my_run(self, run_id: int) -> bool:
        """Same RUN, any attempt.

        Attempt is recorded for the log, not for the decision: a re-run of the
        dispatch job bumps the attempt and must be allowed to dispatch again,
        which is the whole reason this compares run ids only.
        """
        return self.run_id == run_id


@dataclass(frozen=True)
class Response:
    """One API answer. Headers are carried because rate-limit detection needs
    them."""

    status: int
    headers: dict[str, str]
    body: object | None

    @property
    def message(self) -> str:
        return self.body.get("message", "") if isinstance(self.body, dict) else ""


class GuardUnavailable(Exception):
    """The guard could not be operated, so the dispatch proceeds unguarded.

    An exception rather than a return value so no call site can forget that the
    failure direction here is "dispatch anyway" — see the module docstring.
    """


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the HTTP client."""
    return subprocess.run(cmd, **kwargs)


def _parse_http(raw: str, label: str) -> Response:
    """Split a ``curl -i`` response into status, headers and parsed body."""
    text = raw.replace("\r\n", "\n")
    if "\n\n" not in text:
        raise GuardUnavailable(f"unexpected response for {label}: {text[:300]!r}")
    header_block, _, body = text.partition("\n\n")
    lines = header_block.splitlines()
    try:
        status_code = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise GuardUnavailable(
            f"could not parse the HTTP status line for {label}: "
            f"{(lines[0] if lines else '')!r}"
        ) from None
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

    curl, not ``gh api``, for the same reason ``e2e_tenant_lease.py`` and
    ``poll_check_runs_gate.py`` use it: ``gh api`` treats every non-2xx as a
    command failure and prints its own diagnostic instead of the response. Here
    the non-2xx codes are the entire mechanism — 422 on ref creation IS the slot
    being taken.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise GuardUnavailable("GH_TOKEN (or GITHUB_TOKEN) is not set")

    cmd = [
        "curl",
        "-sS",
        "-i",
        "--max-time",
        "30",
        "-X",
        method,
        # Authorization read from stdin (-K -) rather than -H so the token never
        # appears in curl's argv, where anything else on the runner could read it
        # from /proc/<pid>/cmdline. Re-supplied per attempt below because stdin
        # is consumed per process — a retry reusing only the argv would send an
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
    for attempt in range(1, _TRANSPORT_ATTEMPTS + 1):
        result = run(cmd, input=config, capture_output=True, text=True, check=False)
        last = attempt == _TRANSPORT_ATTEMPTS

        if result.returncode != 0:
            if last:
                raise GuardUnavailable(
                    f"curl failed for {label} after {_TRANSPORT_ATTEMPTS} "
                    f"attempts: {result.stderr.strip()}"
                )
        else:
            response = _parse_http(result.stdout, label)
            if response.status < 500 or last:
                return response

        sleep(_TRANSPORT_BACKOFF_SECONDS * 2 ** (attempt - 1))

    raise GuardUnavailable(f"exhausted transport attempts for {label}")


def _rate_limited(response: Response) -> bool:
    """Is this a rate limit rather than a permission problem?

    Both arrive as 403. The distinction changes only the wording of the warning
    here — both fail open — but conflating them makes a rate-limited hour look
    like a misconfigured permission, which is a diagnosis-hostile lie.
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
    exist, so 404 counts, but a rate limit explicitly does not."""
    return response.status in (401, 403, 404) and not _rate_limited(response)


def create_claim_blob(
    repo: str, run_id: int, attempt: int, now: float, pr_number: int | None = None
) -> str:
    """Write the claim record the ref will point at. Returns the blob sha.

    ``pr_number`` is written for a reader a repo away. The connector run this
    dispatch is about to start is handed only a SHA, and a SHA is not enough to
    find its PR once the branch has moved: ``GET /commits/{sha}/pulls`` answers
    with an empty list for a commit a force-push has passed, which is exactly the
    case worth detecting. So the claim — the record that authorises the dispatch —
    also carries the identity needed to retract it, and the connector's
    pre-lease recheck reads it back (``e2e-dispatch-recheck``, FND-701).

    Omitted rather than null when there is no PR (the merge_group path), so a
    reader cannot tell "no PR" apart from "a claim written before this field
    existed" — and does not need to: both mean "do not stand down".
    """
    payload: dict[str, object] = {
        "run_id": run_id,
        "attempt": attempt,
        "claimed_at": now,
    }
    if pr_number is not None:
        payload["pr_number"] = pr_number
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
    raise GuardUnavailable(
        f"could not write the dispatch claim record in {repo}: "
        f"HTTP {response.status} {response.message!r}"
        + (" (rate-limited)" if _rate_limited(response) else "")
    )


def try_claim(repo: str, ref: str, blob_sha: str) -> str:
    """One atomic attempt. Returns "claimed" or "taken"; raises otherwise.

    The 422 is not an error path bolted on — it is the lock.
    """
    response = gh_request(
        "POST", f"repos/{repo}/git/refs", {"ref": ref, "sha": blob_sha}
    )
    if response.status in (200, 201):
        return "claimed"
    if response.status == 422 and "already exists" in response.message.lower():
        return "taken"
    raise GuardUnavailable(
        f"could not create the dispatch claim {ref} in {repo}: "
        f"HTTP {response.status} {response.message!r}"
        + (" (rate-limited)" if _rate_limited(response) else "")
    )


def read_claim_target(repo: str, ref: str) -> str | None:
    """The sha the claim ref points at, or None if it does not exist."""
    response = gh_request("GET", f"repos/{repo}/git/ref/{ref.removeprefix('refs/')}")
    if response.status == 404:
        return None
    if response.status >= 400 or not isinstance(response.body, dict):
        raise GuardUnavailable(
            f"could not read the dispatch claim {ref}: HTTP {response.status}"
        )
    target = response.body.get("object") or {}
    sha = target.get("sha") if isinstance(target, dict) else None
    return str(sha) if sha else None


def read_claimant(repo: str, blob_sha: str) -> Claimant | None:
    """Decode the claim record a ref points at, or None if it is unreadable."""
    response = gh_request("GET", f"repos/{repo}/git/blobs/{blob_sha}")
    if response.status >= 400 or not isinstance(response.body, dict):
        print(
            f"::warning::a dispatch claim record ({blob_sha}) is unreadable "
            f"(HTTP {response.status})."
        )
        return None
    content = response.body.get("content")
    if not isinstance(content, str):
        return None
    try:
        record = json.loads(base64.b64decode(content))
    except (ValueError, TypeError):
        print("::warning::a dispatch claim record is not valid JSON.")
        return None
    if not isinstance(record, dict):
        return None
    try:
        run_id = int(record["run_id"])
        attempt = int(record["attempt"])
    except (KeyError, TypeError, ValueError):
        print("::warning::a dispatch claim record names no run.")
        return None
    claimed_at = record.get("claimed_at")
    return Claimant(
        run_id=run_id,
        attempt=attempt,
        claimed_at=float(claimed_at) if isinstance(claimed_at, (int, float)) else None,
    )


def delete_ref(repo: str, ref: str) -> bool:
    """Delete a claim ref. False ⇒ already gone, which is not a fault."""
    response = gh_request(
        "DELETE", f"repos/{repo}/git/refs/{ref.removeprefix('refs/')}"
    )
    return response.status in (200, 204)


def check_run_age_key(check_run: dict) -> tuple[str, int]:
    """Sort key that orders same-named check runs oldest-first.

    Deliberately a copy of ``poll_check_runs_gate.check_run_age_key`` rather than
    an import: nothing in .github/scripts imports a sibling script (every one is
    a self-contained stdlib-only entry point), and the two must agree or the
    guard would judge a retry against a different attempt than the gate reports.
    ``test_the_guard_and_the_gate_order_check_runs_identically`` imports both and
    holds them to the same table.
    """
    started_at = check_run.get("started_at")
    check_id = check_run.get("id")
    return (
        started_at if isinstance(started_at, str) else "",
        check_id if isinstance(check_id, int) else 0,
    )


@dataclass(frozen=True)
class DispatchState:
    """What the check runs on a SHA say about one app's dispatch."""

    #: Any check at all under this name, i.e. a dispatch happened (or is one API
    #: call away from happening).
    dispatched: bool
    #: EVERY check under this name has concluded. Not just the newest: an older
    #: attempt still in flight means its connector run is still at the tenant,
    #: and re-dispatching on top of that is the contention this module prevents.
    settled: bool
    #: Conclusion of the newest attempt, None if it has not concluded or carries
    #: no conclusion. Newest, not folded: a retried SHA keeps the old attempt's
    #: check, so any-failure would re-trigger a leg that has since passed on
    #: every subsequent event, forever.
    newest_conclusion: str | None

    @property
    def retryable(self) -> bool:
        """Is there a failed answer here that is worth asking again?"""
        return (
            self.dispatched
            and self.settled
            and self.newest_conclusion is not None
            and self.newest_conclusion not in _PASSING_CONCLUSIONS
        )


def read_dispatch_state(
    repo: str, sha: str, check_run_name: str
) -> DispatchState | None:
    """One listing, both answers. None when the listing is unreadable.

    Deliberately not two calls (``dispatch_exists`` plus a conclusion read):
    they would be two separate listings of the same endpoint that could disagree
    across the gap, and the second would be pure cost.
    """
    matches = named_check_runs(repo, sha, check_run_name)
    if matches is None:
        return None
    if not matches:
        return DispatchState(dispatched=False, settled=False, newest_conclusion=None)
    newest = max(matches, key=check_run_age_key)
    conclusion = newest.get("conclusion")
    return DispatchState(
        dispatched=True,
        settled=all(entry.get("status") == _COMPLETED for entry in matches),
        newest_conclusion=conclusion if isinstance(conclusion, str) else None,
    )


def checks_state(repo: str, sha: str, check_run_name: str) -> str | None:
    """ "absent", "running" or "done" for ``check_run_name`` on ``sha``.

    None when the answer is unreadable. Three states rather than a boolean
    because "absent" and "done" mean opposite things to the prune: a claim whose
    check was never created may still be about to dispatch, while one whose check
    has concluded cannot.

    Paginated, and "absent" is only returned once every page has been read. A
    single ``per_page=100`` GET would report a named check living past page one as
    "never dispatched", which is the one wrong answer that costs a tenant: the
    reclaim path would then fire a second connector run. This repo's SHAs already
    carry enough checks for that to be reachable, which is why
    ``poll_check_runs_gate.py`` paginates the same endpoint.

    Anything that prevents full coverage — a failed page, a missing page, more
    pages than the cap — is "unreadable" rather than "absent", so the callers
    fall open instead of acting on a partial list.
    """
    matches = named_check_runs(repo, sha, check_run_name)
    if matches is None:
        return None
    if not matches:
        return "absent"
    return "done" if all(e.get("status") == _COMPLETED for e in matches) else "running"


def named_check_runs(repo: str, sha: str, check_run_name: str) -> list[dict] | None:
    """Every check run on ``sha`` called ``check_run_name``; None if unreadable.

    A list rather than a verdict because a retried SHA legitimately carries more
    than one: ``create_check_run.py`` POSTs a fresh check per dispatch instead of
    PATCHing the previous attempt's. Callers decide what to do with several —
    ``checks_state`` folds them (any still running ⇒ running), while
    ``newest_conclusion`` picks the latest.

    See ``checks_state`` for why this paginates and why partial coverage is None.
    """
    matches: list[dict] = []
    seen = 0
    for page in range(1, _CHECKS_MAX_PAGES + 1):
        response = gh_request(
            "GET",
            f"repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}",
        )
        if response.status >= 400 or not isinstance(response.body, dict):
            print(
                f"::warning::could not read the check runs on {sha} "
                f"(HTTP {response.status})."
            )
            return None
        runs = response.body.get("check_runs")
        if not isinstance(runs, list):
            return None
        matches.extend(
            entry
            for entry in runs
            if isinstance(entry, dict) and entry.get("name") == check_run_name
        )
        seen += len(runs)

        total = response.body.get("total_count")
        # total_count makes coverage decidable; without it a short page is the
        # only end-of-list signal left.
        if seen >= total if isinstance(total, int) else len(runs) < 100:
            break
        if not runs:
            # A page that adds nothing while the count says there is more: the
            # listing cannot be completed, so it is unreadable, not empty.
            print(
                f"::warning::the check-run listing for {sha} stopped short of "
                f"its own total_count ({seen}/{total})."
            )
            return None
    else:
        print(
            f"::warning::{sha} carries more than {_CHECKS_MAX_PAGES} pages of "
            "check runs, so this could not be read in full."
        )
        return None

    return matches


def claim_is_settled(repo: str, ref: str, sha: str, check_run_name: str) -> bool | None:
    """Can nothing that depends on this claim still be live?

    The prune's safety condition. A concluded check settles it outright. A check
    that is still running does not. A claim with NO check has to fall back to the
    claimant's run: a peer between its claim and its check creation is a window
    of one API call, and pruning inside that window would let a duplicate event
    for the same SHA dispatch a second time.
    """
    state = checks_state(repo, sha, check_run_name)
    if state is None:
        return None
    if state == "running":
        return False
    if state == "done":
        return True

    target = read_claim_target(repo, ref)
    if target is None:
        return False
    claimant = read_claimant(repo, target)
    if claimant is None:
        return None
    return not run_is_live(repo, claimant.run_id)


def run_is_live(repo: str, run_id: int) -> bool:
    """Is the claiming run still going?

    Errs towards True. Reading a transient API failure as "over" would reap a
    live peer's claim and produce the duplicate dispatch this exists to prevent,
    whereas erring towards "live" only skips a dispatch whose verdict is already
    on its way.
    """
    response = gh_request("GET", f"repos/{repo}/actions/runs/{run_id}")
    if response.status == 404:
        # Deleted, or never existed. Nothing will ever dispatch for this claim.
        return False
    if response.status >= 400 or not isinstance(response.body, dict):
        print(
            f"::warning::could not read run {run_id} in {repo} "
            f"(HTTP {response.status}); treating it as still live."
        )
        return True
    return response.body.get("status") != _COMPLETED


def pr_head_sha(repo: str, number: int) -> str | None:
    """The head SHA the PR currently points at, or None if it could not be read.

    None is "unknown", never "no head": every caller treats an unreadable answer
    as "not superseded" and dispatches, because the only thing worse than paying
    for a stale e2e run is a PR head whose e2e silently never ran.
    """
    response = gh_request("GET", f"repos/{repo}/pulls/{number}")
    if response.status >= 400 or not isinstance(response.body, dict):
        print(
            f"::warning::could not read pull request #{number} "
            f"(HTTP {response.status}); dispatching without the stale-head check."
        )
        return None
    head = response.body.get("head")
    if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
        print(
            f"::warning::pull request #{number} reported no head sha; "
            "dispatching without the stale-head check."
        )
        return None
    return head["sha"].strip().lower()


def open_pr_heads(repo: str) -> set[str] | None:
    """Head SHAs of every open PR, or None if the list could not be read.

    None is not an empty set: an unreadable list must not license pruning every
    claim in the namespace.
    """
    heads: set[str] = set()
    for page in range(1, _OPEN_PR_MAX_PAGES + 1):
        response = gh_request(
            "GET", f"repos/{repo}/pulls?state=open&per_page=100&page={page}"
        )
        if response.status >= 400 or not isinstance(response.body, list):
            print(
                "::warning::could not list open pull requests "
                f"(HTTP {response.status}); skipping the dispatch-claim prune."
            )
            return None
        for entry in response.body:
            if isinstance(entry, dict):
                head = entry.get("head")
                if isinstance(head, dict) and isinstance(head.get("sha"), str):
                    heads.add(head["sha"].lower())
        if len(response.body) < 100:
            break
    return heads


def list_claims(repo: str, app: str) -> list[str] | None:
    """Every claim ref for one app, or None if the listing failed."""
    prefix = f"{REF_NAMESPACE}/{slug(app)}"
    refs: list[str] = []
    seen: set[str] = set()
    for page in range(1, _PRUNE_MAX_PAGES + 1):
        response = gh_request(
            "GET", f"repos/{repo}/git/matching-refs/{prefix}?per_page=100&page={page}"
        )
        if response.status == 404:
            # No claims for this app yet. matching-refs is documented to answer
            # with an empty array, but the older single-ref endpoint 404s and the
            # two have been confused before, so both are accepted as "none".
            return refs
        if response.status >= 400 or not isinstance(response.body, list):
            print(
                f"::warning::could not list dispatch claims for {app} "
                f"(HTTP {response.status}); skipping the prune."
            )
            return None
        fresh = 0
        for entry in response.body:
            if isinstance(entry, dict) and isinstance(entry.get("ref"), str):
                ref = entry["ref"]
                if ref not in seen:
                    seen.add(ref)
                    refs.append(ref)
                    fresh += 1
        # Two stop conditions, because matching-refs does not document `page`:
        # a short page means the end, and a page that adds nothing new means the
        # parameter was ignored and we are re-reading page one.
        if len(response.body) < 100 or fresh == 0:
            break
    return refs


def prune(repo: str, app: str, keep_sha: str, check_run_name: str) -> int:
    """Delete claims that no live dispatch can still depend on.

    Deletable means: not the SHA being claimed now, not the head of an open PR,
    and that SHA's check run is finished or was never created. Merge-queue SHAs
    are never open PR heads, which is what keeps the namespace from growing with
    every merge — and they are safe to forget, because no second dispatch event
    can arrive for a merge-queue SHA once its run is over.

    Best-effort throughout: every failure returns early with a warning. Pruning
    is housekeeping and must never fail a dispatch.
    """
    refs = list_claims(repo, app)
    if not refs:
        return 0
    heads = open_pr_heads(repo)
    if heads is None:
        return 0

    deleted = 0
    for ref in refs:
        if deleted >= _PRUNE_MAX_DELETES:
            print(
                f"Reached the per-dispatch prune cap of {_PRUNE_MAX_DELETES} "
                f"stale dispatch claims for {app}; the rest go next time."
            )
            break
        sha = sha_of(ref)
        if not sha or sha == keep_sha.lower() or sha in heads:
            continue
        if claim_is_settled(repo, ref, sha, check_run_name) is not True:
            continue
        if delete_ref(repo, ref):
            deleted += 1
    if deleted:
        print(f"Pruned {deleted} stale dispatch claim(s) for {app}.")
    return deleted


def resolve(
    repo: str,
    ref: str,
    *,
    app: str,
    sha: str,
    check_run_name: str,
    run_id: int,
    attempt: int,
    pr_number: int | None = None,
    clock=time.time,
) -> tuple[str, Claimant | None]:
    """Decide whether this run should dispatch.

    Returns ("claimed" | "reclaimed" | "duplicate", the blocking claimant or
    None). Raises ``GuardUnavailable`` when the guard cannot be operated, which
    the caller turns into "dispatch anyway".
    """
    target = read_claim_target(repo, ref)
    if target is None:
        blob = create_claim_blob(repo, run_id, attempt, clock(), pr_number)
        if try_claim(repo, ref, blob) == "claimed":
            return "claimed", None
        # Somebody won the race between the read and the CAS. The CAS was the
        # authority, as intended; re-read and judge the winner below.
        target = read_claim_target(repo, ref)
        if target is None:
            # Taken and then released inside one round trip. Nothing sane does
            # that, so rather than loop, fail open — a duplicate dispatch is the
            # status quo and an unexplained state is not worth guessing at.
            raise GuardUnavailable(
                f"{ref} was taken and gone again within one round trip"
            )

    claimant = read_claimant(repo, target)
    if claimant is None:
        # An unreadable record cannot be attributed, and attributing it wrongly
        # in either direction is worse than the status quo.
        raise GuardUnavailable(f"the claim record behind {ref} is unreadable")

    if claimant.is_my_run(run_id):
        print(
            f"This run already claimed the dispatch slot {ref} "
            f"(attempt {claimant.attempt}); re-dispatching."
        )
        return "claimed", None

    state = read_dispatch_state(repo, sha, check_run_name)
    if state is None:
        raise GuardUnavailable(f"could not tell whether {sha} was already dispatched")
    if state.dispatched:
        # A leg that has already been dispatched is normally settled: the check
        # run is the verdict and every duplicate run's gate reads it. The one
        # exception is a leg that dispatched and FAILED, because then there is a
        # question still worth asking — and re-adding the `e2e` label is what
        # people reach for to ask it. Before this, that re-label spent a full run
        # (both native base-image builds, Trivy, SDR K8s E2E) dispatching nothing
        # and re-reading the same red check.
        #
        # Three conditions, and every one of them is load-bearing:
        #
        # * NEWEST conclusion, not "any failure" — a SHA retried once keeps the
        #   old attempt's check, so any-failure would re-trigger a leg that has
        #   since passed, on every subsequent event, forever.
        # * A PASSING newest conclusion stays deduped. Re-testing a green leg
        #   costs a tenant to re-answer an answered question.
        # * The claimant run must be dead. A live claimant may be mid-re-run of
        #   its own dispatch job (``is_my_run`` lets it through), and that plus a
        #   reclaim here is two dispatches contending for one tenant — the exact
        #   harm this module exists to prevent.
        if state.retryable and not run_is_live(repo, claimant.run_id):
            print(
                f"Run {claimant.run_id} ({claimant.run_url(repo)}) dispatched "
                f"{app} for {sha} and its newest '{check_run_name}' concluded "
                f"{state.newest_conclusion!r}; that run is over, so this one "
                "reclaims the slot and re-dispatches."
            )
            return _reclaim(
                repo,
                ref,
                run_id=run_id,
                attempt=attempt,
                pr_number=pr_number,
                claimant=claimant,
                clock=clock,
            )
        print(
            f"Run {claimant.run_id} ({claimant.run_url(repo)}) already dispatched "
            f"{app} for {sha}; skipping this duplicate dispatch. Its "
            f"'{check_run_name}' check on this commit carries the verdict, and "
            "this run's connector gate reads that same check."
        )
        # Only the live-claimant case is left with something retryable in it, and
        # there the answer is that run, not this one: re-running IT is allowed
        # (``is_my_run`` compares run ids only, so a re-run bumps the attempt and
        # goes through). The whole run, not ``--failed``: a leg that dispatched
        # fine and then failed downstream leaves the dispatch job green, so
        # ``--failed`` re-runs the gate alone and it re-reads the same red check.
        if state.retryable:
            print(
                f"::notice::{app} e2e concluded {state.newest_conclusion!r} on "
                f"{sha[:8]} but run {claimant.run_id} is still going, so this run "
                f"cannot take the slot. To retry now: gh run rerun "
                f"{claimant.run_id} (the whole run — not --failed). Otherwise "
                "re-add the `e2e` label once that run finishes."
            )
        return "duplicate", claimant

    if run_is_live(repo, claimant.run_id):
        print(
            f"Run {claimant.run_id} ({claimant.run_url(repo)}) is claiming the "
            f"{app} dispatch for {sha} right now; skipping this duplicate."
        )
        return "duplicate", claimant

    print(
        f"::warning::run {claimant.run_id} claimed the {app} dispatch for {sha} "
        "and finished without dispatching; reclaiming the slot."
    )
    return _reclaim(
        repo,
        ref,
        run_id=run_id,
        attempt=attempt,
        pr_number=pr_number,
        claimant=claimant,
        clock=clock,
    )


def _reclaim(
    repo: str,
    ref: str,
    *,
    run_id: int,
    attempt: int,
    pr_number: int | None,
    claimant: Claimant,
    clock,
) -> tuple[str, Claimant | None]:
    """Take the slot from a finished claimant, or lose the race and stand down.

    Shared by both reclaim reasons (a claimant that never dispatched, and one
    whose dispatch failed) so there is one CAS, not two. Delete-then-create is
    what makes it safe with several contenders: the create is the compare-and-set,
    so exactly one wins and every other reads back "duplicate" rather than
    dispatching a second run at the same tenant.
    """
    delete_ref(repo, ref)
    blob = create_claim_blob(repo, run_id, attempt, clock(), pr_number)
    if try_claim(repo, ref, blob) == "claimed":
        return "reclaimed", None
    # Another contender reclaimed it first. It will dispatch; this one must not.
    print(f"Another run reclaimed {ref} first; skipping this duplicate dispatch.")
    return "duplicate", claimant


def write_outputs(outputs: dict[str, str], path: str | None) -> None:
    if not path:
        for key, value in outputs.items():
            print(f"{key}={value}")
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def summarise(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Claim the (dispatching SHA, app) connector e2e dispatch slot."
    )
    parser.add_argument(
        "--repo", required=True, help="owner/repo the claim ref lives in."
    )
    parser.add_argument(
        "--sha", required=True, help="The dispatching SHA (this repo's PR head)."
    )
    parser.add_argument("--app", required=True, help="Target connector repo name.")
    parser.add_argument(
        "--pr-number",
        default="",
        help="Number of the pull request whose head --sha is, on the "
        "pull_request path. Empty (the merge_group path) disables the "
        "stale-head check: a queue entry has no PR head to fall behind.",
    )
    parser.add_argument(
        "--check-run-name",
        required=True,
        help="The check run the claimer creates before dispatching, e.g. "
        "'Connector E2E run / atlan-mysql-app'. Its presence on --sha is the "
        "evidence that a dispatch already happened.",
    )
    parser.add_argument("--run-id", required=True, type=int, help="github.run_id")
    parser.add_argument(
        "--run-attempt", required=True, type=int, help="github.run_attempt"
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Skip the stale-claim prune. The prune is best-effort housekeeping; "
        "this exists so a caller can drop its API cost entirely.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    sha = args.sha.strip().lower()
    if not _SHA_RE.match(sha):
        # Fail open, like every other guard failure: a malformed SHA is a caller
        # bug, and blocking the dispatch would hide it behind a missing e2e run
        # rather than surfacing it.
        print(
            f"::warning::--sha {args.sha!r} is not a commit sha, so the duplicate "
            "dispatch guard cannot key on it; dispatching unguarded."
        )
        write_outputs(
            {
                "claimed": "true",
                "state": "disabled",
                "claim-ref": "",
                "holder-run-id": "",
            },
            os.environ.get("GITHUB_OUTPUT"),
        )
        return 0

    # ── Is this SHA still the thing under review? ────────────────────────────
    # The CAS below is keyed on (app, SHA), so it cannot see the OTHER shape of
    # duplication: one PR, two live head SHAs. A push that lands while the
    # previous commit's PR Checks run is still on its way to this step leaves two
    # runs dispatching two full fan-outs, and the connector-side tenant lease
    # then does exactly what it promises — it queues the current SHA behind the
    # obsolete one's whole install-plus-legs cycle. Measured on PR #3322: 3m38s
    # of lease wait in one connector, 10m12s in another, 12m in a third, every
    # second of it spent waiting for a commit nobody was going to merge
    # (FND-696).
    #
    # So: if this run's SHA is no longer the PR's head, the dispatch is dropped
    # here rather than serialised there. Cheaper than cancellation, and it needs
    # no write permission — at this point the stale run has not dispatched yet,
    # so there is nothing to cancel. (Once it HAS dispatched, this check cannot
    # help; the connector run is live and the lease queue is the right answer.)
    #
    # One API call, and unreadable means "not superseded": the failure that
    # matters is skipping the head commit's only dispatch, so every doubt
    # resolves towards dispatching.
    pr_number = args.pr_number.strip()
    if pr_number.isdigit():
        head = pr_head_sha(args.repo, int(pr_number))
        if head is not None and head != sha:
            print(
                f"::notice::skipping the {args.app} dispatch: {sha[:8]} is no "
                f"longer the head of PR #{pr_number} ({head[:8]} is). The tenant "
                "belongs to the head commit's run."
            )
            summarise(
                f"- **{args.app}**: stale dispatch skipped — `{sha[:8]}` was "
                f"superseded by `{head[:8]}` on PR #{pr_number}."
            )
            write_outputs(
                {
                    "claimed": "false",
                    "state": "superseded",
                    # Empty on purpose: no claim was taken, and naming a ref this
                    # run never wrote would read as one it holds.
                    "claim-ref": "",
                    "holder-run-id": "",
                },
                os.environ.get("GITHUB_OUTPUT"),
            )
            return 0

    ref = claim_ref(args.app, sha)
    try:
        state, blocker = resolve(
            args.repo,
            ref,
            app=args.app,
            sha=sha,
            check_run_name=args.check_run_name,
            run_id=args.run_id,
            attempt=args.run_attempt,
            # Recorded in the claim so the connector run this is about to start
            # can find its way back to the PR and recheck the head immediately
            # before it queues for a tenant (FND-701). None on the merge_group
            # path, where the same `--pr-number` is empty for the same reason:
            # a queue entry's SHA is not any PR's head.
            pr_number=int(pr_number) if pr_number.isdigit() else None,
        )
    except GuardUnavailable as unavailable:
        print(
            f"::warning::the duplicate-dispatch guard is not working ({unavailable}), "
            "so this run is dispatching unguarded. Worst case is the pre-existing "
            "behaviour: a duplicate connector run. Grant this job 'contents: write' "
            "(claim refs), 'checks: read', 'actions: read' and "
            "'pull-requests: read' if that is what "
            "is missing."
        )
        write_outputs(
            {
                "claimed": "true",
                "state": "disabled",
                "claim-ref": ref,
                "holder-run-id": "",
            },
            os.environ.get("GITHUB_OUTPUT"),
        )
        return 0

    if state != "duplicate" and not args.no_prune:
        try:
            prune(args.repo, args.app, sha, args.check_run_name)
        except GuardUnavailable as unavailable:
            print(f"::warning::the dispatch-claim prune stopped early: {unavailable}")

    if state == "duplicate" and blocker is not None:
        summarise(
            f"- **{args.app}**: duplicate dispatch skipped — "
            f"[run {blocker.run_id}]({blocker.run_url(args.repo)}) already "
            f"dispatched `{sha[:8]}`."
        )

    write_outputs(
        {
            "claimed": "false" if state == "duplicate" else "true",
            "state": state,
            "claim-ref": ref,
            "holder-run-id": str(blocker.run_id) if blocker else "",
        },
        os.environ.get("GITHUB_OUTPUT"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
