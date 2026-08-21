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
import select
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "actions" / "e2e-tenant-lease")
)

from e2e_tenant_lease import (  # noqa: E402
    Holder,
    RateLimited,
    _denied,
    _rate_limited,
    acquire,
    acquire_ordered,
    create_identity_blob,
    gh_request,
    holder_is_live,
    lease_ref,
    main,
    read_holder,
    release,
    release_all,
    release_ref,
    slug,
    try_acquire,
    verify_held,
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
        # Full argv per call, so the credential-exposure canary can assert the
        # token never reaches curl's command line.
        self.cmds: list[list[str]] = []
        self.inputs: list[str | None] = []

    def route(self, method: str, contains: str, *responses: tuple[int, object]) -> None:
        self.routes[(method, contains)] = list(responses)

    def __call__(self, cmd: list[str], **kwargs):
        method = cmd[cmd.index("-X") + 1]
        url = cmd[-1]
        self.calls.append((method, url))
        self.cmds.append(list(cmd))
        self.inputs.append(kwargs.get("input"))
        if "-d" in cmd:
            self.payloads.append(json.loads(cmd[cmd.index("-d") + 1]))
        for (route_method, contains), responses in self.routes.items():
            if route_method != method or contains not in url:
                continue
            entry = responses[0] if len(responses) == 1 else responses.pop(0)
            status, body = entry[0], entry[1]
            headers = entry[2] if len(entry) > 2 else {}
            payload = "" if body is None else json.dumps(body)
            header_lines = "".join(f"\r\n{k}: {v}" for k, v in headers.items())
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


def _stub_unheld(http: FakeHTTP, *later: tuple) -> None:
    """The lease ref does not exist. `acquire` reads the ref BEFORE minting an
    identity blob, so the free path starts with a 404 here — that pre-read is
    what makes a waiting pass two calls instead of five."""
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}), *later)


#: A rate-limit answer. GitHub spells this as a 403 with the remaining-quota
#: header at zero, which is indistinguishable from a permission 403 by status
#: alone — the conflation that made the lease disable itself under contention.
_RATE_LIMIT = (
    403,
    {"message": "API rate limit exceeded for installation"},
    {"x-ratelimit-remaining": "0"},
)
_SECONDARY_LIMIT = (
    403,
    {"message": "You have exceeded a secondary rate limit"},
    {"retry-after": "45"},
)
_PERMISSION_DENIED = (403, {"message": "Resource not accessible by integration"})


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


def test_identity_blob_raises_rather_than_denying_on_a_rate_limit(
    http: FakeHTTP,
) -> None:
    # Signalled as an exception so no call site can silently treat it as a
    # permission denial and fail the lease open.
    http.route("POST", "/git/blobs", _RATE_LIMIT)
    with pytest.raises(RateLimited):
        create_identity_blob(REPO, 1, 1, 0.0)


def test_try_acquire_raises_rather_than_denying_on_a_rate_limit(
    http: FakeHTTP,
) -> None:
    http.route("POST", "/git/refs", _SECONDARY_LIMIT)
    with pytest.raises(RateLimited) as raised:
        try_acquire(REPO, REF, BLOB)
    assert raised.value.retry_after == 45


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_RATE_LIMIT, True),
        (_SECONDARY_LIMIT, True),
        ((429, {"message": "Too Many Requests"}), True),
        (_PERMISSION_DENIED, False),
        ((404, {"message": "Not Found"}), False),
        (
            (
                403,
                {"message": "Resource not accessible"},
                {"x-ratelimit-remaining": "42"},
            ),
            False,
        ),
    ],
)
def test_rate_limit_detection(http: FakeHTTP, response: tuple, expected: bool) -> None:
    http.route("GET", "/probe", response)
    assert _rate_limited(gh_request("GET", "probe")) is expected


def test_a_403_with_quota_remaining_is_a_permission_denial(http: FakeHTTP) -> None:
    # The discriminator has to be the quota, not the status: a permission 403
    # arrives with plenty of budget left and must still disable the lease.
    http.route(
        "GET",
        "/probe",
        (403, {"message": "Resource not accessible"}, {"x-ratelimit-remaining": "988"}),
    )
    response = gh_request("GET", "probe")
    assert _rate_limited(response) is False
    assert _denied(response) is True


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
    _stub_unheld(http)
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
    http.route("GET", "/git/ref/", (200, _ref_body()))
    # The holder's run_id is HIGHER than ours (100), which under the old rule
    # would have made us the winner.
    http.route("GET", "/git/blobs/", (200, _holder_blob(99999, 1, 990.0)))
    http.route("GET", "/actions/runs/", _live())

    state, blocker = _acquire(wait_seconds=30, poll_seconds=30)

    assert state == "timeout"
    assert blocker is not None and blocker.run_id == 99999
    # And critically: it never deleted the live holder's lease, and never even
    # attempted the CAS — the ref was held, so there was nothing to race for.
    assert http.count("DELETE", "/git/refs/") == 0
    assert http.count("POST", "/git/refs") == 0


def test_a_waiting_pass_costs_two_api_calls(http: FakeHTTP) -> None:
    """The GITHUB_TOKEN budget is 1000/hour per REPOSITORY and every matrix leg
    shares it, so a waiting poll has to be cheap. Reading the ref tells us the
    lease is held; the run-status call tells us whether to reap it. The holder
    record is memoised by the ref's target sha, so it is not re-read while the
    lease has not changed hands."""
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(500)))
    http.route("GET", "/actions/runs/", _live())

    _acquire(wait_seconds=90, poll_seconds=30)

    # 3 passes: 3 ref reads + 3 run-status reads + exactly ONE record read.
    assert http.count("GET", "/git/ref/") == 3
    assert http.count("GET", "/actions/runs/") == 3
    assert http.count("GET", "/git/blobs/") == 1
    # And nothing was written at all while the lease was held.
    assert http.count("POST", "/git/blobs") == 0
    assert http.count("POST", "/git/refs") == 0


def test_the_holder_record_is_re_read_when_the_lease_changes_hands(
    http: FakeHTTP,
) -> None:
    # The memo is keyed on the target sha precisely so a new holder is noticed.
    http.route(
        "GET",
        "/git/ref/",
        (200, _ref_body("aaa")),
        (200, _ref_body("bbb")),
        (200, _ref_body("bbb")),
    )
    http.route(
        "GET",
        "/git/blobs/",
        (200, _holder_blob(500)),
        (200, _holder_blob(600)),
    )
    http.route("GET", "/actions/runs/", _live())

    _, blocker = _acquire(wait_seconds=90, poll_seconds=30)
    assert http.count("GET", "/git/blobs/") == 2
    assert blocker is not None and blocker.run_id == 600


def test_acquire_waits_then_wins_when_the_holder_releases(http: FakeHTTP) -> None:
    http.route(
        "GET",
        "/git/ref/",
        (200, _ref_body()),
        (404, {"message": "Not Found"}),
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(500)))
    http.route("GET", "/actions/runs/", _live())
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    assert _acquire(wait_seconds=60, poll_seconds=30) == ("acquired", None)


def test_acquire_loses_a_race_after_the_ref_looked_free(http: FakeHTTP) -> None:
    """The pre-read is an optimisation, not a decision. Another contender can win
    between our read and our CAS, and the 422 has to be handled as normal
    contention rather than trusted from the earlier read."""
    _stub_unheld(http, (404, {"message": "Not Found"}))
    _stub_blob_write(http)
    http.route(
        "POST",
        "/git/refs",
        (422, {"message": "Reference already exists"}),
        (201, {"ref": REF}),
    )
    assert _acquire(wait_seconds=60, poll_seconds=30) == ("acquired", None)


def test_acquire_reaps_a_dead_holder_and_takes_the_lease(http: FakeHTTP) -> None:
    """The case `if: always()` cannot cover — a cancelled run whose release job
    never started. The next contender clears it in-band."""
    http.route(
        "GET",
        "/git/ref/",
        (200, _ref_body()),
        (404, {"message": "Not Found"}),
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(500)))
    http.route("GET", "/actions/runs/", _live("completed"))
    http.route("DELETE", "/git/refs/", (204, None))
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": REF}))

    assert _acquire() == ("acquired", None)
    assert http.count("DELETE", "/git/refs/") == 1


def test_reaping_retries_the_cas_without_sleeping(http: FakeHTTP) -> None:
    # The tenant is free the instant the dead lease is deleted; sleeping a full
    # poll interval first would hand it to whoever wakes up sooner.
    http.route(
        "GET",
        "/git/ref/",
        (200, _ref_body()),
        (404, {"message": "Not Found"}),
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(500)))
    http.route("GET", "/actions/runs/", _live("completed"))
    http.route("DELETE", "/git/refs/", (204, None))
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": REF}))

    slept: list[int] = []
    assert _acquire(sleep=slept.append) == ("acquired", None)
    assert slept == []


def test_acquire_treats_its_own_existing_lease_as_held(http: FakeHTTP) -> None:
    # A retried step, or a re-run of the same attempt. Must not deadlock against
    # itself.
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    assert _acquire() == ("acquired", None)


def test_acquire_retries_when_the_holder_is_unreadable(http: FakeHTTP) -> None:
    # Unreadable record: the CAS on a later pass is authoritative rather than a
    # guess, so it waits rather than assuming either way.
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (500, {"message": "boom"}))
    state, blocker = _acquire(wait_seconds=30, poll_seconds=30)
    assert state == "timeout"
    assert blocker is None
    assert http.count("DELETE", "/git/refs/") == 0


def test_acquire_is_denied_without_blob_write(http: FakeHTTP) -> None:
    # NOT fail-open (FND-702). The predecessor returned "disabled" and exited 0
    # on the theory that the run proceeded unserialised; it never did — the
    # install's own verify step reds every Prepare tenant leg two jobs later.
    _stub_unheld(http)
    http.route("POST", "/git/blobs", _PERMISSION_DENIED)
    assert _acquire() == ("denied", None)


def test_acquire_is_denied_without_ref_write(http: FakeHTTP) -> None:
    _stub_unheld(http)
    _stub_blob_write(http)
    http.route("POST", "/git/refs", _PERMISSION_DENIED)
    assert _acquire() == ("denied", None)


@pytest.mark.parametrize("refused", ["/git/blobs", "/git/refs"])
def test_a_denial_does_not_keep_retrying(http: FakeHTTP, refused: str) -> None:
    # A denial is a fact about the token, not about timing, so it must answer on
    # the first pass rather than burning the whole wait budget re-learning it.
    _stub_unheld(http)
    _stub_blob_write(http)
    http.route("POST", refused, _PERMISSION_DENIED)
    slept: list[int] = []
    state, _ = _acquire(wait_seconds=300, poll_seconds=1, sleep=slept.append)
    assert state == "denied"
    assert slept == []


def test_acquire_respects_the_wait_budget(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(500)))
    http.route("GET", "/actions/runs/", _live())
    slept: list[int] = []
    _acquire(wait_seconds=90, poll_seconds=30, sleep=slept.append)
    # 3 attempts, so 2 sleeps: the last attempt is not followed by a wait.
    assert slept == [30, 30]


def test_acquire_always_makes_one_attempt_on_a_zero_budget(http: FakeHTTP) -> None:
    _stub_unheld(http)
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    assert _acquire(wait_seconds=0, poll_seconds=30) == ("acquired", None)


# --- rate limiting must never fail the lease open --------------------------


def test_a_rate_limit_is_not_a_permission_denial(http: FakeHTTP) -> None:
    """Both are 403. Conflating them switched the lease OFF exactly when it was
    needed: several legs queueing for tenants is also when the shared
    per-repository budget runs out, and the fail-open then let every waiting run
    proceed onto the tenant unserialised."""
    _stub_unheld(http)
    http.route("POST", "/git/blobs", _RATE_LIMIT)
    state, _ = _acquire(wait_seconds=30, poll_seconds=30)
    assert state == "rate-limited"


def test_a_rate_limited_cas_does_not_disable_the_lease(http: FakeHTTP) -> None:
    _stub_unheld(http)
    _stub_blob_write(http)
    http.route("POST", "/git/refs", _RATE_LIMIT)
    state, _ = _acquire(wait_seconds=30, poll_seconds=30)
    assert state == "rate-limited"


def test_a_secondary_rate_limit_is_recognised_from_retry_after(
    http: FakeHTTP,
) -> None:
    # Secondary limits do not always carry x-ratelimit-remaining, so the header
    # and the message are both checked rather than relying on GitHub's prose.
    _stub_unheld(http)
    http.route("POST", "/git/blobs", _SECONDARY_LIMIT)
    state, _ = _acquire(wait_seconds=30, poll_seconds=30)
    assert state == "rate-limited"


def test_a_rate_limit_backoff_honours_retry_after(http: FakeHTTP) -> None:
    _stub_unheld(http)
    http.route("POST", "/git/blobs", _SECONDARY_LIMIT)
    slept: list[int] = []
    _acquire(wait_seconds=90, poll_seconds=30, sleep=slept.append)
    # Retry-After of 45 beats the 30s poll interval, and is not applied after the
    # final attempt.
    assert slept == [45, 45]


def test_a_rate_limit_backoff_never_undercuts_the_poll_interval(
    http: FakeHTTP,
) -> None:
    _stub_unheld(http)
    http.route(
        "POST",
        "/git/blobs",
        (403, {"message": "rate limit"}, {"retry-after": "1"}),
    )
    slept: list[int] = []
    _acquire(wait_seconds=60, poll_seconds=30, sleep=slept.append)
    assert slept == [30]


def test_a_rate_limit_backoff_is_capped(http: FakeHTTP) -> None:
    # A very long Retry-After must not swallow the whole wait budget in one sleep.
    _stub_unheld(http)
    http.route(
        "POST",
        "/git/blobs",
        (403, {"message": "rate limit"}, {"retry-after": "99999"}),
    )
    slept: list[int] = []
    _acquire(wait_seconds=60, poll_seconds=30, sleep=slept.append)
    assert slept == [300]


def test_a_rate_limit_that_clears_still_acquires(http: FakeHTTP) -> None:
    _stub_unheld(http, (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", _RATE_LIMIT, (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    assert _acquire(wait_seconds=60, poll_seconds=30) == ("acquired", None)


def test_a_429_is_treated_as_a_rate_limit(http: FakeHTTP) -> None:
    _stub_unheld(http)
    http.route("POST", "/git/blobs", (429, {"message": "Too Many Requests"}))
    state, _ = _acquire(wait_seconds=30, poll_seconds=30)
    assert state == "rate-limited"


# --- transient transport failures ------------------------------------------


def _curl_failed(stderr: str = "curl: (60) SSL certificate problem"):
    class R:
        stdout = ""
        returncode = 60

    R.stderr = stderr
    return R()


def test_a_transient_transport_failure_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observed live: `curl: (60) SSL certificate problem` on one runner failed a
    lease job outright, and through the matrix aggregate that skipped the install
    on every OTHER cloud whose lease had been taken. One blip must not cost a run
    its tenants."""
    monkeypatch.setenv("GH_TOKEN", "x")
    attempts: list[int] = []

    def flaky(cmd, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return _curl_failed()
        return _completed('HTTP/2 201\r\n\r\n{"sha": "abc"}')

    monkeypatch.setattr("e2e_tenant_lease.run", flaky)
    response = gh_request("POST", "probe", {"x": 1}, sleep=lambda _s: None)
    assert response.status == 201
    assert len(attempts) == 2


def test_a_persistent_transport_failure_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Retries absorb a blip, not an outage — and a lease that cannot be reached
    # must not be quietly treated as free.
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr("e2e_tenant_lease.run", lambda cmd, **kw: _curl_failed())
    with pytest.raises(SystemExit, match="after 3 attempts"):
        gh_request("GET", "probe", sleep=lambda _s: None)


def test_a_5xx_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    answers = [
        _completed("HTTP/2 502\r\n\r\n"),
        _completed('HTTP/2 200\r\n\r\n{"ok": true}'),
    ]
    monkeypatch.setattr("e2e_tenant_lease.run", lambda cmd, **kw: answers.pop(0))
    assert gh_request("GET", "probe", sleep=lambda _s: None).status == 200


def test_a_persistent_5xx_is_returned_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The callers already treat an unreadable answer conservatively (assume the
    # lease is held), which is safer than aborting the job.
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr(
        "e2e_tenant_lease.run", lambda cmd, **kw: _completed("HTTP/2 503\r\n\r\n")
    )
    assert gh_request("GET", "probe", sleep=lambda _s: None).status == 503


def test_a_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # 422 is the lock being held — retrying it would turn the primitive into a
    # busy-wait and mask contention.
    monkeypatch.setenv("GH_TOKEN", "x")
    attempts: list[int] = []

    def counting(cmd, **kwargs):
        attempts.append(1)
        return _completed('HTTP/2 422\r\n\r\n{"message": "Reference already exists"}')

    monkeypatch.setattr("e2e_tenant_lease.run", counting)
    assert gh_request("POST", "probe", {"x": 1}, sleep=lambda _s: None).status == 422
    assert len(attempts) == 1


def test_transport_backoff_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr("e2e_tenant_lease.run", lambda cmd, **kw: _curl_failed())
    slept: list[int] = []
    with pytest.raises(SystemExit):
        gh_request("GET", "probe", sleep=slept.append)
    assert slept == [2, 4]


# --- verify (per-cloud ownership, not the matrix aggregate) -----------------


def test_verify_passes_when_this_run_holds_the_lease(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    assert verify_held(REPO, REF, 100, 1) is True


def test_verify_fails_when_another_run_holds_the_lease(http: FakeHTTP) -> None:
    """The case the matrix aggregate cannot express.

    `needs.lease-tenant.result` is the aggregate across clouds, so a gate on it
    says "some cloud's lease succeeded", never "mine did". Observed live: one
    cloud's acquire failed on a transient TLS error while two others succeeded, and
    the aggregate then skipped the install for all three. Each leg has to confirm
    its own tenant.
    """
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(777, 1)))
    assert verify_held(REPO, REF, 100, 1) is False


def test_verify_fails_when_the_lease_is_unheld(http: FakeHTTP) -> None:
    # Installing against a tenant nobody has leased is the race the lease exists
    # to close, so an absent lease is a failure and not a licence to proceed.
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    assert verify_held(REPO, REF, 100, 1) is False


def test_verify_distinguishes_attempts(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 2)))
    assert verify_held(REPO, REF, 100, 1) is False


def test_main_verify_exits_non_zero_when_not_held(
    http: FakeHTTP, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(777, 1)))
    assert main(_argv("verify")) == 1
    printed = capsys.readouterr().out
    assert "::error::" in printed
    assert "777" in printed


def test_main_verify_exits_zero_when_held(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    assert main(_argv("verify")) == 0


def test_verify_needs_no_sha_or_budget(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It runs in a different job from acquire and derives everything from the run.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    argv = [
        "--mode",
        "verify",
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
    ]
    assert main(argv) == 0


# --- release ---------------------------------------------------------------


def test_release_deletes_our_own_lease(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    http.route("DELETE", "/git/refs/", (204, None))
    assert release(REPO, REF, 100, 1) is True


def test_release_rechecks_the_target_immediately_before_deleting(
    http: FakeHTTP,
) -> None:
    """The check-then-delete is not atomic and the API offers no conditional
    delete, so the target is re-read to narrow the window to the DELETE
    round-trip.

    The interleaving this guards against: the TTL breaks OUR lease while we are
    still live, another run acquires, and we then delete the replacement holder's
    lease — putting a second installer on the tenant. Cannot happen while the TTL
    exceeds the longest legitimate hold, which is why that sizing is pinned by a
    test and why the TTL is load-bearing for release as well as acquisition.
    """
    http.route(
        "GET",
        "/git/ref/",
        (200, _ref_body("ours")),
        (200, _ref_body("someone-elses")),
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    assert release(REPO, REF, 100, 1) is False
    assert http.count("DELETE", "/git/refs/") == 0


def test_release_proceeds_when_the_target_is_unchanged(http: FakeHTTP) -> None:
    http.route(
        "GET",
        "/git/ref/",
        (200, _ref_body("ours")),
        (200, _ref_body("ours")),
    )
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    http.route("DELETE", "/git/refs/", (204, None))
    assert release(REPO, REF, 100, 1) is True


def test_release_refuses_when_ownership_cannot_be_confirmed(http: FakeHTTP) -> None:
    # An unreadable record is not permission to delete. The next contender reaps
    # it if this run is genuinely over.
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (500, {"message": "boom"}))
    assert release(REPO, REF, 100, 1) is False
    assert http.count("DELETE", "/git/refs/") == 0


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
    _stub_unheld(http)
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


def test_main_acquire_fails_when_the_lease_cannot_be_taken(
    http: FakeHTTP, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FND-702: the documented fail-open was fail-closed. `prepare-tenant` runs
    # this same driver in --mode verify, which needs only `contents: read`,
    # finds no lease and exits 1 under `set -euo pipefail` — so exiting 0 here
    # bought nothing except a red two jobs later saying "Re-run this job", which
    # cannot help. Exit non-zero at the one place that can name the grant.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    _stub_unheld(http)
    http.route("POST", "/git/blobs", _PERMISSION_DENIED)
    assert main(_argv("acquire")) == 1
    printed = capsys.readouterr().out
    assert "::error::" in printed
    # Names the grant, the workflow to grant it in, and that retrying is futile.
    assert "contents: write" in printed
    assert "actions: read" in printed
    assert "re-running will not fix it" in printed
    assert "::warning::" not in printed


def test_main_acquire_reports_denied_in_its_outputs(
    http: FakeHTTP, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A caller that reads `state` must see the denial, not a value that reads as
    # a benign posture. `acquired` stays false, so no consumer can mistake it.
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    _stub_unheld(http)
    http.route("POST", "/git/blobs", _PERMISSION_DENIED)
    assert main(_argv("acquire")) == 1
    written = output.read_text()
    assert "state=denied" in written
    assert "acquired=false" in written
    assert "lease-refs=\n" in written


def test_main_acquire_fails_on_a_persistent_rate_limit(
    http: FakeHTTP, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Explicitly NOT fail-open: proceeding unserialised because the API was busy
    # is how two runs end up installing onto one tenant.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    _stub_unheld(http)
    http.route("POST", "/git/blobs", _RATE_LIMIT)
    assert main(_argv("acquire", "--wait-seconds", "1", "--poll-seconds", "1")) == 1
    printed = capsys.readouterr().out
    assert "::error::" in printed
    assert "rate-limited" in printed
    assert "Nothing is wrong with this change" in printed


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
    _stub_unheld(http)
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


def test_token_is_not_in_the_curl_command_line(http: FakeHTTP) -> None:
    """The credential must not be observable via /proc/<pid>/cmdline.

    The token is fed to curl through a stdin config (-K -) instead of a -H
    argument, so while the request runs the process list carries no copy of it.
    """
    secret = "ghp_super_secret_token"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GH_TOKEN", secret)
        http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
        try_acquire(REPO, REF, BLOB)

    cmd = http.cmds[0]
    assert not any(secret in arg for arg in cmd), "token leaked into curl argv"


def test_every_transport_retry_still_carries_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdin is consumed per process, so the config has to be re-supplied on each
    retry. A retry that reused the argv alone would send an unauthenticated
    request and read the resulting 401 as a permission denial — which fails the
    lease OPEN, the worst possible outcome for a transient blip."""
    secret = "ghp_another_secret"
    monkeypatch.setenv("GH_TOKEN", secret)
    seen: list[str | None] = []

    def flaky(cmd, **kwargs):
        seen.append(kwargs.get("input"))
        if len(seen) == 1:
            return _curl_failed()
        return _completed('HTTP/2 201\r\n\r\n{"sha": "abc"}')

    monkeypatch.setattr("e2e_tenant_lease.run", flaky)
    gh_request("POST", "probe", {"x": 1}, sleep=lambda _s: None)

    assert len(seen) == 2
    for supplied in seen:
        assert supplied is not None and secret in supplied


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


# --- ordered multi-cloud acquisition (FND-646) ------------------------------
#
# The property under test is the absence of a deadlock, which no single-run test
# can observe directly. What CAN be pinned is the mechanism that rules it out:
# one run takes its tenants one at a time, in an order that is a pure function of
# the cloud names, and gives back everything if it cannot get the whole set. Get
# any of those three wrong and hold-and-wait is back.


def _ref_for(cloud: str) -> str:
    return f"refs/e2e-tenant-lease/example/{cloud}/holder"


def _path_of(ref: str) -> str:
    """The URL fragment FakeHTTP routes on for a given lease ref."""
    return "/git/ref/" + ref.removeprefix("refs/")


def _ordered(clouds: list[str], **kwargs):
    defaults = dict(
        wait_seconds=60,
        poll_seconds=30,
        ttl_seconds=0,
        sleep=lambda _s: None,
        clock=lambda: 1000.0,
    )
    defaults.update(kwargs)
    return acquire_ordered(REPO, "example", clouds, 100, 1, **defaults)


def _claimed_refs(http: FakeHTTP) -> list[str]:
    """The refs this run put through the CAS, in the order it tried them.

    Read from the payloads rather than the URLs because every ref creation POSTs
    to the same endpoint — the ref name only exists in the body.
    """
    return [p["ref"] for p in http.payloads if "ref" in p]


def _stub_free_and_claimable(http: FakeHTTP, clouds: list[str]) -> None:
    for cloud in clouds:
        http.route("GET", _path_of(_ref_for(cloud)), (404, {"message": "Not Found"}))
    _stub_blob_write(http)
    http.route("POST", "/git/refs", *[(201, {"ref": ""}) for _ in clouds])


def test_ordered_acquisition_takes_every_cloud(http: FakeHTTP) -> None:
    _stub_free_and_claimable(http, ["aws", "azure", "gcp"])
    outcome = _ordered(["aws", "azure", "gcp"])
    assert outcome.state == "acquired"
    assert list(outcome.held) == [_ref_for(c) for c in ("aws", "azure", "gcp")]


def test_ordered_acquisition_sorts_by_cloud_name(http: FakeHTTP) -> None:
    """THE guarantee. Resource ordering only excludes a cycle if every contender
    agrees on the order, so it must come from the names and nothing else — not
    the caller's list order, not run id, not arrival order."""
    _stub_free_and_claimable(http, ["aws", "azure", "gcp"])
    _ordered(["gcp", "aws", "azure"])
    assert _claimed_refs(http) == [_ref_for(c) for c in ("aws", "azure", "gcp")]


def test_ordered_acquisition_of_a_subset_keeps_the_same_relative_order(
    http: FakeHTTP,
) -> None:
    """A repo whose tenant matrix carries only some clouds locks only those. That
    is safe precisely because sorting a subset preserves the relative order of
    the locks it shares with any other contender."""
    _stub_free_and_claimable(http, ["aws", "gcp"])
    _ordered(["gcp", "aws"])
    assert _claimed_refs(http) == [_ref_for("aws"), _ref_for("gcp")]


def test_ordered_acquisition_deduplicates_clouds(http: FakeHTTP) -> None:
    # A duplicate would CAS the same ref twice, and the second attempt reads its
    # own lease as somebody else's occupancy.
    _stub_free_and_claimable(http, ["aws"])
    outcome = _ordered(["aws", "aws"])
    assert list(outcome.held) == [_ref_for("aws")]


@pytest.mark.parametrize("spelling", ["aws,gcp", "aws, gcp", " GCP ,aws", "AWS,gcp"])
def test_ordered_acquisition_orders_by_the_lease_key_not_the_spelling(
    spelling: str, http: FakeHTTP
) -> None:
    """Every spelling of one set names the same pair of LOCKS, so every spelling
    has to take them in the same order. Sorting the raw tokens does not: " gcp"
    sorts before "aws" on its leading space, so `"aws, gcp"` and `"aws,gcp"` would
    take one pair of locks in opposite orders — two contenders disagreeing about
    which lock comes first, which is exactly the FND-646 cycle. So the sort key is
    the `slug()` the lease is keyed on, not the text the caller typed."""
    _stub_free_and_claimable(http, ["aws", "gcp"])
    _ordered(spelling.split(","))
    assert _claimed_refs(http) == [_ref_for("aws"), _ref_for("gcp")]


def test_ordered_acquisition_deduplicates_across_spellings(http: FakeHTTP) -> None:
    # Same lease named twice. Canonicalizing before the dedup is what collapses
    # it to one entry; two raw spellings would CAS the one ref twice, and the
    # second attempt reads this run's own lease as somebody else's occupancy.
    _stub_free_and_claimable(http, ["aws"])
    outcome = _ordered(["aws", " AWS "])
    assert list(outcome.held) == [_ref_for("aws")]


def test_ordered_acquisition_reports_a_denial_on_the_first_cloud(
    http: FakeHTTP,
) -> None:
    # A denial is repo-wide, so it answers on the FIRST lease of the set rather
    # than being rediscovered per cloud. It must reach the caller as "denied" and
    # not as the timeout it would look like if the state were swallowed — the
    # two have opposite remedies ("grant the token" vs "wait for the tenant").
    for cloud in ("aws", "azure", "gcp"):
        http.route("GET", _path_of(_ref_for(cloud)), (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", _PERMISSION_DENIED)

    outcome = _ordered(["aws", "azure", "gcp"])

    assert outcome.state == "denied"
    assert outcome.held == ()
    assert outcome.ref == _ref_for("aws")
    # Nothing was taken, so nothing is given back — and in particular the unwind
    # does not issue a DELETE the same token cannot make either.
    assert http.count("DELETE", "/git/refs/") == 0


def test_a_blocked_cloud_gives_back_what_was_already_held(http: FakeHTTP) -> None:
    """The whole point. Holding aws while blocking on azure is hold-and-wait —
    exactly the shape that deadlocked when two runs split a freed set — so a run
    that cannot get the whole set must hold none of it."""
    theirs = "c" * 40
    http.route(
        "GET",
        _path_of(_ref_for("aws")),
        # Free on the way in, ours on the way back out.
        (404, {"message": "Not Found"}),
        (200, {"ref": _ref_for("aws"), "object": {"sha": BLOB, "type": "blob"}}),
        (200, {"ref": _ref_for("aws"), "object": {"sha": BLOB, "type": "blob"}}),
    )
    http.route(
        "GET",
        _path_of(_ref_for("azure")),
        (200, {"ref": _ref_for("azure"), "object": {"sha": theirs, "type": "blob"}}),
    )
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": _ref_for("aws")}))
    http.route("GET", f"/git/blobs/{BLOB}", (200, _holder_blob(100, 1)))
    http.route("GET", f"/git/blobs/{theirs}", (200, _holder_blob(555)))
    http.route("GET", "/actions/runs/", _live())
    http.route("DELETE", "/git/refs/", (204, None))

    outcome = _ordered(["aws", "azure"], wait_seconds=1, poll_seconds=1)

    assert outcome.state == "timeout"
    assert outcome.held == ()
    assert outcome.blocker is not None and outcome.blocker.run_id == 555
    # The error names the tenant that actually blocked, not the first of the set.
    assert outcome.ref == _ref_for("azure")
    assert http.count("DELETE", "/git/refs/") == 1


def test_the_wait_budget_is_total_across_the_set(http: FakeHTTP) -> None:
    """`wait-seconds` becomes a budget for the whole ordered set, not a fresh one
    per cloud — otherwise N clouds could outlast the job timeout by N times over
    and the runner's "cancelled after Nm" would replace the actionable error."""
    ticks = iter([1000.0, 1000.0, 1000.0, 9999.0, 9999.0, 9999.0])
    http.route("GET", _path_of(_ref_for("aws")), (404, {"message": "Not Found"}))
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": _ref_for("aws")}))
    http.route("GET", f"/git/blobs/{BLOB}", (200, _holder_blob(100, 1)))
    http.route("DELETE", "/git/refs/", (204, None))

    outcome = _ordered(["aws", "azure"], wait_seconds=60, clock=lambda: next(ticks))

    assert outcome.state == "timeout"
    # azure was never even attempted: the budget was gone.
    assert _claimed_refs(http) == [_ref_for("aws")]
    assert outcome.ref == _ref_for("azure")
    assert outcome.held == ()


def test_release_all_gives_every_lease_back(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    http.route("DELETE", "/git/refs/", (204, None), (204, None))
    release_all(REPO, [_ref_for("aws"), _ref_for("azure")], 100, 1)
    assert http.count("DELETE", "/git/refs/") == 2


def test_release_all_of_nothing_makes_no_calls(http: FakeHTTP) -> None:
    release_all(REPO, [], 100, 1)
    assert http.calls == []


def test_release_all_attempts_every_ref_even_when_one_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the unwind path for a partial hold, so it must not abandon the rest
    of the set at the first transport failure: the leases it skipped would stay
    held until their TTL expired, which is the hold-and-wait that ordered
    acquisition exists to remove. Every ref is attempted, then the first failure
    is re-raised so the job still goes red."""
    attempted: list[str] = []

    def failing_release(repo: str, ref: str, run_id: int, attempt: int) -> bool:
        attempted.append(ref)
        if ref == _ref_for("gcp"):
            raise SystemExit("::error::curl retries exhausted")
        return True

    monkeypatch.setattr("e2e_tenant_lease.release", failing_release)

    with pytest.raises(SystemExit, match="retries exhausted"):
        release_all(REPO, [_ref_for("aws"), _ref_for("gcp")], 100, 1)

    # Reverse order, so gcp is the one that fails — and aws is still given back.
    assert attempted == [_ref_for("gcp"), _ref_for("aws")]


# --- the ordered CLI -------------------------------------------------------


def _argv_clouds(mode: str, clouds: str, *extra: str) -> list[str]:
    return [
        "--mode",
        mode,
        "--repo",
        REPO,
        "--app",
        "example",
        "--clouds",
        clouds,
        "--run-id",
        "100",
        "--run-attempt",
        "1",
        *extra,
    ]


def test_main_acquire_reports_the_whole_set_it_holds(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    _stub_free_and_claimable(http, ["aws", "gcp"])

    assert main(_argv_clouds("acquire", "gcp,aws")) == 0

    written = out.read_text()
    assert "acquired=true" in written
    assert f"lease-refs={_ref_for('aws')},{_ref_for('gcp')}" in written
    assert "clouds=aws,gcp" in written


def test_main_acquire_fails_when_the_whole_set_is_denied(
    http: FakeHTTP, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The path lease-tenant actually takes — it always passes --clouds — so the
    # FND-702 exit code has to hold on the ordered branch too, not only on the
    # single-tenant one.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    for cloud in ("aws", "azure", "gcp"):
        http.route("GET", _path_of(_ref_for(cloud)), (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", _PERMISSION_DENIED)

    assert main(_argv_clouds("acquire", "aws,azure,gcp")) == 1
    assert "::error::" in capsys.readouterr().out


def test_main_acquire_with_an_empty_cloud_list_falls_back_to_one_tenant(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single-tenant path resolves an EMPTY cloud list, so an empty --clouds
    has to mean "use --cloud" rather than "lease nothing". That is what lets the
    action pass both unconditionally instead of branching in shell."""
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    _stub_unheld(http)
    _stub_blob_write(http)
    http.route("POST", "/git/refs", (201, {"ref": REF}))

    assert main(_argv("acquire", "--clouds", "")) == 0
    assert f"lease-refs={REF}" in out.read_text()


def test_main_rejects_both_cloud_and_clouds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Preferring one silently would leave the other's tenant unserialised while
    # the job still went green.
    monkeypatch.setenv("GH_TOKEN", "x")
    with pytest.raises(SystemExit, match="both given"):
        main(_argv("acquire", "--clouds", "aws,gcp"))


def test_main_verify_refuses_a_cloud_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """verify exists so each install leg confirms ITS OWN tenant. A set-shaped
    verify would invite the install to check the aggregate again, which is the
    approximation this mode replaced."""
    monkeypatch.setenv("GH_TOKEN", "x")
    with pytest.raises(SystemExit, match="verify takes --cloud"):
        main(_argv_clouds("verify", "aws,gcp"))


def test_main_release_gives_back_the_whole_set(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("GET", "/git/ref/", (200, _ref_body()), (200, _ref_body()))
    http.route("GET", "/git/blobs/", (200, _holder_blob(100, 1)))
    http.route("DELETE", "/git/refs/", (204, None), (204, None))

    assert main(_argv_clouds("release", "aws,gcp")) == 0
    assert http.count("DELETE", "/git/refs/") == 2


# --- the wait has to be visible while it is waiting ------------------------


_LEASE_SCRIPT = (
    Path(__file__).parent.parent.parent
    / "actions"
    / "e2e-tenant-lease"
    / "e2e_tenant_lease.py"
)


def test_the_wait_log_streams_rather_than_arriving_at_exit() -> None:
    """Python block-buffers stdout when it is a pipe, and an Actions log is
    always a pipe. Without the import-time line-buffering this module sets, a run
    that queued for ten minutes showed an EMPTY step for ten minutes and then
    printed all ten minutes of "waiting for ..." at once — indistinguishable from
    a wedged step, and duly read as a deadlock (FND-696).

    Asserted through a real pipe rather than by reading the source: the property
    is "the line arrives before the process does", and only a subprocess can say
    that.
    """
    program = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(_LEASE_SCRIPT.parent)!r})\n"
        # Importing is the whole point: the buffering is configured at import,
        # so nothing here may print before it.
        "import e2e_tenant_lease  # noqa: F401\n"
        "print('[1/180] waiting for the tenant')\n"
        # Long enough that an exit flush cannot be what delivered the line.
        "time.sleep(120)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 30)
        assert ready, (
            "nothing reached the log while the process was still running, so a "
            "waiting lease is invisible for as long as it waits"
        )
        assert proc.stdout is not None
        assert "waiting for the tenant" in proc.stdout.readline()
    finally:
        proc.kill()
        proc.wait(timeout=30)
