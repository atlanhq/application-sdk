"""Wake (resume) a paused DataForge e2e source before a run, pause it after.

Why this exists (FND-1992)
--------------------------
A DataForge-backed connector pins ONE provisioned instance as its e2e source
(``DATAFORGE_RESOURCE_ID``, ``category: "ci"`` so the lifecycle reaper skips it).
To avoid paying for an always-on instance, that pin is left PAUSED between runs —
but a paused instance fails every e2e/integration leg at source resolution, and
CI had no way to wake it. This driver is the wake/pause half:

  * ``--mode wake``  — resume the pinned instance and poll until it is
    PROVISIONED (credentials resolving is not the same as the instance
    accepting connections, so readiness is a state poll, not just a 2xx).
  * ``--mode pause`` — pause it again after the run, under ``if: always()`` in
    the workflow so a cancelled or failed run never leaks a running instance.

This is the LIFECYCLE path, distinct from ``fetch_dataforge_source.py`` (which
is read-only and only reads credentials). It mutates instance state, so it
exchanges for a token carrying ``resource:lifecycle resource:read`` rather than
``credentials:read`` — a strictly wider, admin-granted privilege. The repo's
DataForge workload binding must allow those scopes or the exchange yields a
token that 403s on resume/pause.

Concurrency (the hard half)
---------------------------
One pinned instance is shared by every leg of a run (integration + the aws/azure/
gcp e2e legs) AND by concurrent runs on different PRs. The first leg to finish
must NOT pause it out from under the others. Legs WITHIN a run are ordered by the
workflow DAG (wake runs before any leg, pause after all of them), so this driver
only has to solve the CROSS-RUN case, and it does it as a refcount modelled on
the ``e2e-tenant-lease`` mechanism:

  * wake registers a per-run HOLDER ref
    ``refs/dataforge-source/<resource>/holder/<run_id>-<attempt>`` (GitHub
    evaluates ref creation atomically — same primitive the tenant lease locks
    on) and resumes the instance only if it is not already awake;
  * pause deletes THIS run's holder ref, then pauses the instance only when no
    OTHER live holder remains. A holder is "live" iff its run is still in
    progress (GitHub Actions API) and within the TTL backstop — so a cancelled
    run's abandoned holder is reaped by whoever pauses next, exactly like an
    abandoned tenant lease (FND-250).

Unlike the tenant lease this is a SHARED refcount, not a mutex: many runs may
hold at once; the instance is woken on the first arrival and paused on the last
departure.

Security / logging
------------------
DataForge tokens arrive via env, never argv. Error bodies are never echoed — a
fixed, allowlisted failure class is parsed from a known-safe field and anything
unrecognised collapses to ``request_failed`` (mirrors fetch_dataforge_source.py:
a DataForge error body carries no guarantee it can never embed a credential).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.dataforge.atlan.dev"

# One ref per (resource, run). The <resource> component is slugged; the holder
# component carries the run identity so a run can find and delete its OWN ref
# and read every other run's to refcount. Namespace is distinct from the tenant
# lease's so the two never collide.
REF_NAMESPACE = "dataforge-source"

# The settled, ready-to-use state (entity.ResStatusProvisioned). PROVISIONED is
# DataForge's strong guarantee that credentials resolve AND compute is up.
READY_STATUS = "PROVISIONED"

# The only state a resume may be issued from. Validated live (FND-1992): the
# resume/pause path is an async Temporal workflow, and the API rejects a resume
# issued mid-PAUSING; while RESUMING one is already in flight. So wake kicks a
# resume ONLY from PAUSED and waits every other non-ready state out — it never
# issues a resume from PAUSING/RESUMING. Pause is symmetrically valid only from
# PROVISIONED.
PAUSED_STATUS = "PAUSED"

# States a wake can never recover from — fail fast with a named class rather
# than polling to the readiness timeout.
TERMINAL_BAD_STATES = frozenset({"DENIED", "FAILED", "DECOMMISSIONED", "DELETED"})

# The lifecycle scopes CI needs: resource:read to poll the state read, and
# resource:lifecycle to resume/pause. Space-separated per RFC 6749. The server
# grants the intersection with the repo's workload-binding AllowedScopes, so
# this is a REQUEST — the binding must actually allow both.
LIFECYCLE_SCOPE = "resource:lifecycle resource:read"

_COMPLETED = "completed"

# Allowlisted DataForge failure classes (see fetch_dataforge_source.py). Anything
# else — including a body field that could carry a credential value — collapses
# to "request_failed" so it can never reach the log.
_KNOWN_ERROR_CLASSES = frozenset(
    {
        "resource_not_found",
        "resource_paused",
        "not_provisioned",
        "no_credential_source",
        "forbidden",
        "unauthorized",
        "bad_request",
        "not_found",
        "conflict",
        "rate_limited",
        "lifecycle_conflict",
    }
)

# Transport retry budget for the GitHub API (mirrors e2e_tenant_lease.py).
_TRANSPORT_ATTEMPTS = 3
_TRANSPORT_BACKOFF_SECONDS = 2


class DataforgeLifecycleError(RuntimeError):
    """A wake/pause step could not complete. Message is fixed and body-free."""


# ── DataForge HTTP (urllib; the seam tests monkeypatch) ──────────────────────


def _df_request(method: str, url: str, token: str, *, timeout: int = 30) -> dict:
    """One DataForge API call. Returns the parsed JSON body (``{}`` if empty).

    Raises ``DataforgeLifecycleError`` on any HTTP error, carrying only the
    status and an allowlisted failure class parsed from a known-safe field —
    never the body.
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
            f"dataforge {method} {url.split('?')[0]} failed: "
            f"HTTP {exc.code} ({_error_class(exc)})"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        # URLError/timeout/OSError = unreachable (VPN down, DNS, refused);
        # JSONDecodeError = an unexpected response shape. Fixed, body-free.
        raise DataforgeLifecycleError(
            f"dataforge {method} {url.split('?')[0]} failed: {type(exc).__name__} "
            "— is the VPN up?"
        ) from exc


def _error_class(exc: urllib.error.HTTPError) -> str:
    """Extract a safe, allowlisted failure class from an HTTP error body."""
    try:
        parsed = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:
        return "request_failed"
    candidate = ""
    if isinstance(parsed, dict):
        candidate = str(parsed.get("error") or parsed.get("code") or "").strip().lower()
    return candidate if candidate in _KNOWN_ERROR_CLASSES else "request_failed"


# ── GitHub OIDC exchange (lifecycle scope) ───────────────────────────────────


def _github_oidc_token(audience: str = "dataforge") -> str:
    """Fetch this run's OIDC token from the runner's token service."""
    req_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if not req_url or not req_token:
        raise DataforgeLifecycleError(
            "GitHub OIDC is unavailable — the job needs `permissions: "
            "id-token: write`. This action is CI-only."
        )
    sep = "&" if "?" in req_url else "?"
    req = urllib.request.Request(
        f"{req_url}{sep}audience={urllib.parse.quote(audience)}",
        headers={"Authorization": f"Bearer {req_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["value"]
    except urllib.error.HTTPError as exc:
        raise DataforgeLifecycleError(
            f"GitHub OIDC token request failed: HTTP {exc.code}"
        ) from exc
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        raise DataforgeLifecycleError(
            f"GitHub OIDC token request failed: {type(exc).__name__}"
        ) from exc


def _oauth_base_urls(base_url: str) -> list[str]:
    """Candidate hosts for /oauth/token, tried in order (see fetch script)."""
    override = os.environ.get("DATAFORGE_OAUTH_BASE_URL", "").strip()
    if override:
        return [override.rstrip("/")]
    candidates = [base_url]
    app_host = base_url.replace("://api.", "://", 1)
    if app_host != base_url:
        candidates.append(app_host)
    return candidates


def exchange_for_service_token(base_url: str, oidc_token: str) -> str:
    """RFC 8693 exchange: runner OIDC token -> 1h SERVICE token (lifecycle scope)."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": oidc_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "scope": LIFECYCLE_SCOPE,
        }
    ).encode()
    last_exc: urllib.error.HTTPError | None = None
    for oauth_base in _oauth_base_urls(base_url):
        req = urllib.request.Request(
            f"{oauth_base}/oauth/token",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)["access_token"]
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 404:  # this host doesn't route /oauth — try the next
                continue
            break
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise DataforgeLifecycleError(
                f"dataforge token exchange failed: {type(exc).__name__} — is the "
                "VPN up and a workload binding (with resource:lifecycle + "
                "resource:read) registered for this repo?"
            ) from exc
    assert last_exc is not None
    raise DataforgeLifecycleError(
        f"dataforge token exchange failed: HTTP {last_exc.code} — does this repo's "
        "workload binding grant resource:lifecycle + resource:read?"
    ) from last_exc


# ── DataForge lifecycle calls ────────────────────────────────────────────────


def resource_status(base_url: str, token: str, resource_id: str) -> str:
    """GET /api/v1/resources/{id} -> the resource's status string.

    resource:read gates this read; it is the readiness poll's one call.
    """
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
    """POST /api/v1/resources/{id}/resume (resource:lifecycle). Idempotent."""
    url = f"{base_url}/api/v1/resources/{urllib.parse.quote(resource_id)}/resume"
    _df_request("POST", url, token)


def pause_resource(base_url: str, token: str, resource_id: str) -> None:
    """POST /api/v1/resources/{id}/pause (resource:lifecycle). Idempotent."""
    url = f"{base_url}/api/v1/resources/{urllib.parse.quote(resource_id)}/pause"
    _df_request("POST", url, token)


# ── GitHub refs: the cross-run refcount (modelled on e2e-tenant-lease) ───────


_SAFE_SLUG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def slug(value: str, *, default: str = "default") -> str:
    """Reduce a free-text key to one safe git ref path component."""
    cleaned = "".join(
        c if c in _SAFE_SLUG_CHARS else "-" for c in value.strip().lower()
    )
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "-")
    cleaned = cleaned.strip("-._")
    while cleaned.endswith(".lock"):
        cleaned = cleaned[: -len(".lock")].strip("-._")
    return cleaned or default


def holder_prefix(resource_id: str) -> str:
    """The ref prefix under which every run's holder for one resource lives."""
    return f"refs/{REF_NAMESPACE}/{slug(resource_id, default='resource')}/holder"


def holder_ref(resource_id: str, run_id: int, attempt: int) -> str:
    """This run's own holder ref for one resource."""
    return f"{holder_prefix(resource_id)}/{run_id}-{attempt}"


@dataclass(frozen=True)
class Holder:
    """A run holding the source awake, read back from a holder ref + its blob."""

    ref: str
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


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single subprocess seam so tests can stub the GitHub HTTP client."""
    return subprocess.run(cmd, **kwargs)


def _parse_http(raw: str, label: str) -> Response:
    text = raw.replace("\r\n", "\n")
    if "\n\n" not in text:
        raise DataforgeLifecycleError(f"unexpected response for {label}")
    header_block, _, body = text.partition("\n\n")
    lines = header_block.splitlines()
    try:
        status_code = int(lines[0].split()[1])
    except (IndexError, ValueError):
        raise DataforgeLifecycleError(f"could not parse HTTP status line for {label}")
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
        return Response(status_code, headers, None)


def gh_request(method: str, path: str, payload: dict | None = None) -> Response:
    """Call the GitHub API via curl, returning the status rather than raising.

    curl (not ``gh api``) because the non-2xx codes ARE the mechanism: a 422 on
    ref creation is a holder already registered, distinct from a 403 permission
    denial. Token is fed on stdin (-K -) so it never appears in argv.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise DataforgeLifecycleError("GH_TOKEN (or GITHUB_TOKEN) must be set")
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
    for attempt in range(1, _TRANSPORT_ATTEMPTS + 1):
        result = run(cmd, input=config, capture_output=True, text=True, check=False)
        last = attempt == _TRANSPORT_ATTEMPTS
        if result.returncode != 0:
            if last:
                raise DataforgeLifecycleError(
                    f"curl failed for {label} after {_TRANSPORT_ATTEMPTS} attempts"
                )
        else:
            response = _parse_http(result.stdout, label)
            if response.status < 500 or last:
                return response
        time.sleep(_TRANSPORT_BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise DataforgeLifecycleError(f"exhausted transport attempts for {label}")


def create_holder(repo: str, resource_id: str, run_id: int, attempt: int) -> str:
    """Register this run as a holder: write a record blob, then create the ref.

    Idempotent for a re-run of the SAME run+attempt (a 422 "already exists" is
    this run's own prior holder — treat as held). Returns "created" or "held".
    """
    blob = gh_request(
        "POST",
        f"repos/{repo}/git/blobs",
        {
            "content": json.dumps(
                {"run_id": run_id, "attempt": attempt, "acquired_at": time.time()}
            ),
            "encoding": "utf-8",
        },
    )
    if not (
        blob.status in (200, 201)
        and isinstance(blob.body, dict)
        and blob.body.get("sha")
    ):
        raise DataforgeLifecycleError(
            f"could not write the dataforge-source holder record in {repo}: "
            f"HTTP {blob.status} {blob.message!r} (needs contents: write)"
        )
    ref = holder_ref(resource_id, run_id, attempt)
    created = gh_request(
        "POST", f"repos/{repo}/git/refs", {"ref": ref, "sha": str(blob.body["sha"])}
    )
    if created.status in (200, 201):
        return "created"
    if created.status == 422 and "already exists" in created.message.lower():
        return "held"  # our own holder from a prior attempt of this run
    raise DataforgeLifecycleError(
        f"could not register the dataforge-source holder {ref} in {repo}: "
        f"HTTP {created.status} {created.message!r} (needs contents: write)"
    )


def delete_ref(repo: str, ref: str) -> bool:
    """Delete a ref. False ⇒ already gone, which is not a fault."""
    resp = gh_request("DELETE", f"repos/{repo}/git/refs/{ref.removeprefix('refs/')}")
    return resp.status in (200, 204)


def list_holders(repo: str, resource_id: str) -> list[Holder]:
    """Every run currently registered as a holder of this resource."""
    prefix = holder_prefix(resource_id)
    resp = gh_request(
        "GET", f"repos/{repo}/git/matching-refs/{prefix.removeprefix('refs/')}"
    )
    if resp.status == 404 or not isinstance(resp.body, list):
        return []
    if resp.status >= 400:
        print(
            f"::warning::could not list dataforge-source holders (HTTP {resp.status})."
        )
        return []
    holders: list[Holder] = []
    for item in resp.body:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "")
        obj = item.get("object") or {}
        sha = obj.get("sha") if isinstance(obj, dict) else None
        name = ref.rsplit("/", 1)[-1] if ref else ""
        run_id, attempt = _parse_holder_name(name)
        if run_id is None:
            continue
        holders.append(
            Holder(ref, run_id, attempt, _acquired_at(repo, str(sha)) if sha else None)
        )
    return holders


def _parse_holder_name(name: str) -> tuple[int | None, int]:
    """Split a "<run_id>-<attempt>" holder ref leaf into ints."""
    run_part, sep, attempt_part = name.rpartition("-")
    if not sep or not run_part.isdigit() or not attempt_part.isdigit():
        return None, 0
    return int(run_part), int(attempt_part)


def _acquired_at(repo: str, blob_sha: str) -> float | None:
    resp = gh_request("GET", f"repos/{repo}/git/blobs/{blob_sha}")
    if resp.status >= 400 or not isinstance(resp.body, dict):
        return None
    content = resp.body.get("content")
    if not isinstance(content, str):
        return None
    try:
        record = json.loads(base64.b64decode(content).decode("utf-8"))
        value = record.get("acquired_at")
        return float(value) if value is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def holder_is_live(repo: str, holder: Holder, *, ttl_seconds: int, now: float) -> bool:
    """Is this holder's run still legitimately holding the source awake?

    Errs towards True (mirrors the tenant lease): reading a transient API
    failure as "dead" would pause a source a live run is still crawling, whereas
    erring live only leaves it awake one extra pause-job cycle. A completed run,
    a deleted run, or a hold past the TTL backstop is reaped.
    """
    resp = gh_request("GET", f"repos/{repo}/actions/runs/{holder.run_id}")
    if resp.status == 404:
        return False  # run gone — nothing will ever release this holder
    if resp.status >= 400 or not isinstance(resp.body, dict):
        print(
            f"::warning::could not read run {holder.run_id} (HTTP {resp.status}); "
            "treating its dataforge-source holder as still live."
        )
        return True
    if resp.body.get("status") == _COMPLETED:
        return False
    if ttl_seconds <= 0 or holder.acquired_at is None:
        return True
    if now - holder.acquired_at > ttl_seconds:
        print(
            f"::warning::run {holder.run_id} has held the dataforge source for "
            f">{ttl_seconds}s and still reports '{resp.body.get('status')}' — "
            "reaping its holder. If it is genuinely still working, raise ttl-seconds."
        )
        return False
    return True


# ── Modes ─────────────────────────────────────────────────────────────────────


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
) -> str:
    """Register this run as a holder, resume the source if paused, poll to ready.

    The holder ref is created BEFORE the resume/poll so a concurrent run's pause
    already sees us and cannot pause the source out from under this run.
    """
    create_holder(repo, resource_id, run_id, attempt)
    token = exchange_for_service_token(base_url, _github_oidc_token())

    # One poll loop over the async state machine (PAUSED→RESUMING→PROVISIONED,
    # driven by a Temporal workflow). A resume is issued ONLY from PAUSED, and at
    # most once: the API rejects a resume mid-PAUSING, and while RESUMING one is
    # already in flight. Every other non-ready state (PAUSING, RESUMING,
    # PROVISIONING…) is simply waited out — a PAUSING pin settles to PAUSED and
    # is resumed on a later pass.
    deadline = time.monotonic() + ready_timeout_seconds
    resumed = False
    status = ""
    while True:
        status = resource_status(base_url, token, resource_id)
        if status == READY_STATUS:
            print(f"dataforge source is {READY_STATUS} — ready.")
            return READY_STATUS
        if status in TERMINAL_BAD_STATES:
            raise DataforgeLifecycleError(
                f"pinned dataforge resource is {status}; it cannot be woken. "
                "Re-provision the CI e2e instance."
            )
        if status == PAUSED_STATUS and not resumed:
            try:
                resume_resource(base_url, token, resource_id)
                print(f"dataforge source was {PAUSED_STATUS}; resume requested.")
            except DataforgeLifecycleError:
                # A concurrent run may have resumed it between our read and this
                # call (PAUSED→RESUMING), which the API rejects. If it is no
                # longer PAUSED that race is benign — keep polling; otherwise the
                # resume genuinely failed, so re-raise.
                if resource_status(base_url, token, resource_id) == PAUSED_STATUS:
                    raise
                print("dataforge source was resumed by a concurrent run; polling.")
            resumed = True
        if time.monotonic() >= deadline:
            raise DataforgeLifecycleError(
                f"pinned dataforge resource did not reach {READY_STATUS} within "
                f"{ready_timeout_seconds}s (last status {status}). It may be stuck."
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
) -> str:
    """Deregister this run, then pause the source iff no other live run holds it.

    Runs under ``if: always()`` so a failed/cancelled run still gives the source
    back. Returns "paused", "kept-awake" (a live holder remains) or "already".
    """
    now = time.time() if now is None else now
    delete_ref(repo, holder_ref(resource_id, run_id, attempt))

    survivors = [
        h for h in list_holders(repo, resource_id) if not h.is_me(run_id, attempt)
    ]
    live = []
    for h in survivors:
        if holder_is_live(repo, h, ttl_seconds=ttl_seconds, now=now):
            live.append(h)
        else:
            delete_ref(repo, h.ref)  # reap an abandoned holder (FND-250 pattern)
    if live:
        ids = ", ".join(str(h.run_id) for h in live)
        print(
            f"{len(live)} other live run(s) still hold the dataforge source "
            f"(run(s) {ids}); leaving it awake."
        )
        return "kept-awake"

    token = exchange_for_service_token(base_url, _github_oidc_token())
    status = resource_status(base_url, token, resource_id)
    if status != READY_STATUS:
        # pause is valid ONLY from PROVISIONED (the API requires it, and a pause
        # issued mid-RESUMING errors). PAUSED/PAUSING need no action; a transient
        # RESUMING/PROVISIONING is left to settle and be paused on a later pass.
        print(f"dataforge source is {status}; no pause needed.")
        return "already"
    pause_resource(base_url, token, resource_id)
    print("no other holders remain; dataforge source pause requested.")
    return "paused"


# ── Entry point ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
        # is a no-op success, not a failure (a managed-mode caller has no pin).
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
    except DataforgeLifecycleError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
