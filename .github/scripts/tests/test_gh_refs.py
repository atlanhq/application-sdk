"""Tests for .github/scripts/_gh_refs.py — the one ref transport (FND-2674).

Two things are pinned here that the per-caller suites cannot pin.

The first is that there is only ONE copy. `_gh_refs` was extracted from the
tenant lease for the DataForge refcount, and the extraction dropped the
FND-702 rate-limit/permission split on the way out — reintroducing verbatim the
conflation that lets a lease disable itself under contention, caught in review
rather than by a test. A copy is the failure mode, so the copy count is what gets
asserted.

The second is the handful of places where the two copies had DRIFTED and had to
be reconciled onto one answer. Those are the points where a reader would
reasonably expect either behaviour, so each one says which was chosen and why.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from _gh_refs import (  # noqa: E402
    Holder,
    _timestamp,
    holder_is_live,
    read_ref_target,
    slug,
)

_SCRIPTS = Path(__file__).resolve().parent.parent
REPO = "atlanhq/atlan-example-app"

#: The transport's defining functions. A module that declares one of these at top
#: level is a second implementation, whatever it is named.
_TRANSPORT_FUNCTIONS = frozenset(
    {"gh_request", "_parse_http", "_rate_limited", "holder_is_live", "_retry_after"}
)

#: Modules allowed to carry their own copy, each with the reason it is not simply
#: an import. Both are the `e2e-dispatch` claim guard, whose failure posture is
#: the OPPOSITE of the lease's: an unusable guard dispatches anyway
#: (`GuardUnavailable`), where an unusable lease fails the job (`SystemExit`), so
#: sharing this module means parameterising what its failures raise. That is its
#: own change; this test exists so a THIRD posture cannot arrive unnoticed.
_KNOWN_COPIES = {"e2e_dispatch_guard.py"}


def _top_level_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_the_ref_transport_has_exactly_one_definition() -> None:
    """A second copy is how the FND-702 split got dropped. Import it instead."""
    offenders = sorted(
        path.name
        for path in _SCRIPTS.glob("*.py")
        if path.name not in {"_gh_refs.py"} | _KNOWN_COPIES
        and _top_level_functions(path) & _TRANSPORT_FUNCTIONS
    )
    assert not offenders, (
        f"{', '.join(offenders)} define(s) the ref transport rather than importing "
        "_gh_refs. Two copies is how the rate-limit/permission split (FND-702) got "
        "dropped from one of them; add to _KNOWN_COPIES only with the reason a "
        "shared implementation cannot serve it."
    )


def test_the_lease_imports_the_transport_rather_than_defining_it() -> None:
    """The lease is the copy FND-2674 removed, so it is asserted by name — a
    revert that re-inlines the transport must not be able to pass."""
    lease = _SCRIPTS / "e2e_tenant_lease.py"
    assert not _top_level_functions(lease) & _TRANSPORT_FUNCTIONS
    assert "from _gh_refs import" in lease.read_text(encoding="utf-8")


# --- the reconciled points -------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["ci/e2e cratedb.2n", "openapi", "aws", "", "My App/Name", "a..b", "thing.lock"],
)
def test_slug_is_the_key_both_callers_already_use(value: str) -> None:
    """The two copies disagreed about ".", and the narrower set won because it
    renames nothing: the DataForge pin id contains a dot, no app or cloud name
    does, so dropping "." leaves both sides' live ref names byte-identical.
    Keeping it would have moved the refcount's prefix under in-flight runs.

    Asserted as properties rather than as a table of outputs, because what has to
    hold is that the result is a legal single ref component.
    """
    result = slug(value)
    assert result
    assert ".." not in result
    assert not result.endswith(".lock")
    assert not result.startswith("-")
    assert "/" not in result
    assert result == result.lower()


def test_slug_keeps_the_dotted_dataforge_pin_id_as_it_is_today() -> None:
    # The one live key that the "." decision actually moved. Pinned as a literal
    # so a widened char set cannot silently re-point the refcount's prefix.
    assert slug("ci/e2e cratedb.2n") == "ci-e2e-cratedb-2n"


def test_a_naive_timestamp_is_read_as_utc_not_runner_local() -> None:
    """The runner can be in any zone. Reading a UTC instant as local shifts the
    run's start by hours, in the direction that makes a hold look LONGER than it
    was — which is the direction that breaks a live holder."""
    assert _timestamp("2026-01-01T00:00:00") == _timestamp("2026-01-01T00:00:00Z")


def test_an_epoch_number_is_accepted_too() -> None:
    assert _timestamp(1700000000) == 1700000000.0


@pytest.mark.parametrize("value", ["", "not-a-date", None, True, {}])
def test_an_unparseable_timestamp_is_no_timestamp(value: object) -> None:
    assert _timestamp(value) is None


class _FakeHTTP:
    """Replays curl-shaped answers, keyed by a fragment of the URL."""

    def __init__(self, routes: dict[str, tuple[int, object]]) -> None:
        self.routes = routes

    def __call__(self, cmd: list[str], **kwargs):
        url = cmd[-1]
        for fragment, (status, body) in self.routes.items():
            if fragment in url:
                payload = "" if body is None else json.dumps(body)
                return _Completed(f"HTTP/2 {status}\r\n\r\n{payload}")
        raise AssertionError(f"unstubbed request: {url}")


class _Completed:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0
        self.stderr = ""


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_TOKEN", "x")

    def install(routes: dict[str, tuple[int, object]]) -> None:
        monkeypatch.setattr("_gh_refs.run", _FakeHTTP(routes))

    return install


def test_a_run_with_no_created_at_is_left_alone_by_the_ttl(http, capsys) -> None:
    """The stricter of the two variants, taken from the lease.

    A run object with no created_at should never happen, so it means something
    changed underneath us — and the TTL cannot be evaluated without it, because
    there is nothing to sanity-check the holder's own clock against. Applying the
    TTL anyway would break a LIVE holder on a malformed API answer; skipping it
    costs a waiter one more poll.
    """
    http({"/actions/runs/": (200, {"status": "in_progress"})})
    holder = Holder(run_id=7, attempt=1, acquired_at=0.0)
    assert holder_is_live(REPO, holder, ttl_seconds=60, now=1e9) is True
    assert "breaking it" not in capsys.readouterr().out


def test_the_ttl_still_breaks_a_hold_past_it(http) -> None:
    http({"/actions/runs/": (200, {"status": "in_progress", "created_at": 1000.0})})
    holder = Holder(run_id=7, attempt=1, acquired_at=2000.0)
    assert holder_is_live(REPO, holder, ttl_seconds=60, now=3000.0) is False


def test_an_acquisition_before_its_own_run_is_not_trusted(http) -> None:
    # A clock far out of step would otherwise report an enormous hold and break a
    # live holder's lease on the very first poll.
    http(
        {
            "/actions/runs/": (
                200,
                {"status": "in_progress", "created_at": "2026-01-01T00:00:00Z"},
            )
        }
    )
    holder = Holder(run_id=7, attempt=1, acquired_at=0.0)
    assert holder_is_live(REPO, holder, ttl_seconds=1, now=1e9) is True


def test_read_ref_target_reports_an_absent_ref_as_unheld(http) -> None:
    http({"/git/ref/": (404, {"message": "Not Found"})})
    assert read_ref_target(REPO, "refs/thing/holder") is None


def test_read_ref_target_returns_the_sha_a_ref_points_at(http) -> None:
    http({"/git/ref/": (200, {"object": {"sha": "b" * 40, "type": "blob"}})})
    assert read_ref_target(REPO, "refs/thing/holder") == "b" * 40


def test_read_ref_target_reads_an_unreadable_ref_as_unheld(http) -> None:
    """None conflates "nobody holds it" with "we could not tell" on purpose: for a
    caller racing a CAS both mean "try the CAS", and the CAS is the authority.

    A 4xx rather than a 5xx so the transport's own retry budget (which sleeps)
    stays out of it; the branch under test is the same either way."""
    http({"/git/ref/": (403, {"message": "Resource not accessible by integration"})})
    assert read_ref_target(REPO, "refs/thing/holder") is None
