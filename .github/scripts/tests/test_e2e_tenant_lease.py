"""Tests for .github/actions/e2e-tenant-lease/e2e_tenant_lease.py.

Co-located module (checked out with the composite action in consumer repos); the
test lives here with the other action-script tests.

The HTTP client is stubbed at the ``run()`` seam with a fake that speaks real
curl-shaped responses, so status-code handling — 422 "already exists" vs 403
"no permission" vs a genuine error — is exercised through the same parser
production uses rather than around it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "actions" / "e2e-tenant-lease")
)

from e2e_tenant_lease import (  # noqa: E402
    Ticket,
    acquire,
    create_ticket,
    delete_ticket,
    lease_prefix,
    list_tickets,
    main,
    parse_ticket,
    release,
    run_is_live,
    slug,
    write_outputs,
)

REPO = "atlanhq/atlan-example-app"
PREFIX = "e2e-tenant-lease/example/aws"
SHA = "a" * 40


# --- fake HTTP client ------------------------------------------------------


class FakeHTTP:
    """Records requests and replays queued curl-shaped responses.

    Keyed by (method, path-prefix) so a test only has to describe the calls it
    cares about; anything unmatched raises rather than silently answering 200,
    which is what keeps a test from passing because production stopped making a
    call it was supposed to make.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[tuple[int, object]]] = {}
        self.calls: list[tuple[str, str]] = []

    def route(self, method: str, contains: str, *responses: tuple[int, object]) -> None:
        self.routes[(method, contains)] = list(responses)

    def __call__(self, cmd: list[str], **kwargs):
        method = cmd[cmd.index("-X") + 1]
        url = cmd[-1]
        self.calls.append((method, url))
        for (route_method, contains), responses in self.routes.items():
            if route_method != method or contains not in url:
                continue
            status, body = responses[0] if len(responses) == 1 else responses.pop(0)
            payload = "" if body is None else json.dumps(body)
            return _completed(f"HTTP/2 {status}\r\n\r\n{payload}")
        raise AssertionError(f"unstubbed request: {method} {url}")


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


def _ref(run_id: int, attempt: int = 1) -> dict:
    return {"ref": f"refs/{PREFIX}/{run_id}-{attempt}"}


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


def test_lease_prefix_combines_app_and_cloud() -> None:
    assert lease_prefix("openapi", "aws") == "e2e-tenant-lease/openapi/aws"


def test_lease_prefix_defaults_the_cloud() -> None:
    assert lease_prefix("openapi", "") == "e2e-tenant-lease/openapi/default"


def test_different_clouds_of_one_app_do_not_share_a_lease() -> None:
    # Per-cloud tenants are independent resources; sharing a lease across them
    # would serialise legs that have no reason to wait for each other.
    assert lease_prefix("openapi", "aws") != lease_prefix("openapi", "gcp")


# --- ticket identity and ordering -----------------------------------------


def test_ticket_name_is_derived_only_from_the_run() -> None:
    # The whole protocol rests on this: the acquiring job and the releasing job
    # are different jobs that must compute the same name with no shared state.
    assert Ticket(12, 1).name == "12-1"


def test_ticket_ordering_is_by_run_id() -> None:
    assert sorted([Ticket(30, 1), Ticket(10, 1), Ticket(20, 1)]) == [
        Ticket(10, 1),
        Ticket(20, 1),
        Ticket(30, 1),
    ]


def test_ticket_ordering_breaks_ties_on_attempt() -> None:
    assert sorted([Ticket(10, 2), Ticket(10, 1)]) == [Ticket(10, 1), Ticket(10, 2)]


def test_parse_ticket_round_trips_a_ref() -> None:
    assert parse_ticket(f"refs/{PREFIX}/4242-2") == Ticket(4242, 2)


@pytest.mark.parametrize(
    "ref",
    [
        f"refs/{PREFIX}/not-a-ticket",
        f"refs/{PREFIX}/12",
        f"refs/{PREFIX}/12-",
        f"refs/{PREFIX}/-1",
        f"refs/{PREFIX}/12-1-99",
    ],
)
def test_parse_ticket_rejects_non_tickets(ref: str) -> None:
    assert parse_ticket(ref) is None


# --- ref primitives --------------------------------------------------------


def test_create_ticket_reports_created(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {"ref": "x"}))
    assert create_ticket(REPO, PREFIX, Ticket(1, 1), SHA) == "created"


def test_create_ticket_treats_an_existing_ref_as_ours(http: FakeHTTP) -> None:
    # Reached whenever a later job of the same run re-derives the ticket, and on
    # a retried step. Not a conflict: the name identifies the run.
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    assert create_ticket(REPO, PREFIX, Ticket(1, 1), SHA) == "exists"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_create_ticket_reports_denied_without_ref_write(
    http: FakeHTTP, status: int
) -> None:
    http.route("POST", "/git/refs", (status, {"message": "Resource not accessible"}))
    assert create_ticket(REPO, PREFIX, Ticket(1, 1), SHA) == "denied"


def test_create_ticket_raises_on_an_unrelated_422(http: FakeHTTP) -> None:
    # A malformed ref or a bad sha is a bug in the caller, not contention — it
    # must not be swallowed as "someone else holds the lease".
    http.route("POST", "/git/refs", (422, {"message": "Object does not exist"}))
    with pytest.raises(SystemExit):
        create_ticket(REPO, PREFIX, Ticket(1, 1), SHA)


def test_list_tickets_parses_and_skips_foreign_refs(http: FakeHTTP) -> None:
    http.route(
        "GET",
        "/git/matching-refs/",
        (200, [_ref(10), {"ref": f"refs/{PREFIX}/README"}, _ref(20, 3)]),
    )
    assert list_tickets(REPO, PREFIX) == [Ticket(10, 1), Ticket(20, 3)]


def test_list_tickets_treats_404_as_an_empty_queue(http: FakeHTTP) -> None:
    http.route("GET", "/git/matching-refs/", (404, {"message": "Not Found"}))
    assert list_tickets(REPO, PREFIX) == []


def test_list_tickets_raises_on_a_server_error(http: FakeHTTP) -> None:
    # Silently reading "no queue" from a 500 would hand the tenant to every
    # waiting run at once.
    http.route("GET", "/git/matching-refs/", (500, {"message": "boom"}))
    with pytest.raises(SystemExit):
        list_tickets(REPO, PREFIX)


def test_delete_ticket_reports_success(http: FakeHTTP) -> None:
    http.route("DELETE", "/git/refs/", (204, None))
    assert delete_ticket(REPO, PREFIX, Ticket(1, 1)) is True


def test_delete_ticket_tolerates_an_already_reaped_ticket(http: FakeHTTP) -> None:
    # Two contenders can reap the same dead ticket; the loser must not fail.
    http.route("DELETE", "/git/refs/", (422, {"message": "Reference does not exist"}))
    assert delete_ticket(REPO, PREFIX, Ticket(1, 1)) is False


# --- holder liveness -------------------------------------------------------


def test_holder_in_progress_is_live(http: FakeHTTP) -> None:
    http.route("GET", "/actions/runs/", (200, {"status": "in_progress"}))
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=0) is True


def test_holder_completed_is_dead(http: FakeHTTP) -> None:
    http.route("GET", "/actions/runs/", (200, {"status": "completed"}))
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=0) is False


def test_holder_deleted_run_is_dead(http: FakeHTTP) -> None:
    # Nothing will ever release this ticket, so it has to be breakable.
    http.route("GET", "/actions/runs/", (404, {"message": "Not Found"}))
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=0) is False


@pytest.mark.parametrize("status", ["queued", "waiting", "requested", "pending"])
def test_holder_pre_start_statuses_are_live(http: FakeHTTP, status: str) -> None:
    # A queued run has a place in line and will install; treating it as dead
    # would let a later run install underneath it.
    http.route("GET", "/actions/runs/", (200, {"status": status}))
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=0) is True


def test_holder_is_assumed_live_on_an_api_error(http: FakeHTTP) -> None:
    # Erring towards "dead" on a transient 500 would put two runs on one tenant
    # mid-install — the exact race the lease exists to prevent. Erring towards
    # "live" costs one poll interval.
    http.route("GET", "/actions/runs/", (500, {"message": "boom"}))
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=0) is True


def test_holder_past_ttl_is_broken(http: FakeHTTP) -> None:
    http.route(
        "GET",
        "/actions/runs/",
        (200, {"status": "in_progress", "created_at": "2020-01-01T00:00:00Z"}),
    )
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=60) is False


def test_holder_within_ttl_is_left_alone(http: FakeHTTP) -> None:
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    http.route(
        "GET",
        "/actions/runs/",
        (200, {"status": "in_progress", "created_at": started.isoformat()}),
    )
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=3600) is True


def test_ttl_of_zero_never_breaks_a_live_holder(http: FakeHTTP) -> None:
    http.route(
        "GET",
        "/actions/runs/",
        (200, {"status": "in_progress", "created_at": "2020-01-01T00:00:00Z"}),
    )
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=0) is True


def test_holder_without_a_created_at_is_left_alone(http: FakeHTTP) -> None:
    # No timestamp means the TTL backstop cannot judge; the run's own status is
    # then the only evidence, and it says live.
    http.route("GET", "/actions/runs/", (200, {"status": "in_progress"}))
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=60) is True


def test_holder_with_an_unparseable_created_at_is_left_alone(http: FakeHTTP) -> None:
    http.route(
        "GET", "/actions/runs/", (200, {"status": "in_progress", "created_at": "soon"})
    )
    assert run_is_live(REPO, Ticket(1, 1), ttl_seconds=60) is True


# --- acquire ---------------------------------------------------------------


def _acquire(**kwargs):
    defaults = dict(
        wait_seconds=60,
        poll_seconds=30,
        ttl_seconds=0,
        sleep=lambda _s: None,
    )
    defaults.update(kwargs)
    return acquire(REPO, PREFIX, Ticket(100, 1), SHA, **defaults)


def test_acquire_takes_an_uncontended_lease(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(100)]))
    assert _acquire() == ("acquired", None)


def test_acquire_takes_the_lease_when_it_is_the_lowest_run_id(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(200), _ref(100), _ref(300)]))
    # No liveness call is needed or made: nothing sorts ahead of us.
    assert _acquire() == ("acquired", None)
    assert not [call for call in http.calls if "/actions/runs/" in call[1]]


def test_acquire_waits_behind_a_live_earlier_run(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(50), _ref(100)]))
    http.route("GET", "/actions/runs/", (200, {"status": "in_progress"}))
    assert _acquire() == ("timeout", Ticket(50, 1))


def test_acquire_reaps_a_dead_holder_and_proceeds(http: FakeHTTP) -> None:
    # The case an `if: always()` release job cannot cover: a cancelled run whose
    # release job never started. The next contender clears it in-band.
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(50), _ref(100)]))
    http.route("GET", "/actions/runs/", (200, {"status": "completed"}))
    http.route("DELETE", "/git/refs/", (204, None))
    assert _acquire() == ("acquired", None)
    assert (
        "DELETE",
        f"https://api.github.com/repos/{REPO}/git/refs/{PREFIX}/50-1",
    ) in (http.calls)


def test_acquire_reaps_several_dead_holders_in_one_poll(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(30), _ref(40), _ref(100)]))
    http.route("GET", "/actions/runs/", (200, {"status": "completed"}))
    http.route("DELETE", "/git/refs/", (204, None))
    assert _acquire() == ("acquired", None)
    deletes = [call for call in http.calls if call[0] == "DELETE"]
    assert len(deletes) == 2


def test_acquire_stops_reaping_at_the_first_live_holder(http: FakeHTTP) -> None:
    # 30 is dead, 40 is live: 40 holds the lease and 100 must not delete it.
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(30), _ref(40), _ref(100)]))
    http.route(
        "GET",
        "/actions/runs/",
        (200, {"status": "completed"}),
        (200, {"status": "in_progress"}),
    )
    http.route("DELETE", "/git/refs/", (204, None))
    # A single poll, so the two queued liveness answers map one-to-one onto the
    # two tickets ahead of us.
    assert _acquire(wait_seconds=30, poll_seconds=30) == ("timeout", Ticket(40, 1))
    deletes = [call for call in http.calls if call[0] == "DELETE"]
    assert len(deletes) == 1
    assert "30-1" in deletes[0][1]


def test_acquire_gets_the_lease_on_a_later_poll(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {}))
    http.route(
        "GET",
        "/git/matching-refs/",
        (200, [_ref(50), _ref(100)]),
        (200, [_ref(100)]),
    )
    http.route("GET", "/actions/runs/", (200, {"status": "in_progress"}))
    assert _acquire(wait_seconds=60, poll_seconds=30) == ("acquired", None)


def test_acquire_recreates_a_ticket_that_vanished(http: FakeHTTP) -> None:
    # A peer that misread this run as finished can reap us. Re-creating restores
    # the same position, because the name is derived from the run.
    http.route("POST", "/git/refs", (201, {}), (201, {}))
    http.route("GET", "/git/matching-refs/", (200, []))
    assert _acquire() == ("acquired", None)
    assert len([call for call in http.calls if call[0] == "POST"]) == 2


def test_acquire_disables_itself_without_ref_write(http: FakeHTTP) -> None:
    # Fail-open: the lease reduces contention, it is not what makes a
    # wrong-version run detectable (each leg re-asserts the version, FND-31).
    http.route("POST", "/git/refs", (403, {"message": "Resource not accessible"}))
    assert _acquire() == ("disabled", None)


def test_acquire_disables_itself_when_recreation_is_denied(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {}), (403, {"message": "nope"}))
    http.route("GET", "/git/matching-refs/", (200, []))
    assert _acquire() == ("disabled", None)


def test_acquire_makes_two_api_calls_per_contended_poll(http: FakeHTTP) -> None:
    # The GITHUB_TOKEN budget is 1000 requests/hour/repo, and only the current
    # front-of-queue ticket is checked for liveness — checking every ticket every
    # poll would blow that budget on a long queue.
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(40), _ref(50), _ref(100)]))
    http.route("GET", "/actions/runs/", (200, {"status": "in_progress"}))
    _acquire(wait_seconds=30, poll_seconds=30)
    polling_calls = [call for call in http.calls if call[0] == "GET"]
    assert len(polling_calls) == 2


def test_acquire_respects_the_wait_budget(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(50), _ref(100)]))
    http.route("GET", "/actions/runs/", (200, {"status": "in_progress"}))
    slept: list[int] = []
    _acquire(wait_seconds=90, poll_seconds=30, sleep=slept.append)
    # 3 attempts, so 2 sleeps: the last attempt is not followed by a wait.
    assert slept == [30, 30]


def test_acquire_always_makes_one_attempt_on_a_zero_budget(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(100)]))
    assert _acquire(wait_seconds=0, poll_seconds=30) == ("acquired", None)


# --- release ---------------------------------------------------------------


def test_release_deletes_this_runs_ticket(http: FakeHTTP) -> None:
    http.route("DELETE", "/git/refs/", (204, None))
    assert release(REPO, PREFIX, Ticket(100, 1)) is True
    assert http.calls[0][1].endswith(f"/git/refs/{PREFIX}/100-1")


def test_release_is_quiet_when_the_ticket_is_already_gone(http: FakeHTTP) -> None:
    http.route("DELETE", "/git/refs/", (422, {"message": "Reference does not exist"}))
    assert release(REPO, PREFIX, Ticket(100, 1)) is False


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
        "--sha",
        SHA,
        *extra,
    ]


def test_main_acquire_exits_zero_and_reports_outputs(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(100)]))
    assert main(_argv("acquire")) == 0
    assert "acquired=true" in out.read_text()
    assert f"lease-ref=refs/{PREFIX}/100-1" in out.read_text()


def test_main_acquire_fails_loudly_on_timeout(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(50), _ref(100)]))
    http.route("GET", "/actions/runs/", (200, {"status": "in_progress"}))
    assert main(_argv("acquire", "--wait-seconds", "1", "--poll-seconds", "1")) == 1
    printed = capsys.readouterr().out
    # The holder has to be named: "the tenant is busy" is only actionable if the
    # reader can see which run to wait for.
    assert "::error::" in printed
    assert "50" in printed
    assert "acquired=false" in out.read_text()
    assert "holder-run-id=50" in out.read_text()


def test_main_acquire_timeout_says_the_change_is_not_at_fault(
    http: FakeHTTP, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of FND-250: a contention outcome must not read as a test
    # failure, because that sends a reviewer into the wrong diff.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(50), _ref(100)]))
    http.route("GET", "/actions/runs/", (200, {"status": "in_progress"}))
    main(_argv("acquire", "--wait-seconds", "1", "--poll-seconds", "1"))
    assert "Nothing is wrong with this change" in capsys.readouterr().out


def test_main_acquire_can_warn_instead_of_failing(
    http: FakeHTTP, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(50), _ref(100)]))
    http.route("GET", "/actions/runs/", (200, {"status": "in_progress"}))
    argv = _argv(
        "acquire", "--wait-seconds", "1", "--poll-seconds", "1", "--on-timeout", "warn"
    )
    assert main(argv) == 0
    assert "::warning::" in capsys.readouterr().out


def test_main_acquire_exits_zero_when_the_lease_is_disabled(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("POST", "/git/refs", (403, {"message": "nope"}))
    assert main(_argv("acquire")) == 0


def test_main_release_exits_zero(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("DELETE", "/git/refs/", (204, None))
    assert main(_argv("release")) == 0


def test_main_release_exits_zero_even_when_nothing_was_held(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed release must never redden a run whose tests passed; the reaper
    # covers it.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("DELETE", "/git/refs/", (422, {"message": "Reference does not exist"}))
    assert main(_argv("release")) == 0


def test_main_release_needs_no_sha(
    http: FakeHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The release job derives the ticket from the run, so it does not have to be
    # handed any state by the acquiring job.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    http.route("DELETE", "/git/refs/", (204, None))
    argv = [
        "--mode",
        "release",
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


def test_main_acquire_requires_a_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    argv = [
        "--mode",
        "acquire",
        "--repo",
        REPO,
        "--app",
        "example",
        "--run-id",
        "100",
        "--run-attempt",
        "1",
    ]
    with pytest.raises(SystemExit):
        main(argv)


def test_acquire_and_release_agree_on_the_ticket_ref(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two modes run in different jobs and share no state — if they ever
    disagreed about the ref name, every run would leak its lease."""
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    http.route("POST", "/git/refs", (201, {}))
    http.route("GET", "/git/matching-refs/", (200, [_ref(100)]))
    main(_argv("acquire"))
    acquired_ref = [
        line.split("=", 1)[1]
        for line in out.read_text().splitlines()
        if line.startswith("lease-ref=")
    ][0]

    http.calls.clear()
    http.route("DELETE", "/git/refs/", (204, None))
    main(_argv("release"))
    deleted = http.calls[0][1]
    assert deleted.endswith(acquired_ref.removeprefix("refs/"))


def test_missing_token_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="GH_TOKEN"):
        create_ticket(REPO, PREFIX, Ticket(1, 1), SHA)
