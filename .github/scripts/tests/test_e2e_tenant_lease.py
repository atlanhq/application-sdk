"""Tests for .github/actions/e2e-tenant-lease/e2e_tenant_lease.py.

Co-located module (checked out with the composite action in consumer repos); the
test lives here with the other action-script tests.

The HTTP client is stubbed at the ``run()`` seam with a fake that speaks real
curl-shaped responses, so status-code handling — 422 "already exists" (which IS
the lock), 403 "no permission", 500 transient — is exercised through the same
parser production uses rather than around it.

A note on what the first version of these tests got wrong, since it is the whole
reason this file is shaped the way it is. The original protocol was an ordered
ticket queue, and every test stubbed a ref listing in which the peer's ticket
already existed. That made the untested case — a contender arriving *after*
another had already acquired — the one that actually happened on the first live
run, where both runs acquired and both installed onto the same tenant. So the
tests below deliberately drive acquisition through the sequence of API answers a
second contender really sees, rather than through a tidy snapshot.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "actions" / "e2e-tenant-lease")
)

from e2e_tenant_lease import (  # noqa: E402
    Holder,
    acquire,
    create_identity_blob,
    holder_is_live,
    lease_ref,
    main,
    read_holder,
    release,
    release_ref,
    slug,
    try_acquire,
    write_outputs,
)

REPO = "atlanhq/atlan-example-app"
REF = "refs/e2e-tenant-lease/example/aws/holder"
BLOB = "b" * 40


# --- fake HTTP client ------------------------------------------------------


class FakeHTTP:
    """Records requests and replays queued curl-shaped responses.

    Keyed by (method, path-fragment) so a test only describes the calls it cares
    about; anything unmatched raises rather than silently answering 200, which is
    what keeps a test from passing because production stopped making a call it
    was supposed to make.

    A route with several responses pops one per call, so a test can express
    "occupied, then free" — the sequence, not just the snapshot.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[tuple[int, object]]] = {}
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict] = []

    def route(self, method: str, contains: str, *responses: tuple[int, object]) -> None:
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
            status, body = responses[0] if len(responses) == 1 else responses.pop(0)
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
    monkeypatch.setattr("e2e_tenant_lease.run", fake)
    return fake


def _holder_blob(run_id: int, attempt: int = 1, acquired_at: float | None = 1000.0):
    record = {"run_id": run_id, "attempt": attempt}
    if acquired_at is not None:
        record["acquired_at"] = acquired_at
    encoded = base64.b64encode(json.dumps(record).encode()).decode()
    return {"content": encoded, "encoding": "base64"}


def _ref_body(sha: str = BLOB):
    return {"ref": REF, "object": {"sha": sha, "type": "blob"}}


def _stub_blob_write(http: FakeHTTP) -> None:
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))


# --- keying ----------------------------------------------------------------


def test_slug_passes_through_a_plain_name() -> None:
    assert slug("openapi") == "openapi"


def test_slug_lowercases_and_replaces_unsafe_characters() -> None:
    assert slug("My App/Name") == "my-app-name"


def test_slug_maps_empty_to_default() -> None:
    # The single-tenant fallback leg spells cloud as a defined-but-empty string;
    # an empty ref component is a 422, not a lease.
    assert slug("") == "default"


def test_slug_strips_git_hostile_shapes() -> None:
    assert ".." not in slug("a..b")
    assert not slug("-lead").startswith("-")
    assert not slug("thing.lock").endswith(".lock")


def test_slug_of_only_unsafe_characters_falls_back() -> None:
    assert slug("///") == "default"


def test_lease_ref_is_fixed_for_a_tenant() -> None:
    """The name must carry NO per-run component. Collision between contenders is
    the signal the whole design runs on — a name only one run could guess is a
    name that can never collide, which is what the ticket-queue version got
    wrong."""
    assert lease_ref("openapi", "aws") == lease_ref("openapi", "aws")
    assert lease_ref("openapi", "aws") == "refs/e2e-tenant-lease/openapi/aws/holder"


def test_lease_ref_defaults_the_cloud() -> None:
    assert lease_ref("openapi", "") == "refs/e2e-tenant-lease/openapi/default/holder"


def test_different_clouds_of_one_app_do_not_share_a_lease() -> None:
    # Per-cloud tenants are independent resources; sharing one lease would
    # serialise legs that have no reason to wait for each other.
    assert lease_ref("openapi", "aws") != lease_ref("openapi", "gcp")


def test_different_apps_on_one_cloud_do_not_share_a_lease() -> None:
    # The installation is per app per tenant, so two apps do not contend.
    assert lease_ref("openapi", "aws") != lease_ref("mysql", "aws")


# --- the CAS primitive -----------------------------------------------------


def test_try_acquire_reports_acquired_on_201(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    assert try_acquire(REPO, REF, BLOB) == "acquired"


def test_try_acquire_reports_occupied_on_422(http: FakeHTTP) -> None:
    # This is the lock, not an error path: GitHub evaluates ref creation
    # atomically, so of N simultaneous callers exactly one sees 201.
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    assert try_acquire(REPO, REF, BLOB) == "occupied"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_try_acquire_reports_denied_without_ref_write(
    http: FakeHTTP, status: int
) -> None:
    http.route("POST", "/git/refs", (status, {"message": "Resource not accessible"}))
    assert try_acquire(REPO, REF, BLOB) == "denied"


def test_try_acquire_raises_on_an_unrelated_422(http: FakeHTTP) -> None:
    # A malformed ref or bad sha is a bug in the caller, not contention, and must
    # not be swallowed as "someone else holds the lease".
    http.route("POST", "/git/refs", (422, {"message": "Object does not exist"}))
    with pytest.raises(SystemExit):
        try_acquire(REPO, REF, BLOB)


def test_identity_blob_records_the_run_and_its_acquisition_time(
    http: FakeHTTP,
) -> None:
    # The waiter side depends on every one of these fields: run_id and attempt to
    # tell whose lease it is, acquired_at to judge how long it has been held.
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    assert create_identity_blob(REPO, 42, 2, 1234.0) == BLOB
    record = json.loads(http.payloads[0]["content"])
    assert record == {"run_id": 42, "attempt": 2, "acquired_at": 1234.0}


def test_the_lease_ref_points_at_the_identity_blob(http: FakeHTTP) -> None:
    # One atomic creation both takes the lease and records who took it; a
    # separate "who holds it" write would leave a window with an unidentifiable
    # holder.
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    try_acquire(REPO, REF, BLOB)
    assert http.payloads[0] == {"ref": REF, "sha": BLOB}


@pytest.mark.parametrize("status", [401, 403, 404])
def test_identity_blob_reports_denied(http: FakeHTTP, status: int) -> None:
    http.route("POST", "/git/blobs", (status, {"message": "nope"}))
    assert create_identity_blob(REPO, 1, 1, 0.0) is None


# --- reading the holder ----------------------------------------------------


def test_read_holder_returns_the_recorded_run(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(500, 1, 900.0)))
    assert read_holder(REPO, REF) == Holder(run_id=500, attempt=1, acquired_at=900.0)


def test_read_holder_returns_none_when_unheld(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    assert read_holder(REPO, REF) is None


def test_read_holder_returns_none_when_the_record_is_unreadable(
    http: FakeHTTP,
) -> None:
    # Conflating "unheld" with "cannot tell" is deliberate: both mean "retry the
    # CAS", and the CAS is the authority. Guessing at an unreadable holder is
    # what would be dangerous.
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (500, {"message": "boom"}))
    assert read_holder(REPO, REF) is None


def test_read_holder_tolerates_a_record_that_is_not_json(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route(
        "GET",
        "/git/blobs/",
        (200, {"content": base64.b64encode(b"not json").decode()}),
    )
    assert read_holder(REPO, REF) is None


def test_read_holder_tolerates_a_record_naming_no_run(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route(
        "GET",
        "/git/blobs/",
        (200, {"content": base64.b64encode(b'{"nope":1}').decode()}),
    )
    assert read_holder(REPO, REF) is None


def test_read_holder_tolerates_a_record_without_an_acquisition_time(
    http: FakeHTTP,
) -> None:
    # Then the TTL simply cannot judge, and run liveness is the only evidence.
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(7, 1, acquired_at=None)))
    assert read_holder(REPO, REF) == Holder(run_id=7, attempt=1, acquired_at=None)


# --- holder liveness -------------------------------------------------------


def _live(status: str = "in_progress", created_at: str | None = None):
    """A run object. `created_at` is what the TTL cross-checks the holder's own
    acquisition timestamp against, so TTL tests have to supply it."""
    body: dict[str, object] = {"status": status}
    if created_at is not None:
        body["created_at"] = created_at
    return (200, body)


#: A run that started at POSIX 900, i.e. just before the holders below acquire at
#: 1000. Spelled as the ISO string GitHub actually returns.
_RUN_STARTED_900 = (
    datetime.fromtimestamp(900, tz=timezone.utc).isoformat().replace("+00:00", "Z")
)


def test_holder_in_progress_is_live(http: FakeHTTP) -> None:
    http.route("GET", "/actions/runs/", _live())
    holder = Holder(1, 1, 0.0)
    assert holder_is_live(REPO, holder, ttl_seconds=0, now=0.0) is True


def test_holder_completed_is_dead(http: FakeHTTP) -> None:
    http.route("GET", "/actions/runs/", _live("completed"))
    holder = Holder(1, 1, 0.0)
    assert holder_is_live(REPO, holder, ttl_seconds=0, now=0.0) is False


def test_holder_deleted_run_is_dead(http: FakeHTTP) -> None:
    # Nothing will ever release this lease, so it has to be breakable.
    http.route("GET", "/actions/runs/", (404, {"message": "Not Found"}))
    holder = Holder(1, 1, 0.0)
    assert holder_is_live(REPO, holder, ttl_seconds=0, now=0.0) is False


@pytest.mark.parametrize("status", ["queued", "waiting", "requested", "pending"])
def test_holder_pre_start_statuses_are_live(http: FakeHTTP, status: str) -> None:
    # A queued run still holds its lease and will use it; treating it as dead
    # would let a later run install underneath it.
    http.route("GET", "/actions/runs/", _live(status))
    holder = Holder(1, 1, 0.0)
    assert holder_is_live(REPO, holder, ttl_seconds=0, now=0.0) is True


def test_holder_is_assumed_live_on_an_api_error(http: FakeHTTP) -> None:
    # Erring towards "dead" on a transient 500 would break a live holder's lease
    # and put a second installer on the tenant. Erring towards "live" costs one
    # poll interval.
    http.route("GET", "/actions/runs/", (500, {"message": "boom"}))
    holder = Holder(1, 1, 0.0)
    assert holder_is_live(REPO, holder, ttl_seconds=0, now=0.0) is True


def test_ttl_measures_hold_time_not_run_age(http: FakeHTTP) -> None:
    """The TTL is anchored to the acquisition time the holder recorded.

    A run reaches the lease job several jobs in and may have queued for runners
    first; none of that is lease-hold time. Anchoring on run age instead made the
    TTL fire on healthy holders, which is the expensive direction — it reaps a
    live mid-install holder.
    """
    http.route("GET", "/actions/runs/", _live(created_at=_RUN_STARTED_900))
    # Acquired at t=1000, now t=1100: held for 100s, well inside a 4h TTL, even
    # though the run itself may be hours old.
    holder = Holder(1, 1, acquired_at=1000.0)
    assert holder_is_live(REPO, holder, ttl_seconds=14400, now=1100.0) is True


def test_holder_past_the_hold_ttl_is_broken(http: FakeHTTP) -> None:
    http.route("GET", "/actions/runs/", _live(created_at=_RUN_STARTED_900))
    holder = Holder(1, 1, acquired_at=1000.0)
    assert holder_is_live(REPO, holder, ttl_seconds=60, now=2000.0) is False


def test_ttl_of_zero_never_breaks_a_live_holder(http: FakeHTTP) -> None:
    http.route("GET", "/actions/runs/", _live(created_at=_RUN_STARTED_900))
    holder = Holder(1, 1, acquired_at=1000.0)
    assert holder_is_live(REPO, holder, ttl_seconds=0, now=10**9) is True


def test_a_holder_without_an_acquisition_time_is_left_alone(http: FakeHTTP) -> None:
    http.route("GET", "/actions/runs/", _live(created_at=_RUN_STARTED_900))
    holder = Holder(1, 1, acquired_at=None)
    assert holder_is_live(REPO, holder, ttl_seconds=60, now=10**9) is True


def test_an_acquisition_time_predating_its_run_is_not_trusted(http: FakeHTTP) -> None:
    """A lease cannot have been acquired before the run that took it existed.

    Found by a reproducer while fixing something else: a stale or corrupt
    acquired_at makes the computed hold time enormous, so every waiter breaks a
    LIVE holder's lease on its first poll and installs underneath it. The
    timestamp is holder-written data, so it has to be checked, not believed.
    """
    http.route("GET", "/actions/runs/", _live(created_at=_RUN_STARTED_900))
    holder = Holder(1, 1, acquired_at=5.0)  # long before the run started
    assert holder_is_live(REPO, holder, ttl_seconds=60, now=10**9) is True


def test_a_run_without_a_start_time_disables_the_ttl(http: FakeHTTP) -> None:
    # Should never happen, so it means something changed underneath us; the run's
    # status is then the only evidence worth acting on.
    http.route("GET", "/actions/runs/", _live())
    holder = Holder(1, 1, acquired_at=1000.0)
    assert holder_is_live(REPO, holder, ttl_seconds=60, now=10**9) is True


def test_a_run_with_an_unparseable_start_time_disables_the_ttl(
    http: FakeHTTP,
) -> None:
    http.route("GET", "/actions/runs/", _live(created_at="whenever"))
    holder = Holder(1, 1, acquired_at=1000.0)
    assert holder_is_live(REPO, holder, ttl_seconds=60, now=10**9) is True


# --- acquire ---------------------------------------------------------------


def _acquire(**kwargs):
    defaults = dict(
        wait_seconds=60,
        poll_seconds=30,
        ttl_seconds=0,
        sleep=lambda _s: None,
        clock=lambda: 1000.0,
    )
    defaults.update(kwargs)
    return acquire(REPO, REF, 100, 1, **defaults)


def test_acquire_takes_an_unheld_lease(http: FakeHTTP) -> None:
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    assert _acquire() == ("acquired", None)


def test_a_late_arriver_does_not_steal_a_live_holders_lease(http: FakeHTTP) -> None:
    """THE regression test for the bug that shipped.

    Run B reached the lease job first and legitimately acquired. Run A arrived
    15s later with a LOWER run_id, and under the ordered-ticket protocol computed
    itself as the rightful holder and acquired too — both installed onto the same
    tenant. Exclusion must not depend on ids at all: whoever holds it, holds it.
    """
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    http.route("GET", "/git/ref/", (200, _ref_body()))
    # The holder's run_id is HIGHER than ours (100), which under the old rule
    # would have made us the winner.
    http.route("GET", "/git/blobs/", (200, _holder_blob(99999, 1, 990.0)))
    http.route("GET", "/actions/runs/", _live())

    state, blocker = _acquire(wait_seconds=30, poll_seconds=30)

    assert state == "timeout"
    assert blocker is not None and blocker.run_id == 99999
    # And critically: it never deleted the live holder's lease.
    assert http.count("DELETE", "/git/refs/") == 0


def test_acquire_waits_then_wins_when_the_holder_releases(http: FakeHTTP) -> None:
    _stub_blob_write(http)
    http.route(
        "POST",
        "/git/refs",
        (422, {"message": "Reference already exists"}),
        (201, {"ref": REF}),
    )
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(500)))
    http.route("GET", "/actions/runs/", _live())
    assert _acquire(wait_seconds=60, poll_seconds=30) == ("acquired", None)


def test_acquire_reaps_a_dead_holder_and_takes_the_lease(http: FakeHTTP) -> None:
    """The case `if: always()` cannot cover — a cancelled run whose release job
    never started. The next contender clears it in-band."""
    _stub_blob_write(http)
    http.route(
        "POST",
        "/git/refs",
        (422, {"message": "Reference already exists"}),
        (201, {"ref": REF}),
    )
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(500)))
    http.route("GET", "/actions/runs/", _live("completed"))
    http.route("DELETE", "/git/refs/", (204, None))

    assert _acquire() == ("acquired", None)
    assert http.count("DELETE", "/git/refs/") == 1


def test_reaping_retries_the_cas_without_sleeping(http: FakeHTTP) -> None:
    # The tenant is free the instant the dead lease is deleted; sleeping a full
    # poll interval first would hand it to whoever wakes up sooner.
    _stub_blob_write(http)
    http.route(
        "POST",
        "/git/refs",
        (422, {"message": "Reference already exists"}),
        (201, {"ref": REF}),
    )
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(500)))
    http.route("GET", "/actions/runs/", _live("completed"))
    http.route("DELETE", "/git/refs/", (204, None))

    slept: list[int] = []
    assert _acquire(sleep=slept.append) == ("acquired", None)
    assert slept == []


def test_acquire_treats_its_own_existing_lease_as_held(http: FakeHTTP) -> None:
    # A retried step, or a re-run of the same attempt. Must not deadlock against
    # itself.
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    assert _acquire() == ("acquired", None)


def test_acquire_retries_when_the_holder_is_unreadable(http: FakeHTTP) -> None:
    # Released between the CAS and the read, or unreadable — either way the next
    # CAS is authoritative rather than a guess.
    _stub_blob_write(http)
    http.route(
        "POST",
        "/git/refs",
        (422, {"message": "Reference already exists"}),
        (201, {"ref": REF}),
    )
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    assert _acquire() == ("acquired", None)


def test_acquire_disables_itself_without_blob_write(http: FakeHTTP) -> None:
    # Fail-open: the lease reduces contention, it is not what makes a
    # wrong-version run detectable (each leg re-asserts the version, FND-31).
    http.route("POST", "/git/blobs", (403, {"message": "Resource not accessible"}))
    assert _acquire() == ("disabled", None)


def test_acquire_disables_itself_without_ref_write(http: FakeHTTP) -> None:
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (403, {"message": "Resource not accessible"}))
    assert _acquire() == ("disabled", None)


def test_acquire_respects_the_wait_budget(http: FakeHTTP) -> None:
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(500)))
    http.route("GET", "/actions/runs/", _live())
    slept: list[int] = []
    _acquire(wait_seconds=90, poll_seconds=30, sleep=slept.append)
    # 3 attempts, so 2 sleeps: the last attempt is not followed by a wait.
    assert slept == [30, 30]


def test_acquire_always_makes_one_attempt_on_a_zero_budget(http: FakeHTTP) -> None:
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    assert _acquire(wait_seconds=0, poll_seconds=30) == ("acquired", None)


# --- release ---------------------------------------------------------------


def test_release_deletes_our_own_lease(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    http.route("DELETE", "/git/refs/", (204, None))
    assert release(REPO, REF, 100, 1) is True


def test_release_refuses_to_delete_someone_elses_lease(http: FakeHTTP) -> None:
    """The ref name is shared by every contender, so unlike a per-run name it is
    possible to delete the wrong lease. Deleting a live holder's lease would put
    two installers on the tenant — the failure this protocol replaced."""
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(777, 1)))
    assert release(REPO, REF, 100, 1) is False
    assert http.count("DELETE", "/git/refs/") == 0


def test_release_distinguishes_attempts_of_the_same_run(http: FakeHTTP) -> None:
    # A re-run is a different holder; attempt 1 must not release attempt 2's lease.
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 2)))
    assert release(REPO, REF, 100, 1) is False


def test_release_is_quiet_when_the_lease_is_already_gone(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    assert release(REPO, REF, 100, 1) is False


def test_release_ref_tolerates_an_already_deleted_ref(http: FakeHTTP) -> None:
    http.route("DELETE", "/git/refs/", (422, {"message": "Reference does not exist"}))
    assert release_ref(REPO, REF) is False


# --- outputs ---------------------------------------------------------------


def test_write_outputs_appends_key_value_pairs(tmp_path: Path) -> None:
    target = tmp_path / "out"
    write_outputs({"acquired": "true", "state": "acquired"}, str(target))
    assert target.read_text() == "acquired=true\nstate=acquired\n"


def test_write_outputs_is_a_noop_without_a_path() -> None:
    write_outputs({"acquired": "true"}, None)


# --- CLI -------------------------------------------------------------------


def _argv(mode: str, *extra: str) -> list[str]:
    return [
        "--mode",
        mode,
        "--repo",
        REPO,
        "--app",
        "example",
        "--cloud",
        "aws",
        "--run-id",
        "100",
        "--run-attempt",
        "1",
        *extra,
    ]


def test_main_acquire_exits_zero_and_reports_outputs(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    assert main(_argv("acquire")) == 0
    written = out.read_text()
    assert "acquired=true" in written
    assert "state=acquired" in written
    assert f"lease-ref={REF}" in written


def test_main_acquire_fails_loudly_on_timeout(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(555)))
    http.route("GET", "/actions/runs/", _live())
    assert main(_argv("acquire", "--wait-seconds", "1", "--poll-seconds", "1")) == 1
    printed = capsys.readouterr().out
    # The holder has to be named: "the tenant is busy" is only actionable if the
    # reader can see which run to wait for.
    assert "::error::" in printed
    assert "555" in printed
    assert "Nothing is wrong with this change" in printed
    written = out.read_text()
    assert "acquired=false" in written
    assert "holder-run-id=555" in written


def test_main_acquire_can_warn_instead_of_failing(
    http: FakeHTTP, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(555)))
    http.route("GET", "/actions/runs/", _live())
    argv = _argv(
        "acquire", "--wait-seconds", "1", "--poll-seconds", "1", "--on-timeout", "warn"
    )
    assert main(argv) == 0
    assert "::warning::" in capsys.readouterr().out


def test_main_acquire_exits_zero_when_the_lease_is_disabled(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("POST", "/git/blobs", (403, {"message": "nope"}))
    assert main(_argv("acquire")) == 0


def test_main_release_exits_zero(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    http.route("DELETE", "/git/refs/", (204, None))
    assert main(_argv("release")) == 0


def test_main_release_exits_zero_even_when_nothing_was_held(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed release must never redden a run whose tests passed; the reaper
    # covers it.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    assert main(_argv("release")) == 0


def test_acquire_and_release_agree_on_the_lease_ref(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two modes run in different jobs and share no state — if they ever
    disagreed about the ref, every run would leak its lease."""
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    main(_argv("acquire"))
    acquired_ref = [
        line.split("=", 1)[1]
        for line in out.read_text().splitlines()
        if line.startswith("lease-ref=")
    ][0]

    http.calls.clear()
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    http.route("DELETE", "/git/refs/", (204, None))
    main(_argv("release"))
    deleted = [c for c in http.calls if c[0] == "DELETE"][0][1]
    assert deleted.endswith(acquired_ref.removeprefix("refs/"))


def test_missing_token_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="GH_TOKEN"):
        try_acquire(REPO, REF, BLOB)


# --- input bounds ----------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1"])
def test_main_rejects_a_poll_interval_below_one(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    with pytest.raises(SystemExit, match="poll-seconds"):
        main(_argv("acquire", "--poll-seconds", value))


def test_main_rejects_a_negative_wait_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    with pytest.raises(SystemExit, match="wait-seconds"):
        main(_argv("acquire", "--wait-seconds", "-1"))


def test_main_rejects_a_negative_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    with pytest.raises(SystemExit, match="ttl-seconds"):
        main(_argv("acquire", "--ttl-seconds", "-1"))


def test_main_validates_before_touching_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no token set, reaching the API would raise about GH_TOKEN instead — so
    # this also pins that validation happens before any request.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="poll-seconds"):
        main(_argv("acquire", "--poll-seconds", "0"))
