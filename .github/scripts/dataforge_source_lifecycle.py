"""Wake (resume) a paused DataForge e2e source before a run, pause it after.

Why this exists (FND-1992)
--------------------------
A DataForge-backed connector pins ONE provisioned instance as its e2e source
(``DATAFORGE_RESOURCE_ID``, ``category: "ci"`` so the lifecycle reaper skips it)
and leaves it PAUSED between runs to avoid paying for an always-on instance. A
paused instance fails every leg at source resolution, and CI had no way to wake
it. This is the wake/pause half:

  * ``--mode wake``  — resume the pin and poll until PROVISIONED (credentials
    resolving is not the same as the instance accepting connections, so
    readiness is a state poll, not just a 2xx). Resume is issued ONLY from
    PAUSED, at most once: the API rejects a resume mid-PAUSING, and while
    RESUMING one is already in flight.
  * ``--mode pause`` — pause it again after the run, under ``if: always()`` in
    the workflow so a cancelled or failed run never leaks a running instance.

Run as a checked-out ``.github/scripts`` script (NOT a composite action) for two
reasons: the call site already needs the sparse checkout + globalprotect-connect
to reach ``api.dataforge.atlan.dev`` over the VPN (that host admits only the VPN
egress IPs), and a script — unlike an isolated composite action — can import the
shared pieces instead of copying them. The GitHub-ref transport comes from
``_gh_refs`` (one source, with the FND-702 rate-limit/permission split intact);
the OIDC exchange and the error-class allowlist come from
``fetch_dataforge_source`` (one exchange, one allowlist). This mutates instance
state, so it exchanges for ``resource:lifecycle resource:read`` rather than the
fetch's ``credentials:read`` — the repo's DataForge workload binding must ALLOW
those scopes.

Concurrency — a cross-run refcount
----------------------------------
One pin is shared by every leg of a run and by concurrent runs. Legs WITHIN a
run are ordered by the workflow DAG (wake before any leg, pause after all), so
this only solves the CROSS-RUN case, as a refcount modelled on the tenant lease:
wake registers a per-run holder ref
``refs/dataforge-source/<resource>/holder/<run_id>-<attempt>``; pause deletes
this run's ref and pauses only when no OTHER *live* run still holds it (liveness
via the Actions API + a TTL backstop). Unlike the tenant lease this is a SHARED
refcount, not a mutex: woken on the first arrival, paused on the last departure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from enum import Enum
from pathlib import Path

# Sibling .github/scripts modules — imported, not copied. When invoked as
# `python3 <path>/dataforge_source_lifecycle.py` the script's own dir is on
# sys.path already; the insert makes `-m`/cwd invocations work too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gh_refs import (  # noqa: E402
    Holder,
    RateLimited,
    RefListError,
    delete_ref,
    holder_is_live,
    list_matching_refs,
    read_blob_json,
    slug,
    try_create_ref,
    write_blob,
)
from fetch_dataforge_source import (  # noqa: E402
    DataforgeSourceError,
    _error_class,
    _exchange_for_service_token,
    _github_oidc_token,
)

DEFAULT_BASE_URL = "https://api.dataforge.atlan.dev"
REF_NAMESPACE = "dataforge-source"

# resource:read to poll the state read, resource:lifecycle to resume/pause. The
# server grants requested ∩ binding.allowed_scopes, so this is a REQUEST.
LIFECYCLE_SCOPE = "resource:lifecycle resource:read"


class State(str, Enum):
    """DataForge resource statuses this script reasons about (entity.ResourceStatus)."""

    PROVISIONED = "PROVISIONED"
    PAUSED = "PAUSED"
    PAUSING = "PAUSING"
    RESUMING = "RESUMING"


# States a wake can never recover from — fail fast with a named error rather than
# polling to the readiness timeout.
TERMINAL_BAD_STATES = frozenset({"DENIED", "FAILED", "DECOMMISSIONED", "DELETED"})


class Outcome(str, Enum):
    """Typed results (asserted by tests; also make the state machine checkable)."""

    READY = "ready"
    CREATED = "created"
    HELD = "held"
    PAUSED = "paused"
    KEPT_AWAKE = "kept-awake"
    ALREADY = "already"


class DataforgeLifecycleError(RuntimeError):
    """A wake/pause step could not complete. Message is fixed and body-free."""


# ── DataForge resource HTTP (urllib; the seam tests monkeypatch) ─────────────


def _df_request(method: str, url: str, token: str, *, timeout: int = 30) -> dict:
    """One DataForge API call. Returns the parsed JSON body (``{}`` if empty).

    Error bodies are never echoed — only the status plus an allowlisted class
    (shared ``_error_class``/allowlist from fetch_dataforge_source).
    """
    req = urllib.request.Request(
        url, method=method, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raise DataforgeLifecycleError(
            f"dataforge {method} {url.split('?', 1)[0]} failed: "
            f"HTTP {exc.code} ({_error_class(exc)})"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DataforgeLifecycleError(
            f"dataforge {method} {url.split('?', 1)[0]} failed: "
            f"{type(exc).__name__} — is the VPN up?"
        ) from exc


def resource_status(base_url: str, token: str, resource_id: str) -> str:
    """GET /api/v1/resources/{id} -> status string (resource:read)."""
    url = f"{base_url}/api/v1/resources/{urllib.parse.quote(resource_id)}"
    doc = _df_request("GET", url, token)
    status = doc.get("status") if isinstance(doc, dict) else None
    if not status:
        raise DataforgeLifecycleError(
            "dataforge state read returned no status for the pinned resource "
            "(is DATAFORGE_RESOURCE_ID a resource UUID, not a name?)"
        )
    return str(status)


def resume_resource(base_url: str, token: str, resource_id: str) -> None:
    """POST /api/v1/resources/{id}/resume (resource:lifecycle)."""
    url = f"{base_url}/api/v1/resources/{urllib.parse.quote(resource_id)}/resume"
    _df_request("POST", url, token)


def pause_resource(base_url: str, token: str, resource_id: str) -> None:
    """POST /api/v1/resources/{id}/pause (resource:lifecycle)."""
    url = f"{base_url}/api/v1/resources/{urllib.parse.quote(resource_id)}/pause"
    _df_request("POST", url, token)


def service_token(base_url: str) -> str:
    """Exchange the run's OIDC token for a lifecycle-scoped SERVICE token."""
    return _exchange_for_service_token(
        base_url, _github_oidc_token(), scope=LIFECYCLE_SCOPE
    )


# ── the cross-run refcount (holder refs, via the shared transport) ───────────


def holder_prefix(resource_id: str) -> str:
    return f"refs/{REF_NAMESPACE}/{slug(resource_id, default='resource')}/holder"


def holder_ref(resource_id: str, run_id: int, attempt: int) -> str:
    return f"{holder_prefix(resource_id)}/{run_id}-{attempt}"


def _parse_holder_name(name: str) -> tuple[int | None, int]:
    run_part, sep, attempt_part = name.rpartition("-")
    if not sep or not run_part.isdigit() or not attempt_part.isdigit():
        return None, 0
    return int(run_part), int(attempt_part)


def create_holder(repo: str, resource_id: str, run_id: int, attempt: int) -> Outcome:
    """Register this run as a holder: write a record blob, then create the ref.

    Idempotent for a re-run of the SAME run+attempt (a 422 is this run's own
    prior holder). ``created`` or ``held``.
    """
    blob = write_blob(
        repo,
        json.dumps({"run_id": run_id, "attempt": attempt, "acquired_at": time.time()}),
    )
    if blob is None:
        raise DataforgeLifecycleError(
            f"could not write the dataforge-source holder record in {repo} "
            "(needs contents: write)"
        )
    outcome = try_create_ref(repo, holder_ref(resource_id, run_id, attempt), blob)
    if outcome == "acquired":
        return Outcome.CREATED
    if outcome == "occupied":
        return Outcome.HELD  # our own holder from a prior attempt of this run
    raise DataforgeLifecycleError(
        f"could not register the dataforge-source holder in {repo} "
        "(needs contents: write)"
    )


def list_holders(repo: str, resource_id: str) -> list[Holder]:
    """Every run registered as a holder. Raises ``RefListError`` if the listing
    itself failed (distinct from a genuinely empty listing) — the caller must
    NOT read a failure as 'no holders' and pause under a live run."""
    holders: list[Holder] = []
    for ref, sha in list_matching_refs(repo, holder_prefix(resource_id)):
        run_id, attempt = _parse_holder_name(ref.rsplit("/", 1)[-1])
        if run_id is None:
            continue
        record = read_blob_json(repo, sha) or {}
        acquired_at = record.get("acquired_at")
        holders.append(
            Holder(
                run_id, attempt, float(acquired_at) if acquired_at is not None else None
            )
        )
    return holders


# ── modes ─────────────────────────────────────────────────────────────────────


def wake(
    base_url: str,
    resource_id: str,
    repo: str,
    run_id: int,
    attempt: int,
    *,
    ready_timeout_seconds: int,
    poll_seconds: int,
    sleep=time.sleep,
) -> Outcome:
    """Register a holder, resume if PAUSED, poll to PROVISIONED.

    The holder ref is created BEFORE the resume/poll so a concurrent run's pause
    already sees us. One loop over the async state machine
    (PAUSED→RESUMING→PROVISIONED, driven by a Temporal workflow): resume is issued
    ONLY from PAUSED, at most once; every other non-ready state is waited out.
    """
    create_holder(repo, resource_id, run_id, attempt)
    token = service_token(base_url)

    deadline = time.monotonic() + ready_timeout_seconds
    resumed = False
    status = ""
    while True:
        status = resource_status(base_url, token, resource_id)
        if status == State.PROVISIONED:
            print(f"dataforge source is {State.PROVISIONED.value} — ready.")
            return Outcome.READY
        if status in TERMINAL_BAD_STATES:
            raise DataforgeLifecycleError(
                f"pinned dataforge resource is {status}; it cannot be woken. "
                "Re-provision the CI e2e instance."
            )
        if status == State.PAUSED and not resumed:
            try:
                resume_resource(base_url, token, resource_id)
                print(f"dataforge source was {State.PAUSED.value}; resume requested.")
            except DataforgeLifecycleError:
                # A concurrent run may have resumed it between our read and this
                # call (PAUSED→RESUMING), which the API rejects. If it is no
                # longer PAUSED that race is benign — keep polling; otherwise the
                # resume genuinely failed, so re-raise.
                if resource_status(base_url, token, resource_id) == State.PAUSED:
                    raise
                print("dataforge source was resumed by a concurrent run; polling.")
            resumed = True
        if time.monotonic() >= deadline:
            raise DataforgeLifecycleError(
                f"pinned dataforge resource did not reach {State.PROVISIONED.value} "
                f"within {ready_timeout_seconds}s (last status {status}). It may be stuck."
            )
        sleep(poll_seconds)


def pause_source(
    base_url: str,
    resource_id: str,
    repo: str,
    run_id: int,
    attempt: int,
    *,
    ttl_seconds: int,
    now: float | None = None,
) -> Outcome:
    """Deregister this run, then pause the source iff no other live run holds it.

    Runs under ``if: always()``. A holder-listing FAILURE errs towards leaving the
    source awake (the same "err live" call ``holder_is_live`` makes for an
    unreadable run) — pausing on a transient API failure is the one outcome this
    job exists to prevent.

    Holders are listed TWICE: once to decide, and again immediately before the
    pause, because the token exchange and state read in between leave a window a
    concurrent wake can arrive in. This is not a mutex — a run that registers
    after the second listing and before the pause lands can still be paused out
    from under. Closing that fully needs a CAS/lease on the resource itself; the
    re-check reduces the window from seconds of network to one API call, which is
    the right trade while one pin sees at most a handful of concurrent runs.
    """
    now = time.time() if now is None else now
    delete_ref(repo, holder_ref(resource_id, run_id, attempt))

    try:
        holders = list_holders(repo, resource_id)
    except RefListError as exc:
        print(f"::warning::{exc}; leaving the dataforge source awake.")
        return Outcome.KEPT_AWAKE

    live = []
    for h in holders:
        if h.is_me(run_id, attempt):
            continue
        if holder_is_live(repo, h, ttl_seconds=ttl_seconds, now=now):
            live.append(h)
        else:
            delete_ref(repo, holder_ref(resource_id, h.run_id, h.attempt))  # reap
    if live:
        ids = ", ".join(str(h.run_id) for h in live)
        print(
            f"{len(live)} other live run(s) still hold the dataforge source "
            f"(run(s) {ids}); leaving it awake."
        )
        return Outcome.KEPT_AWAKE

    token = service_token(base_url)
    status = resource_status(base_url, token, resource_id)
    if status != State.PROVISIONED:
        # pause is valid ONLY from PROVISIONED (the API requires it, and a pause
        # mid-RESUMING errors). PAUSED/PAUSING need no action; a transient
        # RESUMING/PROVISIONING is left to settle and be paused on a later pass.
        print(f"dataforge source is {status}; no pause needed.")
        return Outcome.ALREADY

    # Re-check immediately before the pause. The listing above is separated from
    # this call by a token exchange and a state read — seconds of network — and a
    # concurrent run's wake registers its holder BEFORE it resumes, so a holder
    # that appears in that window belongs to a run about to use the source. Wake
    # ordering its holder first is necessary but not sufficient on its own: it
    # closes the window only if the pauser looks again after it.
    #
    # Compared against the holders already weighed, not "any holder", so a reap
    # whose delete failed cannot wedge the pause off forever — only a NEWLY
    # arrived run defers it. A listing failure here errs the same way as above:
    # leave it awake, and let the next run's pause settle it.
    weighed = {(h.run_id, h.attempt) for h in holders}
    try:
        arrivals = [
            h
            for h in list_holders(repo, resource_id)
            if not h.is_me(run_id, attempt) and (h.run_id, h.attempt) not in weighed
        ]
    except RefListError as exc:
        print(f"::warning::{exc}; leaving the dataforge source awake.")
        return Outcome.KEPT_AWAKE
    if arrivals:
        ids = ", ".join(str(h.run_id) for h in arrivals)
        print(
            f"run(s) {ids} registered while this pause was deciding; "
            "leaving the dataforge source awake."
        )
        return Outcome.KEPT_AWAKE

    pause_resource(base_url, token, resource_id)
    print("no other holders remain; dataforge source pause requested.")
    return Outcome.PAUSED


# ── entry point ────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DataForge source wake/pause (FND-1992)"
    )
    parser.add_argument("--mode", choices=("wake", "pause"), required=True)
    parser.add_argument(
        "--resource-id", default=os.environ.get("DATAFORGE_RESOURCE_ID", "")
    )
    parser.add_argument("--repo", default=os.environ.get("LEASE_REPO", ""))
    parser.add_argument(
        "--run-id", type=int, default=int(os.environ.get("RUN_ID", "0") or "0")
    )
    parser.add_argument(
        "--run-attempt",
        type=int,
        default=int(os.environ.get("RUN_ATTEMPT", "1") or "1"),
    )
    parser.add_argument("--ready-timeout-seconds", type=int, default=600)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--ttl-seconds", type=int, default=16200)
    parser.add_argument(
        "--base-url", default=os.environ.get("DATAFORGE_BASE_URL", DEFAULT_BASE_URL)
    )
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    if not args.resource_id:
        # Nothing pinned — the workflow gates the STEP on this, so reaching here
        # is a no-op success (a managed-mode caller has no pin).
        print("no DATAFORGE_RESOURCE_ID pinned; nothing to wake/pause.")
        return 0
    if not args.repo or not args.run_id:
        print("::error::--repo and --run-id are required (LEASE_REPO / RUN_ID env).")
        return 1

    try:
        if args.mode == "wake":
            wake(
                base_url,
                args.resource_id,
                args.repo,
                args.run_id,
                args.run_attempt,
                ready_timeout_seconds=args.ready_timeout_seconds,
                poll_seconds=args.poll_seconds,
            )
        else:
            pause_source(
                base_url,
                args.resource_id,
                args.repo,
                args.run_id,
                args.run_attempt,
                ttl_seconds=args.ttl_seconds,
            )
    except (DataforgeLifecycleError, DataforgeSourceError) as exc:
        print(f"::error::{exc}")
        return 1
    except RateLimited:
        print(
            "::error::GitHub API rate-limited the dataforge-source refcount; try again."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
