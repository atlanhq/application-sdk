"""Tests for .github/actions/dataforge-source-lifecycle/dataforge_source_lifecycle.py.

Co-located module (checked out with the composite action in consumer repos); the
test lives here with the other action-script tests so the sub-minute pytest run
in scripts-tests.yaml covers it.

Two HTTP surfaces are stubbed separately, because the driver has two:
  * the GitHub refcount (holder refs, liveness) goes through the curl ``run()``
    seam — stubbed with the same FakeHTTP shape as test_e2e_tenant_lease.py, so
    422 "already exists" (a holder already registered) and 404 (a reaped run)
    are exercised through the real parser;
  * the DataForge lifecycle calls (state read, resume, pause) go through urllib
    — stubbed by monkeypatching the module functions, so a test drives the
    resume→RESUMING→PROVISIONED readiness sequence as a queue of statuses.

The point of the suite is the CROSS-RUN refcount: a run must not pause the shared
pinned instance while another live run still holds it, and must reap an abandoned
holder from a cancelled run.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).parent.parent.parent / "actions" / "dataforge-source-lifecycle"),
)

from dataforge_source_lifecycle import (  # noqa: E402
    LIFECYCLE_SCOPE,
    DataforgeLifecycleError,
    Holder,
    _parse_holder_name,
    create_holder,
    holder_is_live,
    holder_prefix,
    holder_ref,
    list_holders,
    main,
    pause_source,
    slug,
    wake,
)

REPO = "atlanhq/atlan-teradata-app"
RID = "ci-e2e-teradata-2n-uuid"


# --- fake GitHub HTTP client (curl seam) -----------------------------------


class FakeHTTP:
    """Records requests and replays queued curl-shaped responses, keyed by
    (method, path-fragment). Unmatched calls raise rather than answer 200."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[tuple]] = {}
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict] = []

    def route(self, method: str, contains: str, *responses: tuple) -> None:
        self.routes[(method, contains)] = list(responses)

    def __call__(self, cmd: list[str], **kwargs):
        method = cmd[cmd.index("-X") + 1]
        url = cmd[-1]
        self.calls.append((method, url))
        if "-d" in cmd:
            self.payloads.append(json.loads(cmd[cmd.index("-d") + 1]))
        for (route_method, contains), responses in self.routes.items():
            if route_method != method or contains not in url:
                continue
            entry = responses[0] if len(responses) == 1 else responses.pop(0)
            status, body = entry[0], entry[1]
            payload = "" if body is None else json.dumps(body)
            return _completed(f"HTTP/2 {status}\r\n\r\n{payload}")
        raise AssertionError(f"unstubbed request: {method} {url}")

    def count(self, method: str, contains: str) -> int:
        return len([c for c in self.calls if c[0] == method and contains in c[1]])


class _completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> FakeHTTP:
    monkeypatch.setenv("GH_TOKEN", "x")
    fake = FakeHTTP()
    monkeypatch.setattr("dataforge_source_lifecycle.run", fake)
    return fake


def _holder_blob(run_id: int, attempt: int = 1, acquired_at: float | None = 1000.0):
    record = {"run_id": run_id, "attempt": attempt}
    if acquired_at is not None:
        record["acquired_at"] = acquired_at
    return {"content": base64.b64encode(json.dumps(record).encode()).decode()}


def _matching_ref(run_id: int, attempt: int = 1, sha: str = "s"):
    return {
        "ref": f"{holder_prefix(RID)}/{run_id}-{attempt}",
        "object": {"sha": sha, "type": "blob"},
    }


# --- fake DataForge lifecycle (urllib functions) ---------------------------


@pytest.fixture
def df(monkeypatch: pytest.MonkeyPatch):
    """Stub the DataForge urllib calls. `statuses` is a queue the state read
    pops; `resumed`/`paused` record the mutating calls."""

    state = {"statuses": ["PAUSED"], "resumed": 0, "paused": 0}

    def _status(base, token, rid):
        q = state["statuses"]
        return q.pop(0) if len(q) > 1 else q[0]

    def _resume(base, token, rid):
        state["resumed"] += 1

    def _pause(base, token, rid):
        state["paused"] += 1

    monkeypatch.setattr(
        "dataforge_source_lifecycle._github_oidc_token", lambda **k: "oidc"
    )
    monkeypatch.setattr(
        "dataforge_source_lifecycle.exchange_for_service_token", lambda b, t: "svc"
    )
    monkeypatch.setattr("dataforge_source_lifecycle.resource_status", _status)
    monkeypatch.setattr("dataforge_source_lifecycle.resume_resource", _resume)
    monkeypatch.setattr("dataforge_source_lifecycle.pause_resource", _pause)
    return state


# --- pure helpers ----------------------------------------------------------


def test_scope_requests_both_lifecycle_and_read():
    # CI needs resource:read to poll and resource:lifecycle to wake — the auth
    # middleware gates the state GET on read and pause/resume on lifecycle.
    assert set(LIFECYCLE_SCOPE.split()) == {"resource:lifecycle", "resource:read"}


def test_slug_is_ref_safe():
    # non-[a-z0-9-_] each map to a single dash (so "/", " ", "." all become "-")
    assert slug("ci/e2e teradata.2n") == "ci-e2e-teradata-2n"
    assert slug("A_b") == "a_b"
    assert slug("") == "default"


def test_holder_ref_shape_and_parse():
    ref = holder_ref(RID, 42, 2)
    assert ref == f"{holder_prefix(RID)}/42-2"
    assert _parse_holder_name("42-2") == (42, 2)
    assert _parse_holder_name("not-a-run") == (None, 0)


# --- create_holder ---------------------------------------------------------


def test_create_holder_writes_blob_then_ref(http: FakeHTTP):
    http.route("POST", "/git/blobs", (201, {"sha": "abc"}))
    http.route("POST", "/git/refs", (201, {"ref": holder_ref(RID, 1, 1)}))
    assert create_holder(REPO, RID, 1, 1) == "created"
    # the ref points at the freshly written holder blob
    assert http.payloads[-1] == {"ref": holder_ref(RID, 1, 1), "sha": "abc"}


def test_create_holder_idempotent_on_own_rerun(http: FakeHTTP):
    http.route("POST", "/git/blobs", (201, {"sha": "abc"}))
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    assert create_holder(REPO, RID, 1, 1) == "held"


def test_create_holder_names_missing_write_permission(http: FakeHTTP):
    http.route("POST", "/git/blobs", (403, {"message": "Resource not accessible"}))
    with pytest.raises(DataforgeLifecycleError, match="contents: write"):
        create_holder(REPO, RID, 1, 1)


# --- list_holders + liveness ----------------------------------------------


def test_list_holders_parses_refs_and_records(http: FakeHTTP):
    http.route(
        "GET", "/git/matching-refs/", (200, [_matching_ref(7), _matching_ref(9)])
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(7)), (200, _holder_blob(9)))
    holders = list_holders(REPO, RID)
    assert {h.run_id for h in holders} == {7, 9}
    assert all(h.acquired_at == 1000.0 for h in holders)


def test_list_holders_empty_when_none(http: FakeHTTP):
    http.route("GET", "/git/matching-refs/", (404, {"message": "Not Found"}))
    assert list_holders(REPO, RID) == []


def test_holder_live_when_run_in_progress(http: FakeHTTP):
    http.route("GET", "/actions/runs/7", (200, {"status": "in_progress"}))
    h = Holder("r", 7, 1, 1000.0)
    assert holder_is_live(REPO, h, ttl_seconds=16200, now=1500.0) is True


def test_holder_dead_when_run_completed(http: FakeHTTP):
    http.route("GET", "/actions/runs/7", (200, {"status": "completed"}))
    h = Holder("r", 7, 1, 1000.0)
    assert holder_is_live(REPO, h, ttl_seconds=16200, now=1500.0) is False


def test_holder_dead_when_run_missing(http: FakeHTTP):
    http.route("GET", "/actions/runs/7", (404, {"message": "Not Found"}))
    h = Holder("r", 7, 1, 1000.0)
    assert holder_is_live(REPO, h, ttl_seconds=16200, now=1500.0) is False


def test_holder_reaped_past_ttl_even_if_running(http: FakeHTTP):
    http.route("GET", "/actions/runs/7", (200, {"status": "in_progress"}))
    h = Holder("r", 7, 1, 1000.0)
    # held for 20000s, past the 16200 TTL — a wedged run is broken.
    assert holder_is_live(REPO, h, ttl_seconds=16200, now=21000.0) is False


def test_holder_liveness_errs_live_on_api_failure(http: FakeHTTP):
    # A transient 500 must read as "still live" — pausing a source a live run is
    # crawling is the expensive direction.
    http.route("GET", "/actions/runs/7", (500, {"message": "server error"}))
    h = Holder("r", 7, 1, 1000.0)
    assert holder_is_live(REPO, h, ttl_seconds=16200, now=1500.0) is True


# --- wake ------------------------------------------------------------------


def _stub_holder_create(http: FakeHTTP) -> None:
    http.route("POST", "/git/blobs", (201, {"sha": "abc"}))
    http.route("POST", "/git/refs", (201, {"ref": holder_ref(RID, 1, 1)}))


def test_wake_resumes_a_paused_source_then_polls_ready(http: FakeHTTP, df):
    _stub_holder_create(http)
    df["statuses"] = ["PAUSED", "RESUMING", "PROVISIONED"]
    assert (
        wake(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )
        == "PROVISIONED"
    )
    assert df["resumed"] == 1  # resumed once
    # the holder ref was created BEFORE the resume/poll
    assert http.count("POST", "/git/refs") == 1


def test_wake_skips_resume_when_already_provisioned(http: FakeHTTP, df):
    _stub_holder_create(http)
    df["statuses"] = ["PROVISIONED"]
    assert (
        wake(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )
        == "PROVISIONED"
    )
    assert df["resumed"] == 0  # a concurrent run already woke it


def test_wake_fails_named_on_terminal_state(http: FakeHTTP, df):
    _stub_holder_create(http)
    df["statuses"] = ["FAILED"]
    with pytest.raises(DataforgeLifecycleError, match="cannot be woken"):
        wake(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )


def test_wake_times_out_with_named_error(http: FakeHTTP, df):
    _stub_holder_create(http)
    df["statuses"] = ["RESUMING"]  # never becomes ready
    with pytest.raises(DataforgeLifecycleError, match="did not reach PROVISIONED"):
        wake(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=0,
            poll_seconds=0,
            sleep=lambda _: None,
        )


def test_wake_waits_through_pausing_then_resumes_only_from_paused(http: FakeHTTP, df):
    # A pause in flight when this run wakes: the pin is PAUSING, settles to
    # PAUSED, and only THEN is a resume issued (never mid-PAUSING, which errors).
    _stub_holder_create(http)
    df["statuses"] = ["PAUSING", "PAUSED", "RESUMING", "PROVISIONED"]
    assert (
        wake(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )
        == "PROVISIONED"
    )
    assert df["resumed"] == 1  # resumed once, and only after PAUSING → PAUSED


def test_wake_tolerates_a_concurrent_run_resuming_it(http: FakeHTTP, df, monkeypatch):
    # We read PAUSED, but a concurrent run resumes it before our resume lands, so
    # the API rejects ours. Because it is no longer PAUSED on re-read, the race
    # is benign — we keep polling to PROVISIONED instead of failing.
    _stub_holder_create(http)
    df["statuses"] = ["PAUSED", "RESUMING", "PROVISIONED"]

    def _boom(base, token, rid):
        raise DataforgeLifecycleError(
            "dataforge POST .../resume failed: HTTP 409 (conflict)"
        )

    monkeypatch.setattr("dataforge_source_lifecycle.resume_resource", _boom)
    assert (
        wake(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )
        == "PROVISIONED"
    )


def test_wake_reraises_when_resume_fails_and_still_paused(
    http: FakeHTTP, df, monkeypatch
):
    # resume errors AND the pin is still PAUSED on re-read — a genuine failure
    # (e.g. missing resource:lifecycle scope), surfaced as a named error.
    _stub_holder_create(http)
    df["statuses"] = ["PAUSED"]  # stays PAUSED on the re-read

    def _boom(base, token, rid):
        raise DataforgeLifecycleError(
            "dataforge POST .../resume failed: HTTP 403 (forbidden)"
        )

    monkeypatch.setattr("dataforge_source_lifecycle.resume_resource", _boom)
    with pytest.raises(DataforgeLifecycleError, match="forbidden"):
        wake(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )


# --- pause (the refcount) --------------------------------------------------


def test_pause_pauses_when_no_other_holder(http: FakeHTTP, df):
    http.route("DELETE", "/git/refs/", (204, None))
    http.route("GET", "/git/matching-refs/", (200, [_matching_ref(1)]))  # only me
    http.route("GET", "/git/blobs/", (200, _holder_blob(1)))
    df["statuses"] = ["PROVISIONED"]
    assert (
        pause_source(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ttl_seconds=16200,
            now=2000.0,
        )
        == "paused"
    )
    assert df["paused"] == 1


def test_pause_kept_awake_when_other_run_live(http: FakeHTTP, df):
    http.route("DELETE", "/git/refs/", (204, None))
    http.route(
        "GET", "/git/matching-refs/", (200, [_matching_ref(1), _matching_ref(9)])
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(9)))
    http.route("GET", "/actions/runs/9", (200, {"status": "in_progress"}))
    assert (
        pause_source(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ttl_seconds=16200,
            now=2000.0,
        )
        == "kept-awake"
    )
    assert df["paused"] == 0  # a concurrent run still needs the source


def test_pause_reaps_abandoned_holder_then_pauses(http: FakeHTTP, df):
    http.route("DELETE", "/git/refs/", (204, None), (204, None))
    http.route(
        "GET", "/git/matching-refs/", (200, [_matching_ref(1), _matching_ref(9)])
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(9)))
    http.route(
        "GET", "/actions/runs/9", (200, {"status": "completed"})
    )  # cancelled/dead
    df["statuses"] = ["PROVISIONED"]
    assert (
        pause_source(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ttl_seconds=16200,
            now=2000.0,
        )
        == "paused"
    )
    assert df["paused"] == 1
    # deleted my ref + reaped the abandoned holder
    assert http.count("DELETE", "/git/refs/") == 2


def test_pause_noops_when_already_paused(http: FakeHTTP, df):
    http.route("DELETE", "/git/refs/", (204, None))
    http.route("GET", "/git/matching-refs/", (200, [_matching_ref(1)]))
    http.route("GET", "/git/blobs/", (200, _holder_blob(1)))
    df["statuses"] = ["PAUSED"]
    assert (
        pause_source(
            "https://api.dataforge.atlan.dev",
            RID,
            REPO,
            1,
            1,
            ttl_seconds=16200,
            now=2000.0,
        )
        == "already"
    )
    assert df["paused"] == 0


# --- main ------------------------------------------------------------------


def test_main_noop_success_without_pin(capsys):
    assert (
        main(["--mode", "wake", "--resource-id", "", "--repo", REPO, "--run-id", "1"])
        == 0
    )
    assert "nothing to wake" in capsys.readouterr().out


def test_main_requires_repo_and_run_id(capsys):
    assert (
        main(["--mode", "wake", "--resource-id", RID, "--repo", "", "--run-id", "0"])
        == 1
    )
