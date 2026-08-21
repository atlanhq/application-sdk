"""Tests for .github/scripts/e2e_dispatch_guard.py (FND-646).

The HTTP client is stubbed at the ``run()`` seam with a fake that speaks real
curl-shaped responses, so status-code handling — 422 "already exists" (which IS
the lock), 403 permission, 403 rate limit, 500 transient — is exercised through
the same parser production uses rather than around it.

Two properties get the most attention here, because both are the kind that pass
a happy-path test and then cost a tenant:

* the CAS decides, not the pre-read. The tests drive the sequence a second
  contender really sees (free, then taken) rather than a tidy snapshot.
* every failure direction is OPEN. A guard that cannot be operated must still
  dispatch, because a PR whose e2e silently never ran is worse than one that
  pays for a duplicate — which is the pre-existing behaviour anyway.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

# The gate is imported only so the drift test can hold its copy of
# check_run_age_key against the guard's; production never imports across scripts.
import poll_check_runs_gate as gate  # noqa: E402
from e2e_dispatch_guard import (  # noqa: E402
    _CHECKS_MAX_PAGES,
    _PASSING_CONCLUSIONS,
    Claimant,
    GuardUnavailable,
    check_run_age_key,
    checks_state,
    claim_is_settled,
    claim_ref,
    main,
    open_pr_heads,
    prune,
    read_dispatch_state,
    resolve,
    run_is_live,
    sha_of,
    slug,
    try_claim,
)

REPO = "atlanhq/application-sdk"
APP = "atlan-openapi-app"
SHA = "c5597b22" + "0" * 32
CHECK = f"Connector E2E run / {APP}"
REF = f"refs/e2e-dispatch/{APP}/{SHA}"
BLOB = "b" * 40

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ACTION = _REPO_ROOT / ".github/actions/e2e-apps/action.yaml"
_PR_WORKFLOW = _REPO_ROOT / ".github/workflows/pull_request.yaml"


# --- fake HTTP client ------------------------------------------------------


class FakeHTTP:
    """Records requests and replays queued curl-shaped responses.

    Keyed by (method, path-fragment) so a test only describes the calls it cares
    about; anything unmatched raises rather than silently answering 200, which is
    what keeps a test from passing because production stopped making a call it
    was supposed to make.

    A route with several responses pops one per call, so a test can express
    "free, then taken" — the sequence, not just the snapshot.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[tuple]] = {}
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict] = []
        self.cmds: list[list[str]] = []
        self.inputs: list[str | None] = []

    def route(self, method: str, contains: str, *responses: tuple) -> None:
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
    monkeypatch.setattr("e2e_dispatch_guard.run", fake)
    return fake


def _claim_blob(run_id: int, attempt: int = 1, claimed_at: float | None = 1000.0):
    record: dict[str, object] = {"run_id": run_id, "attempt": attempt}
    if claimed_at is not None:
        record["claimed_at"] = claimed_at
    encoded = base64.b64encode(json.dumps(record).encode()).decode()
    return {"content": encoded, "encoding": "base64"}


def _checks(*entries: tuple[str, str]):
    return {
        "check_runs": [{"name": name, "status": status} for name, status in entries]
    }


def _attempt(
    conclusion: str | None, *, at: str, check_id: int, name: str | None = None
):
    """One concluded (or running) attempt's check run, with an age.

    Separate from ``_checks`` because the reclaim decision reads fields that
    helper does not carry — a conclusion, and enough ordering information to say
    which of several same-named checks is the newest.
    """
    entry = {
        "name": name or CHECK,
        "status": "completed" if conclusion else "in_progress",
        "started_at": at,
        "id": check_id,
    }
    if conclusion:
        entry["conclusion"] = conclusion
    return entry


def _attempts(*entries: dict):
    return {"total_count": len(entries), "check_runs": list(entries)}


OLD_FAIL = _attempt("failure", at="2026-08-21T00:53:41Z", check_id=100)
NEW_PASS = _attempt("success", at="2026-08-21T01:41:00Z", check_id=200)


_RATE_LIMIT = (
    403,
    {"message": "API rate limit exceeded for installation"},
    {"x-ratelimit-remaining": "0"},
)
_PERMISSION_DENIED = (403, {"message": "Resource not accessible by integration"})


def _resolve(**kwargs):
    defaults = dict(
        app=APP,
        sha=SHA,
        check_run_name=CHECK,
        run_id=1,
        attempt=1,
        clock=lambda: 1000.0,
    )
    defaults.update(kwargs)
    return resolve(REPO, REF, **defaults)  # type: ignore[arg-type]


# --- keying ----------------------------------------------------------------


def test_the_claim_ref_puts_the_app_before_the_sha() -> None:
    """App first so ``matching-refs/e2e-dispatch/<app>`` lists one app's claims;
    the prune must never have to walk the whole namespace."""
    assert claim_ref(APP, SHA) == f"refs/e2e-dispatch/{APP}/{SHA}"


def test_the_claim_ref_is_fixed_for_a_given_sha_and_app() -> None:
    # The whole mechanism is collision: a name only one run could guess is a
    # name that cannot collide, and collision is the signal.
    assert claim_ref(APP, SHA) == claim_ref(APP.upper(), SHA)


def test_the_app_slug_cannot_produce_a_ref_git_rejects() -> None:
    assert slug("My App/Name") == "my-app-name"
    assert ".." not in slug("a..b")
    assert not slug("thing.lock").endswith(".lock")
    assert slug("///") == "app"


def test_sha_of_reads_the_last_component() -> None:
    assert sha_of(REF) == SHA


def test_sha_of_rejects_a_non_sha_tail() -> None:
    # Guards the prune: a ref whose tail is not a commit sha must never be
    # matched against the open-PR head set and deleted on a miss.
    assert sha_of("refs/e2e-dispatch/app/not-a-sha") == ""


# --- the CAS ---------------------------------------------------------------


def test_a_422_already_exists_is_the_slot_being_taken(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    assert try_claim(REPO, REF, BLOB) == "taken"


def test_a_201_is_the_slot_being_claimed(http: FakeHTTP) -> None:
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    assert try_claim(REPO, REF, BLOB) == "claimed"


@pytest.mark.parametrize("answer", [_PERMISSION_DENIED, _RATE_LIMIT])
def test_a_403_on_the_cas_makes_the_guard_unavailable(http: FakeHTTP, answer) -> None:
    """Both directions fail open at the caller. They are told apart only so the
    warning names the real cause — a rate-limited hour reported as a permissions
    problem is a diagnosis-hostile lie."""
    http.route("POST", "/git/refs", answer)
    with pytest.raises(GuardUnavailable):
        try_claim(REPO, REF, BLOB)


def test_an_unheld_slot_is_claimed(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (201, {"ref": REF}))

    assert _resolve() == ("claimed", None)


def test_the_claim_record_names_this_run(http: FakeHTTP) -> None:
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (201, {"ref": REF}))

    _resolve(run_id=4242, attempt=3)

    record = json.loads(http.payloads[0]["content"])
    assert record["run_id"] == 4242
    assert record["attempt"] == 3
    assert record["claimed_at"] == 1000.0


def test_losing_the_race_after_the_pre_read_is_not_a_claim(http: FakeHTTP) -> None:
    """THE case that has to work. The pre-read is an optimisation; the CAS is the
    authority, and a contender that read "free" and then lost the CAS must fall
    through to judging the winner — not proceed as if it had won."""
    http.route(
        "GET",
        "/git/ref/",
        (404, {"message": "Not Found"}),
        (200, {"object": {"sha": BLOB, "type": "blob"}}),
    )
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))
    http.route("GET", "/git/blobs/", (200, _claim_blob(999)))
    http.route("GET", "/check-runs", (200, _checks((CHECK, "in_progress"))))

    state, blocker = _resolve(run_id=1)

    assert state == "duplicate"
    assert blocker is not None and blocker.run_id == 999


# --- resolving an existing claim -------------------------------------------


def _stub_taken(http: FakeHTTP, run_id: int, attempt: int = 1) -> None:
    http.route("GET", "/git/ref/", (200, {"object": {"sha": BLOB, "type": "blob"}}))
    http.route("GET", "/git/blobs/", (200, _claim_blob(run_id, attempt)))


def test_a_dispatch_already_made_for_this_sha_is_skipped(http: FakeHTTP) -> None:
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _checks((CHECK, "in_progress"))))

    state, blocker = _resolve(run_id=1)

    assert state == "duplicate"
    assert blocker is not None and blocker.run_id == 999


def test_a_completed_dispatch_for_this_sha_is_still_skipped(http: FakeHTTP) -> None:
    """Same commit, same code, already tested. The verdict is that check run, and
    every duplicate run's own connector gate polls it — re-dispatching would buy
    a second answer to a question already answered, at the price of a tenant."""
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _checks((CHECK, "completed"))))

    assert _resolve(run_id=1)[0] == "duplicate"


# --- retrying a leg that dispatched and failed -----------------------------
#
# Re-adding the `e2e` label is what people reach for to retry a failed leg, and
# it used to spend a whole run (both native base-image builds, Trivy, SDR K8s
# E2E) dispatching nothing and re-reading the same red check. A new run may now
# take the slot — but only when the failure is genuinely settled, because every
# loosening here is paid for in tenant contention.


def _stub_reclaim_writes(http: FakeHTTP, *, wins_the_race: bool = True) -> None:
    http.route("DELETE", "/git/refs/", (204, None))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route(
        "POST",
        "/git/refs",
        (201, {"ref": REF}) if wins_the_race else (422, {"message": "already exists"}),
    )


def test_a_settled_failure_is_reclaimed_by_a_new_run(http: FakeHTTP) -> None:
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _attempts(OLD_FAIL)))
    http.route("GET", "/actions/runs/999", (200, {"status": "completed"}))
    _stub_reclaim_writes(http)

    assert _resolve(run_id=1) == ("reclaimed", None)


def test_a_passing_leg_is_never_reclaimed(http: FakeHTTP) -> None:
    """Re-testing a green leg spends a tenant to re-answer an answered question,
    and the required check is already satisfied by it."""
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _attempts(NEW_PASS)))

    assert _resolve(run_id=1)[0] == "duplicate"


@pytest.mark.parametrize("passing", ["success", "neutral", "skipped"])
def test_every_conclusion_the_gate_calls_a_pass_is_not_retryable(
    http: FakeHTTP, passing
) -> None:
    """These three are exactly poll_check_runs_gate.PASSING_CONCLUSIONS. Treating
    any of them as retryable would re-dispatch a leg the gate is green on."""
    _stub_taken(http, 999)
    http.route(
        "GET",
        "/check-runs",
        (200, _attempts(_attempt(passing, at="2026-08-21T01:00:00Z", check_id=1))),
    )

    assert _resolve(run_id=1)[0] == "duplicate"


@pytest.mark.parametrize("failing", ["failure", "cancelled", "timed_out", "stale"])
def test_the_non_passing_conclusions_are_retryable(http: FakeHTTP, failing) -> None:
    _stub_taken(http, 999)
    http.route(
        "GET",
        "/check-runs",
        (200, _attempts(_attempt(failing, at="2026-08-21T01:00:00Z", check_id=1))),
    )
    http.route("GET", "/actions/runs/999", (200, {"status": "completed"}))
    _stub_reclaim_writes(http)

    assert _resolve(run_id=1) == ("reclaimed", None)


def test_a_leg_still_in_flight_is_never_reclaimed(http: FakeHTTP) -> None:
    """Its connector run is at the tenant right now; a second dispatch is the
    contention this module exists to prevent."""
    _stub_taken(http, 999)
    http.route(
        "GET",
        "/check-runs",
        (200, _attempts(_attempt(None, at="2026-08-21T01:00:00Z", check_id=1))),
    )

    assert _resolve(run_id=1)[0] == "duplicate"


def test_an_older_attempt_still_running_blocks_the_reclaim(http: FakeHTTP) -> None:
    """`settled` is every attempt, not just the newest. A newer attempt can
    conclude while an older one is still going, and that older connector run is
    still holding the tenant."""
    still_going = _attempt(None, at="2026-08-21T00:10:00Z", check_id=50)
    http_stub = _attempts(still_going, OLD_FAIL)
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, http_stub))

    assert _resolve(run_id=1)[0] == "duplicate"


@pytest.mark.parametrize(
    "newest_first", [False, True], ids=["oldest-first", "newest-first"]
)
def test_the_newest_conclusion_decides_not_any_failure(
    http: FakeHTTP, newest_first
) -> None:
    """A SHA retried once keeps the old attempt's failed check. Folding the two
    together — any-failure, or last-in-listing-order — would re-trigger a leg
    that has since passed, on every subsequent event, forever."""
    listing = [NEW_PASS, OLD_FAIL] if newest_first else [OLD_FAIL, NEW_PASS]
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _attempts(*listing)))

    assert _resolve(run_id=1)[0] == "duplicate"


def test_a_settled_failure_is_not_reclaimed_while_the_claimant_is_live(
    http: FakeHTTP, capsys
) -> None:
    """A live claimant may be mid-re-run of its own dispatch job — ``is_my_run``
    lets that through — and that plus a reclaim here is two dispatches at one
    tenant. The notice hands the operator the one lever that IS safe."""
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _attempts(OLD_FAIL)))
    http.route("GET", "/actions/runs/999", (200, {"status": "in_progress"}))

    assert _resolve(run_id=1)[0] == "duplicate"

    printed = capsys.readouterr().out
    assert "gh run rerun 999" in printed
    assert "not --failed" in printed, (
        "a leg that dispatched fine and failed downstream leaves the dispatch job "
        "green, so --failed re-runs the gate alone and it re-reads the red check"
    )


def test_losing_the_race_to_reclaim_a_failure_does_not_dispatch(
    http: FakeHTTP,
) -> None:
    """Two duplicate runs can both see the same settled failure. The create is
    the compare-and-set, so exactly one may dispatch."""
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _attempts(OLD_FAIL)))
    http.route("GET", "/actions/runs/999", (200, {"status": "completed"}))
    _stub_reclaim_writes(http, wins_the_race=False)

    assert _resolve(run_id=1)[0] == "duplicate"


def test_a_concluded_check_with_no_conclusion_field_is_not_reclaimed(
    http: FakeHTTP,
) -> None:
    """Unreadable is not "failed". Reclaiming on a missing conclusion would make
    every malformed check run a re-dispatch."""
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _checks((CHECK, "completed"))))

    assert _resolve(run_id=1)[0] == "duplicate"


def test_an_unreadable_listing_never_reclaims(http: FakeHTTP) -> None:
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (500, {"message": "server error"}))

    with pytest.raises(GuardUnavailable):
        _resolve(run_id=1)


def test_the_guard_and_the_gate_order_check_runs_identically() -> None:
    """The two copies of check_run_age_key must agree, or the guard would judge a
    retry against a different attempt than the gate reports. Copies rather than a
    shared import because nothing in .github/scripts imports a sibling script;
    this is what stops them drifting."""
    table = [
        {"started_at": "2026-08-21T00:53:41Z", "id": 100},
        {"started_at": "2026-08-21T01:41:00Z", "id": 200},
        {"started_at": "2026-08-21T01:41:00Z", "id": 201},
        {"id": 5},
        {},
        {"started_at": None, "id": None},
    ]
    assert [check_run_age_key(e) for e in table] == [
        gate.check_run_age_key(e) for e in table
    ]
    assert _PASSING_CONCLUSIONS == set(
        gate.PASSING_CONCLUSIONS
    ), "the guard must not call retryable a conclusion the gate calls a pass"


def test_this_runs_own_claim_permits_a_re_dispatch(http: FakeHTTP) -> None:
    """Attempt is recorded for the log, not the decision: re-running the dispatch
    job is how an operator retries a transient dispatch failure, and a claim that
    blocked its own run's re-run would make that impossible."""
    _stub_taken(http, 555, attempt=1)

    state, blocker = _resolve(run_id=555, attempt=2)

    assert state == "claimed"
    assert blocker is None
    # No check-run probe and no re-write: it is already ours.
    assert http.count("GET", "/check-runs") == 0
    assert http.count("POST", "/git/refs") == 0


def test_a_live_claimer_that_has_not_dispatched_yet_still_blocks(
    http: FakeHTTP,
) -> None:
    # The window between a peer's claim and its check creation is one API call.
    # Reclaiming inside it is how one SHA gets two connector runs.
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _checks(("Some other check", "completed"))))
    http.route("GET", "/actions/runs/999", (200, {"status": "in_progress"}))

    assert _resolve(run_id=1)[0] == "duplicate"


def test_a_dead_claimer_that_never_dispatched_is_reclaimed(http: FakeHTTP) -> None:
    """Otherwise the slot stays taken by a run that will never dispatch, and this
    commit has no e2e at all — a silent loss of coverage, which is the failure
    mode this whole area exists to avoid."""
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _checks()))
    http.route("GET", "/actions/runs/999", (200, {"status": "completed"}))
    http.route("DELETE", "/git/refs/", (204, None))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (201, {"ref": REF}))

    assert _resolve(run_id=1) == ("reclaimed", None)


def test_losing_the_reclaim_race_does_not_dispatch(http: FakeHTTP) -> None:
    # Two contenders can both see a dead claimer. Exactly one may take over.
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _checks()))
    http.route("GET", "/actions/runs/999", (200, {"status": "completed"}))
    http.route("DELETE", "/git/refs/", (204, None))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (422, {"message": "Reference already exists"}))

    assert _resolve(run_id=1)[0] == "duplicate"


def test_an_unreadable_claim_record_fails_open(http: FakeHTTP) -> None:
    # Attributing an unreadable claim in either direction is worse than the
    # status quo, so it is not attributed at all.
    http.route("GET", "/git/ref/", (200, {"object": {"sha": BLOB, "type": "blob"}}))
    http.route("GET", "/git/blobs/", (200, {"content": "not base64 json"}))

    with pytest.raises(GuardUnavailable):
        _resolve(run_id=1)


def test_an_unreadable_check_list_fails_open(http: FakeHTTP) -> None:
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (500, {"message": "boom"}))

    with pytest.raises(GuardUnavailable):
        _resolve(run_id=1)


# --- liveness --------------------------------------------------------------


def test_a_missing_run_is_not_live(http: FakeHTTP) -> None:
    http.route("GET", "/actions/runs/7", (404, {"message": "Not Found"}))
    assert run_is_live(REPO, 7) is False


def test_an_unreadable_run_is_treated_as_live(http: FakeHTTP) -> None:
    """Errs towards live. Reading a transient failure as "over" reaps a live
    peer's claim and produces the duplicate this module exists to prevent."""
    http.route("GET", "/actions/runs/7", (500, {"message": "boom"}))
    assert run_is_live(REPO, 7) is True


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending"])
def test_every_non_completed_status_is_live(http: FakeHTTP, status: str) -> None:
    # An unfamiliar future status must err towards leaving a peer alone.
    http.route("GET", "/actions/runs/7", (200, {"status": status}))
    assert run_is_live(REPO, 7) is True


def test_the_dispatch_state_ignores_other_checks_on_the_sha(http: FakeHTTP) -> None:
    http.route(
        "GET",
        "/check-runs",
        (200, _checks(("SDK Gate", "completed"), ("Trivy", "completed"))),
    )
    state = read_dispatch_state(REPO, SHA, CHECK)
    assert state is not None and state.dispatched is False


# --- pruning ---------------------------------------------------------------


def test_an_open_pr_head_is_never_pruned(http: FakeHTTP) -> None:
    other = "a" * 40
    http.route(
        "GET",
        "/matching-refs/",
        (200, [{"ref": f"refs/e2e-dispatch/{APP}/{other}"}]),
    )
    http.route("GET", "/pulls?", (200, [{"head": {"sha": other}}]))

    assert prune(REPO, APP, SHA, CHECK) == 0


def test_the_sha_being_claimed_now_is_never_pruned(http: FakeHTTP) -> None:
    http.route("GET", "/matching-refs/", (200, [{"ref": REF}]))
    http.route("GET", "/pulls?", (200, []))

    assert prune(REPO, APP, SHA, CHECK) == 0


def test_a_settled_merge_queue_claim_is_pruned(http: FakeHTTP) -> None:
    """A merge-queue SHA is never an open PR head, which is what would otherwise
    make the namespace grow with every merge. Safe to forget once its check has
    concluded: no second dispatch event can arrive for it."""
    stale = "a" * 40
    http.route(
        "GET",
        "/matching-refs/",
        (200, [{"ref": f"refs/e2e-dispatch/{APP}/{stale}"}]),
    )
    http.route("GET", "/pulls?", (200, []))
    http.route("GET", "/check-runs", (200, _checks((CHECK, "completed"))))
    http.route("DELETE", "/git/refs/", (204, None))

    assert prune(REPO, APP, SHA, CHECK) == 1


def test_a_claim_whose_dispatch_is_still_running_is_not_pruned(
    http: FakeHTTP,
) -> None:
    stale = "a" * 40
    http.route(
        "GET",
        "/matching-refs/",
        (200, [{"ref": f"refs/e2e-dispatch/{APP}/{stale}"}]),
    )
    http.route("GET", "/pulls?", (200, []))
    http.route("GET", "/check-runs", (200, _checks((CHECK, "in_progress"))))

    assert prune(REPO, APP, SHA, CHECK) == 0


def test_a_check_less_claim_is_pruned_only_when_its_run_is_over(
    http: FakeHTTP,
) -> None:
    """The one case where "no check" must NOT mean "nothing is happening": a peer
    between its claim and its check creation. Pruning there would let a duplicate
    event for the same SHA dispatch a second time."""
    stale = "a" * 40
    ref = f"refs/e2e-dispatch/{APP}/{stale}"
    http.route("GET", "/matching-refs/", (200, [{"ref": ref}]))
    http.route("GET", "/pulls?", (200, []))
    http.route("GET", "/check-runs", (200, _checks()))
    http.route("GET", "/git/ref/", (200, {"object": {"sha": BLOB, "type": "blob"}}))
    http.route("GET", "/git/blobs/", (200, _claim_blob(999)))
    http.route("GET", "/actions/runs/999", (200, {"status": "in_progress"}))

    assert prune(REPO, APP, SHA, CHECK) == 0


def test_an_unreadable_pr_list_prunes_nothing(http: FakeHTTP) -> None:
    """None is not an empty set: an unreadable list must not license deleting
    every claim in the namespace."""
    http.route(
        "GET",
        "/matching-refs/",
        (200, [{"ref": f"refs/e2e-dispatch/{APP}/{'a' * 40}"}]),
    )
    http.route("GET", "/pulls?", (500, {"message": "boom"}))

    assert open_pr_heads(REPO) is None
    assert prune(REPO, APP, SHA, CHECK) == 0


def test_no_claims_for_this_app_is_not_an_error(http: FakeHTTP) -> None:
    # matching-refs is documented to answer with an empty array, but the older
    # single-ref endpoint 404s and the two have been confused before.
    http.route("GET", "/matching-refs/", (404, {"message": "Not Found"}))
    assert prune(REPO, APP, SHA, CHECK) == 0


def test_claim_is_settled_is_unknown_when_the_checks_are_unreadable(
    http: FakeHTTP,
) -> None:
    http.route("GET", "/check-runs", (500, {"message": "boom"}))
    assert claim_is_settled(REPO, REF, SHA, CHECK) is None


# --- the CLI: every failure direction is open ------------------------------


def _outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    return path


def _argv(**overrides) -> list[str]:
    args = {
        "--repo": REPO,
        "--sha": SHA,
        "--app": APP,
        "--check-run-name": CHECK,
        "--run-id": "1",
        "--run-attempt": "1",
    }
    args.update(overrides)
    return [item for pair in args.items() for item in pair]


def _read(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in path.read_text().splitlines() if "=" in line
    )


def test_a_claimed_slot_reports_claimed(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _outputs(tmp_path, monkeypatch)
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    http.route("GET", "/matching-refs/", (200, [{"ref": REF}]))
    http.route("GET", "/pulls?", (200, []))

    assert main(_argv()) == 0

    outputs = _read(out)
    assert outputs["claimed"] == "true"
    assert outputs["state"] == "claimed"
    assert outputs["claim-ref"] == REF


def test_a_duplicate_reports_not_claimed_and_names_the_holder(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _outputs(tmp_path, monkeypatch)
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _checks((CHECK, "in_progress"))))

    assert main(_argv()) == 0

    outputs = _read(out)
    assert outputs["claimed"] == "false"
    assert outputs["state"] == "duplicate"
    assert outputs["holder-run-id"] == "999"


def test_a_duplicate_never_prunes(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Housekeeping is the claimer's job. A run that is standing down should not
    also be deleting refs — and the unstubbed-request guard in FakeHTTP is what
    proves it does not."""
    _outputs(tmp_path, monkeypatch)
    _stub_taken(http, 999)
    http.route("GET", "/check-runs", (200, _checks((CHECK, "in_progress"))))

    assert main(_argv()) == 0
    assert http.count("GET", "/matching-refs/") == 0


@pytest.mark.parametrize(
    "failure",
    [
        _PERMISSION_DENIED,
        _RATE_LIMIT,
        (500, {"message": "boom"}),
    ],
)
def test_every_guard_failure_still_dispatches(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure
) -> None:
    """The load-bearing property. A guard that cannot be operated must not be
    able to withhold the one dispatch that was supposed to happen — the worst
    case for a broken guard is the pre-existing duplicate."""
    out = _outputs(tmp_path, monkeypatch)
    http.route("GET", "/git/ref/", failure)

    assert main(_argv()) == 0

    outputs = _read(out)
    assert outputs["claimed"] == "true"
    assert outputs["state"] == "disabled"


def test_a_transport_failure_still_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "e2e_dispatch_guard.run",
        lambda *_a, **_k: _completed("", returncode=60, stderr="SSL problem"),
    )
    out = _outputs(tmp_path, monkeypatch)

    assert main(_argv()) == 0
    assert _read(out)["claimed"] == "true"


def test_a_missing_token_still_dispatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = _outputs(tmp_path, monkeypatch)

    assert main(_argv()) == 0
    assert _read(out)["claimed"] == "true"


def test_a_malformed_sha_still_dispatches(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A caller bug. Blocking the dispatch would hide it behind a missing e2e run
    # rather than surfacing it.
    out = _outputs(tmp_path, monkeypatch)

    assert main(_argv(**{"--sha": "refs/heads/main"})) == 0
    assert _read(out)["claimed"] == "true"
    assert http.calls == []


# --- the other duplication: one PR, two head SHAs --------------------------
#
# The CAS is keyed on the SHA, so two live commits on one PR are two legitimate
# claims and both fan out. Nothing is duplicated in the CAS's terms; the cost
# lands a repo away, where the tenant lease queues the head commit behind the
# obsolete commit's entire install-plus-legs cycle (FND-696). These tests pin
# both directions: a superseded SHA must not reach a tenant, and every doubt
# about whether it IS superseded must still dispatch.

_HEAD = "d47789e0" + "1" * 32


def test_a_superseded_sha_does_not_dispatch(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _outputs(tmp_path, monkeypatch)
    http.route("GET", "/pulls/3322", (200, {"head": {"sha": _HEAD}}))

    assert main(_argv(**{"--pr-number": "3322"})) == 0

    outputs = _read(out)
    assert outputs["claimed"] == "false"
    assert outputs["state"] == "superseded"
    # No claim was taken, so naming one would read as a ref this run holds.
    assert outputs["claim-ref"] == ""
    # And it stands down completely: no CAS, no prune, no check-run read. The
    # unstubbed-request guard in FakeHTTP is what proves it — one call, total.
    assert len(http.calls) == 1


def test_the_head_sha_dispatches(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _outputs(tmp_path, monkeypatch)
    http.route("GET", "/pulls/3322", (200, {"head": {"sha": SHA}}))
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    http.route("GET", "/matching-refs/", (200, [{"ref": REF}]))
    http.route("GET", "/pulls?", (200, []))

    assert main(_argv(**{"--pr-number": "3322"})) == 0

    assert _read(out)["state"] == "claimed"


def test_a_head_sha_in_a_different_case_is_not_superseded(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One SHA in two spellings must not read as two commits — that would skip
    the head commit's own dispatch, the one failure worse than a stale run."""
    out = _outputs(tmp_path, monkeypatch)
    http.route("GET", "/pulls/3322", (200, {"head": {"sha": SHA.upper()}}))
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    http.route("GET", "/matching-refs/", (200, [{"ref": REF}]))
    http.route("GET", "/pulls?", (200, []))

    assert main(_argv(**{"--pr-number": "3322"})) == 0

    assert _read(out)["state"] == "claimed"


@pytest.mark.parametrize(
    "answer",
    [
        (404, {"message": "Not Found"}),
        (500, {"message": "boom"}),
        _PERMISSION_DENIED,
        _RATE_LIMIT,
        # Shape change: a 200 that says nothing about the head.
        (200, {"number": 3322}),
        (200, {"head": {"ref": "bump-version-main"}}),
    ],
)
def test_an_unreadable_pr_head_still_dispatches(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer
) -> None:
    """Unknown is not stale. Every doubt resolves towards dispatching, because a
    stale run costs tenant time and a wrongly-skipped head commit costs the PR
    its e2e outright."""
    out = _outputs(tmp_path, monkeypatch)
    http.route("GET", "/pulls/3322", answer)
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    http.route("GET", "/matching-refs/", (200, [{"ref": REF}]))
    http.route("GET", "/pulls?", (200, []))

    assert main(_argv(**{"--pr-number": "3322"})) == 0

    assert _read(out)["claimed"] == "true"


@pytest.mark.parametrize("number", ["", "  ", "not-a-number"])
def test_no_pr_number_skips_the_check_entirely(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number
) -> None:
    """The merge_group path. A queue entry's SHA is not any PR's head, so there
    is nothing for it to fall behind — and asking would spend an API call to
    learn that a 404 means "carry on"."""
    out = _outputs(tmp_path, monkeypatch)
    http.route("GET", "/git/ref/", (404, {"message": "Not Found"}))
    http.route("POST", "/git/blobs", (201, {"sha": BLOB}))
    http.route("POST", "/git/refs", (201, {"ref": REF}))
    http.route("GET", "/matching-refs/", (200, [{"ref": REF}]))
    http.route("GET", "/pulls?", (200, []))

    assert main(_argv(**{"--pr-number": number})) == 0

    assert _read(out)["state"] == "claimed"
    assert http.count("GET", "/pulls/3322") == 0


def test_the_stale_head_check_runs_before_the_claim(
    http: FakeHTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order, not preference. Claiming first and standing down after would leave
    a claim ref behind that no run will ever dispatch for, and the next
    contender on that SHA would have to reap it."""
    _outputs(tmp_path, monkeypatch)
    http.route("GET", "/pulls/3322", (200, {"head": {"sha": _HEAD}}))

    assert main(_argv(**{"--pr-number": "3322"})) == 0

    assert http.count("POST", "/git/refs") == 0
    assert http.count("POST", "/git/blobs") == 0


def test_the_token_never_reaches_curls_command_line(http: FakeHTTP) -> None:
    """Anything else on the runner can read /proc/<pid>/cmdline while a request
    is in flight, so the credential goes through stdin (-K -) instead."""
    http.route("GET", "/actions/runs/7", (200, {"status": "completed"}))
    run_is_live(REPO, 7)

    assert not any("x" == arg for arg in http.cmds[0])
    assert not any("Authorization" in arg for arg in http.cmds[0])
    assert http.inputs[0] is not None and "Authorization" in http.inputs[0]


def test_a_5xx_is_retried_before_giving_up(http: FakeHTTP, monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    http.route(
        "GET",
        "/actions/runs/7",
        (500, {"message": "boom"}),
        (200, {"status": "completed"}),
    )

    assert run_is_live(REPO, 7) is False
    assert http.count("GET", "/actions/runs/7") == 2


# --- the check-run listing is paginated ------------------------------------
#
# A single per_page=100 GET reports a named check living past page one as "never
# dispatched" — the one wrong answer that costs a tenant, because the reclaim
# path then fires a second connector run. This repo's SHAs already carry enough
# checks for that to be reachable, which is why poll_check_runs_gate.py
# paginates the same endpoint.


def _filler(count: int, name: str = "SDK Gate"):
    return [{"name": name, "status": "completed"} for _ in range(count)]


def test_a_named_check_past_page_one_is_found(http: FakeHTTP) -> None:
    http.route(
        "GET",
        "/check-runs",
        (200, {"total_count": 101, "check_runs": _filler(100)}),
        (
            200,
            {
                "total_count": 101,
                "check_runs": [{"name": CHECK, "status": "in_progress"}],
            },
        ),
    )
    assert checks_state(REPO, SHA, CHECK) == "running"
    assert http.count("GET", "/check-runs") == 2


def test_a_dispatch_recorded_past_page_one_is_not_reclaimed(http: FakeHTTP) -> None:
    """The failure this pagination exists to stop: a claim held by a dead run
    whose check lives on page two used to read as "never dispatched", and the
    reclaim path would dispatch a second connector run onto the same tenants."""
    _stub_taken(http, 999)
    http.route(
        "GET",
        "/check-runs",
        (200, {"total_count": 101, "check_runs": _filler(100)}),
        (
            200,
            {
                "total_count": 101,
                "check_runs": [{"name": CHECK, "status": "completed"}],
            },
        ),
    )

    state, blocker = _resolve(run_id=1)

    assert state == "duplicate"
    assert blocker is not None and blocker.run_id == 999
    # No reclaim: no ref deletion, no second CAS.
    assert http.count("DELETE", "/git/refs/") == 0
    assert http.count("POST", "/git/refs") == 0


def test_a_failed_later_page_is_unreadable_not_absent(http: FakeHTTP) -> None:
    # "absent" licenses a reclaim, so it may only be returned from a listing
    # that was read in full.
    http.route(
        "GET",
        "/check-runs",
        (200, {"total_count": 200, "check_runs": _filler(100)}),
        (500, {"message": "boom"}),
    )
    assert checks_state(REPO, SHA, CHECK) is None


def test_more_pages_than_the_cap_is_unreadable(http: FakeHTTP) -> None:
    http.route(
        "GET",
        "/check-runs",
        *[
            (200, {"total_count": 10_000, "check_runs": _filler(100)})
            for _ in range(11)
        ],
    )
    assert checks_state(REPO, SHA, CHECK) is None
    # Bounded: it stops at the cap rather than walking a pathological commit.
    assert http.count("GET", "/check-runs") == _CHECKS_MAX_PAGES


def test_a_listing_short_of_its_own_total_is_unreadable(http: FakeHTTP) -> None:
    http.route(
        "GET",
        "/check-runs",
        (200, {"total_count": 200, "check_runs": _filler(100)}),
        (200, {"total_count": 200, "check_runs": []}),
    )
    assert checks_state(REPO, SHA, CHECK) is None


def test_a_single_short_page_is_one_call(http: FakeHTTP) -> None:
    # The normal case must not pay for pagination it does not need.
    http.route("GET", "/check-runs", (200, _checks((CHECK, "completed"))))
    assert checks_state(REPO, SHA, CHECK) == "done"
    assert http.count("GET", "/check-runs") == 1


def test_a_listing_without_a_total_count_still_terminates(http: FakeHTTP) -> None:
    # total_count is not load-bearing: a short page is the fallback end-of-list
    # signal, so a response shape change cannot make this spin.
    http.route("GET", "/check-runs", (200, {"check_runs": _filler(3)}))
    assert checks_state(REPO, SHA, CHECK) == "absent"
    assert http.count("GET", "/check-runs") == 1


def test_several_named_checks_are_only_done_when_all_are(http: FakeHTTP) -> None:
    """Duplicate dispatches are exactly what put two same-named checks on one
    SHA, so the prune's "settled" question is about the whole set."""
    http.route(
        "GET",
        "/check-runs",
        (200, _checks((CHECK, "completed"), (CHECK, "in_progress"))),
    )
    assert checks_state(REPO, SHA, CHECK) == "running"


# --- wiring: the action and its caller -------------------------------------


@pytest.fixture(scope="module")
def action() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(_ACTION.read_text(encoding="utf-8"))


def _step(action: dict, name_fragment: str) -> dict:  # type: ignore[type-arg]
    for step in action["runs"]["steps"]:
        if name_fragment in str(step.get("name", "")):
            return step
    raise AssertionError(f"e2e-apps has no step matching {name_fragment!r}")


def test_the_guard_runs_before_the_check_run_is_created(action: dict) -> None:  # type: ignore[type-arg]
    """Order is the point. A duplicate run that created its own in_progress
    check on the SHA would leave a check nothing completes — the connector's
    callback completes the winner's — and it would hang until the watchdog."""
    names = [str(step.get("name", "")) for step in action["runs"]["steps"]]
    guard = next(i for i, n in enumerate(names) if "Claim the dispatch slot" in n)
    create = next(i for i, n in enumerate(names) if "Create pending check run" in n)
    dispatch = next(
        i for i, n in enumerate(names) if n == "Dispatch connector workflow"
    )
    assert guard < create < dispatch


def test_the_guard_invokes_the_script_at_a_real_path(action: dict) -> None:  # type: ignore[type-arg]
    step = _step(action, "Claim the dispatch slot")
    assert "e2e_dispatch_guard.py" in step["run"]
    assert (_REPO_ROOT / ".github/scripts/e2e_dispatch_guard.py").is_file()


def test_the_guard_cannot_fail_the_dispatch(action: dict) -> None:  # type: ignore[type-arg]
    """Both halves of failing open. `continue-on-error` keeps a crashed guard
    from ending the job before the dispatch, and `!= 'false'` keeps the empty
    output a crashed step leaves behind from reading as "somebody else has it"."""
    guard = _step(action, "Claim the dispatch slot")
    assert guard["continue-on-error"] is True

    for name in ("Create pending check run", "Dispatch connector workflow"):
        condition = _step(action, name)["if"]
        assert "steps.dispatch_guard.outputs.claimed != 'false'" in condition, (
            f"{name} must proceed unless the guard positively reported a "
            "duplicate; anything else fails closed and silently drops e2e"
        )


def test_the_guard_is_callback_mode_only(action: dict) -> None:  # type: ignore[type-arg]
    """Not a scope decision. In poll mode THIS job's conclusion is the verdict,
    so a skipped dispatch would be a vacuous green that can mask the winner's
    real result on the same SHA. In callback mode the verdict is the check run,
    which the duplicate run's own connector gate reads."""
    assert (
        "inputs.wait-mode == 'callback'"
        in _step(action, "Claim the dispatch slot")["if"]
    )


def test_the_guard_uses_this_repos_own_token(action: dict) -> None:  # type: ignore[type-arg]
    # The claim refs live in THIS repo, next to the SHA they key on. The fleet
    # App tokens are scoped for other jobs: github-token to the connector,
    # checks-app-token to check ownership.
    env = _step(action, "Claim the dispatch slot")["env"]
    assert env["GH_TOKEN"] == "${{ github.token }}"
    assert env["REPO"] == "${{ github.repository }}"
    assert env["SHA"] == "${{ inputs.check-sha }}"
    assert env["NAME"] == "${{ inputs.check-run-name }}"


def test_the_pr_number_comes_from_the_event(action: dict) -> None:  # type: ignore[type-arg]
    """Two properties in one expression. It must come from the EVENT, not an
    input, or a caller could name a PR whose head has nothing to do with
    `check-sha` — and the guard would then skip against a stranger's commit. And
    it is empty on the merge_group path, which is exactly what disables the
    stale-head check for a queue entry that has no PR head to fall behind."""
    step = _step(action, "Claim the dispatch slot")
    assert step["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert "--pr-number" in step["run"], (
        "the env var alone does nothing; without the flag the stale-head check "
        "is silently off and every superseded commit fans out again"
    )


def test_the_caller_grants_what_the_guard_needs() -> None:
    """A job-level permissions block REPLACES the workflow default rather than
    merging with it, so a missing grant here does not fail loudly — the guard
    just fails open and every duplicate dispatches again, which is invisible."""
    jobs = yaml.safe_load(_PR_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    permissions = jobs["connector-tests"]["permissions"]
    assert permissions["contents"] == "write", "claim refs are created and deleted"
    assert permissions["checks"] == "read", "has this SHA already been dispatched?"
    assert permissions["pull-requests"] == "read", "the stale-claim prune"
    # Without this, GET /actions/runs/<id> 404s the way a deleted run does — so a
    # LIVE claimant reads as dead, its claim is reclaimed, and a second connector
    # run lands on the same tenants. The sibling lease-tenant job grants it too.
    assert permissions["actions"] == "read", (
        "run liveness decides whether a claimant that has not dispatched yet is "
        "mid-dispatch or dead; a permission-shaped 404 reads as dead"
    )


def test_the_guard_keys_on_the_same_sha_as_the_check_run() -> None:
    """The claim, the check run and the connector gate's poll must all name one
    SHA. If the claim keyed on anything else, the "already dispatched" evidence
    would be looked for on a commit that never had it."""
    jobs = yaml.safe_load(_PR_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    step = next(
        s for s in jobs["connector-tests"]["steps"] if "e2e-apps" in str(s.get("uses"))
    )
    assert step["with"]["check-sha"] == (
        "${{ github.event_name == 'merge_group' && github.sha "
        "|| github.event.pull_request.head.sha }}"
    )


def test_the_claimant_dataclass_ignores_the_attempt_for_ownership() -> None:
    # Spelled out because the opposite reading is tempting and would break the
    # only supported way to retry a dispatch.
    assert Claimant(run_id=7, attempt=1, claimed_at=None).is_my_run(7)
    assert not Claimant(run_id=7, attempt=1, claimed_at=None).is_my_run(8)
