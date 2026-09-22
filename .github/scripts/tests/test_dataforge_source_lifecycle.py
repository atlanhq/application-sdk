"""Tests for .github/scripts/dataforge_source_lifecycle.py.

Two seams are stubbed:
  * the GitHub-ref transport (`_gh_refs.run`, the curl seam) — a FakeHTTP that
    replays curl-shaped responses, so 422 "already exists", 404, and the non-2xx
    listing failures are exercised through the real parser;
  * the DataForge lifecycle calls (state read, resume, pause) + the OIDC exchange
    — monkeypatched module functions, so a test drives the
    resume→RESUMING→PROVISIONED readiness sequence as a queue of statuses.

The point of the suite is the CROSS-RUN refcount and its FAIL-SAFE direction: a
run must not pause the shared pin while another live run holds it, must reap a
cancelled run's abandoned holder, and — the addition after Chris's review — must
leave the source AWAKE when the holder listing itself fails, never read that as
"no holders" and pause.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import dataforge_source_lifecycle as dfl  # noqa: E402
from _gh_refs import RefListError  # noqa: E402
from dataforge_source_lifecycle import (  # noqa: E402
    DataforgeLifecycleError,
    Outcome,
    _parse_holder_name,
    create_holder,
    holder_ref,
    list_holders,
    pause_source,
    slug,
    wake,
)

REPO = "atlanhq/atlan-documentdb-app"
RID = "cratedb-ci-instance-uuid"
BASE = "https://api.dataforge.atlan.dev"


# --- fake GitHub HTTP client (the _gh_refs.run seam) -----------------------


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
            headers = entry[2] if len(entry) > 2 else {}
            header_lines = "".join(f"\r\n{k}: {v}" for k, v in headers.items())
            payload = "" if body is None else json.dumps(body)
            return _completed(f"HTTP/2 {status}{header_lines}\r\n\r\n{payload}")
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
    monkeypatch.setattr("_gh_refs.run", fake)
    return fake


def _holder_blob(run_id: int, attempt: int = 1, acquired_at: float | None = 1000.0):
    record = {"run_id": run_id, "attempt": attempt}
    if acquired_at is not None:
        record["acquired_at"] = acquired_at
    return {"content": base64.b64encode(json.dumps(record).encode()).decode()}


def _matching_ref(run_id: int, attempt: int = 1, sha: str = "s"):
    return {
        "ref": f"{dfl.holder_prefix(RID)}/{run_id}-{attempt}",
        "object": {"sha": sha, "type": "blob"},
    }


# --- fake DataForge lifecycle (urllib functions + OIDC exchange) -----------


@pytest.fixture
def df(monkeypatch: pytest.MonkeyPatch):
    """Stub the DataForge calls. `statuses` is a queue the state read pops;
    `resumed`/`paused` record the mutating calls."""

    state = {"statuses": ["PAUSED"], "resumed": 0, "paused": 0}

    def _status(base, token, rid):
        q = state["statuses"]
        return q.pop(0) if len(q) > 1 else q[0]

    monkeypatch.setattr(dfl, "service_token", lambda base: "svc")
    monkeypatch.setattr(dfl, "resource_status", _status)
    monkeypatch.setattr(
        dfl,
        "resume_resource",
        lambda b, t, r: state.__setitem__("resumed", state["resumed"] + 1),
    )
    monkeypatch.setattr(
        dfl,
        "pause_resource",
        lambda b, t, r: state.__setitem__("paused", state["paused"] + 1),
    )
    return state


# --- pure helpers ----------------------------------------------------------


def test_scope_requests_both_lifecycle_and_read():
    assert set(dfl.LIFECYCLE_SCOPE.split()) == {"resource:lifecycle", "resource:read"}


def test_slug_and_holder_ref():
    assert slug("ci/e2e cratedb.2n") == "ci-e2e-cratedb-2n"
    assert holder_ref(RID, 42, 2) == f"{dfl.holder_prefix(RID)}/42-2"
    assert _parse_holder_name("42-2") == (42, 2)
    assert _parse_holder_name("not-a-run") == (None, 0)


# --- create_holder ---------------------------------------------------------


def _stub_holder_create(http: FakeHTTP) -> None:
    http.route("POST", "/git/blobs", (201, {"sha": "abc"}))
    http.route("POST", "/git/refs", (201, {"ref": holder_ref(RID, 1, 1)}))


def test_create_holder_writes_blob_then_ref(http: FakeHTTP):
    _stub_holder_create(http)
    assert create_holder(REPO, RID, 1, 1) == Outcome.CREATED
    assert http.payloads[-1] == {"ref": holder_ref(RID, 1, 1), "sha": "abc"}


def test_create_holder_idempotent_on_own_rerun(http: FakeHTTP):
    http.route("POST", "/git/blobs", (201, {"sha": "abc"}))
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    assert create_holder(REPO, RID, 1, 1) == Outcome.HELD


def test_create_holder_names_missing_write_permission(http: FakeHTTP):
    http.route("POST", "/git/blobs", (403, {"message": "Resource not accessible"}))
    with pytest.raises(DataforgeLifecycleError, match="contents: write"):
        create_holder(REPO, RID, 1, 1)


# --- list_holders + the fail-safe (Chris review #2 / Copilot #1) -----------


def test_list_holders_parses_refs_and_records(http: FakeHTTP):
    http.route(
        "GET", "/git/matching-refs/", (200, [_matching_ref(7), _matching_ref(9)])
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(7)), (200, _holder_blob(9)))
    holders = list_holders(REPO, RID)
    assert {h.run_id for h in holders} == {7, 9}
    assert all(h.acquired_at == 1000.0 for h in holders)


def test_list_holders_empty_on_404(http: FakeHTTP):
    http.route("GET", "/git/matching-refs/", (404, {"message": "Not Found"}))
    assert list_holders(REPO, RID) == []


@pytest.mark.parametrize("status", [403, 429, 500])
def test_list_holders_raises_on_api_failure_not_empty(http: FakeHTTP, status: int):
    # The failure this job exists to prevent: a non-404 listing error must NOT
    # read as "no holders". It raises so pause_source can leave the source awake.
    http.route("GET", "/git/matching-refs/", (status, {"message": "boom"}))
    with pytest.raises(RefListError):
        list_holders(REPO, RID)


# --- pause (the refcount) --------------------------------------------------


def test_pause_pauses_when_no_other_holder(http: FakeHTTP, df):
    http.route("DELETE", "/git/refs/", (204, None))
    http.route("GET", "/git/matching-refs/", (200, [_matching_ref(1)]))
    http.route("GET", "/git/blobs/", (200, _holder_blob(1)))
    df["statuses"] = ["PROVISIONED"]
    assert (
        pause_source(BASE, RID, REPO, 1, 1, ttl_seconds=16200, now=2000.0)
        == Outcome.PAUSED
    )
    assert df["paused"] == 1


def test_pause_kept_awake_when_other_run_live(http: FakeHTTP, df):
    http.route("DELETE", "/git/refs/", (204, None))
    http.route(
        "GET", "/git/matching-refs/", (200, [_matching_ref(1), _matching_ref(9)])
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(1)), (200, _holder_blob(9)))
    http.route("GET", "/actions/runs/9", (200, {"status": "in_progress"}))
    assert (
        pause_source(BASE, RID, REPO, 1, 1, ttl_seconds=16200, now=2000.0)
        == Outcome.KEPT_AWAKE
    )
    assert df["paused"] == 0


def test_pause_reaps_abandoned_holder_then_pauses(http: FakeHTTP, df):
    http.route("DELETE", "/git/refs/", (204, None), (204, None))
    http.route(
        "GET", "/git/matching-refs/", (200, [_matching_ref(1), _matching_ref(9)])
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(1)), (200, _holder_blob(9)))
    http.route("GET", "/actions/runs/9", (200, {"status": "completed"}))  # dead
    df["statuses"] = ["PROVISIONED"]
    assert (
        pause_source(BASE, RID, REPO, 1, 1, ttl_seconds=16200, now=2000.0)
        == Outcome.PAUSED
    )
    assert df["paused"] == 1
    assert http.count("DELETE", "/git/refs/") == 2  # my ref + the reaped one


def test_pause_kept_awake_when_listing_fails(http: FakeHTTP, df):
    # Chris #2 / Copilot #2: a listing failure must leave the source awake, never
    # pause under a live run. No pause call is made.
    http.route("DELETE", "/git/refs/", (204, None))
    http.route("GET", "/git/matching-refs/", (500, {"message": "server error"}))
    assert (
        pause_source(BASE, RID, REPO, 1, 1, ttl_seconds=16200, now=2000.0)
        == Outcome.KEPT_AWAKE
    )
    assert df["paused"] == 0


def test_pause_noops_when_already_paused(http: FakeHTTP, df):
    http.route("DELETE", "/git/refs/", (204, None))
    http.route("GET", "/git/matching-refs/", (200, [_matching_ref(1)]))
    http.route("GET", "/git/blobs/", (200, _holder_blob(1)))
    df["statuses"] = ["PAUSED"]
    assert (
        pause_source(BASE, RID, REPO, 1, 1, ttl_seconds=16200, now=2000.0)
        == Outcome.ALREADY
    )
    assert df["paused"] == 0


# --- wake ------------------------------------------------------------------


def test_wake_resumes_a_paused_source_then_polls_ready(http: FakeHTTP, df):
    _stub_holder_create(http)
    df["statuses"] = ["PAUSED", "RESUMING", "PROVISIONED"]
    assert (
        wake(
            BASE,
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )
        == Outcome.READY
    )
    assert df["resumed"] == 1
    assert http.count("POST", "/git/refs") == 1  # holder created before resume


def test_wake_skips_resume_when_already_provisioned(http: FakeHTTP, df):
    _stub_holder_create(http)
    df["statuses"] = ["PROVISIONED"]
    assert (
        wake(
            BASE,
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )
        == Outcome.READY
    )
    assert df["resumed"] == 0


def test_wake_waits_through_pausing_then_resumes_only_from_paused(http: FakeHTTP, df):
    _stub_holder_create(http)
    df["statuses"] = ["PAUSING", "PAUSED", "RESUMING", "PROVISIONED"]
    assert (
        wake(
            BASE,
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )
        == Outcome.READY
    )
    assert df["resumed"] == 1  # only after PAUSING settled to PAUSED


def test_wake_tolerates_concurrent_resume(http: FakeHTTP, df, monkeypatch):
    _stub_holder_create(http)
    df["statuses"] = ["PAUSED", "RESUMING", "PROVISIONED"]
    monkeypatch.setattr(
        dfl,
        "resume_resource",
        lambda b, t, r: (_ for _ in ()).throw(
            DataforgeLifecycleError(
                "dataforge POST .../resume failed: HTTP 409 (conflict)"
            )
        ),
    )
    assert (
        wake(
            BASE,
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )
        == Outcome.READY
    )


def test_wake_reraises_when_resume_fails_and_still_paused(
    http: FakeHTTP, df, monkeypatch
):
    _stub_holder_create(http)
    df["statuses"] = ["PAUSED"]  # stays PAUSED on re-read
    monkeypatch.setattr(
        dfl,
        "resume_resource",
        lambda b, t, r: (_ for _ in ()).throw(
            DataforgeLifecycleError(
                "dataforge POST .../resume failed: HTTP 403 (forbidden)"
            )
        ),
    )
    with pytest.raises(DataforgeLifecycleError, match="forbidden"):
        wake(
            BASE,
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=600,
            poll_seconds=1,
            sleep=lambda _: None,
        )


def test_wake_fails_named_on_terminal_state(http: FakeHTTP, df):
    _stub_holder_create(http)
    df["statuses"] = ["FAILED"]
    with pytest.raises(DataforgeLifecycleError, match="cannot be woken"):
        wake(
            BASE,
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
    df["statuses"] = ["RESUMING"]
    with pytest.raises(DataforgeLifecycleError, match="did not reach PROVISIONED"):
        wake(
            BASE,
            RID,
            REPO,
            1,
            1,
            ready_timeout_seconds=0,
            poll_seconds=0,
            sleep=lambda _: None,
        )


# --- main ------------------------------------------------------------------


def test_main_noop_success_without_pin(capsys):
    assert (
        dfl.main(
            ["--mode", "wake", "--resource-id", "", "--repo", REPO, "--run-id", "1"]
        )
        == 0
    )
    assert "nothing to wake" in capsys.readouterr().out


def test_main_requires_repo_and_run_id():
    assert (
        dfl.main(
            ["--mode", "wake", "--resource-id", RID, "--repo", "", "--run-id", "0"]
        )
        == 1
    )
