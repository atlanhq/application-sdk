"""Unit tests for the AE starter (child G, FND-243).

**A differential, unlike its sibling.** The queue starter's tests are claims,
because nothing dispatched onto a task queue before it. This one lifts
``BaseE2ETest._bootstrap_workflow`` plus the submit half of ``run_full_dag``, so
the thing under test is a *sequence that already existed* and the tests can pin
it against what that sequence did.

Every step therefore goes through the real
:class:`~application_sdk.testing.harness.automation_engine.AEClient` with only
its ``_request`` scripted. That is deliberate rather than convenient: the
starter's whole content is the order of four calls and what it does with their
answers, so a double that replaced the client would assert the starter against a
model of AE rather than against AE's own parsers — and every response-shape
decision (which envelope the slug is read from, which field carries the version,
what an empty ``run_id`` means) would stop being exercised.

The two properties most worth having:

* **The version that is published is the version AE assigned**, not the one the
  starter asked for. AE's create returns the number it chose; publishing the
  requested one 404s on a mismatch, and worse, leaves the run comparing its seed
  against a version it never published — which is the read that decides whether
  the tenant's own manifest superseded the harness'.
* **A pre-existing slug is never seeded over.** The DAG under it belongs to
  whoever set it up, and a run that replaced it would break the *next* run
  against that slug rather than its own.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from application_sdk.testing.harness.automation_engine._errors import AtlanApiHttpError
from application_sdk.testing.harness.automation_engine.client import AEClient
from application_sdk.testing.harness.identity import Minter
from application_sdk.testing.harness.starters import (
    AERunHandle,
    AEWorkflowSpec,
    SubmitRetry,
    start_via_automation_engine,
)

_SLEEP = "application_sdk.testing.harness.automation_engine.client.sleep_async"
_SEED_DAG: dict[str, object] = {"nodes": {"extract": {"queue": "atlan-postgres-prod"}}}
_PAYLOAD: dict[str, object] = {"metadata": {"entrypoint": "crawler"}}


class _AE:
    """Scripted AE, recording every request the starter makes.

    Routes on the path rather than on call ordering, so a reordering of the
    sequence changes what the recorder sees rather than passing by coincidence.
    """

    def __init__(
        self,
        *,
        slug: str = "pg-e2e-1",
        assigned_version: int = 1_700_000_099,
        run_id: str = "run-abc",
        overrides: dict[str, tuple[int, Any]] | None = None,
    ) -> None:
        self.slug = slug
        self.assigned_version = assigned_version
        self.run_id = run_id
        self._overrides = overrides or {}
        self.calls: list[tuple[str, str, Any]] = []

    async def request(
        self, method: str, path: str, *, body: Any = None, **_kwargs: Any
    ) -> tuple[int, Any]:
        self.calls.append((method, path, body))
        for fragment, response in self._overrides.items():
            if fragment in path:
                return response
        if path.endswith("/versions") and method == "POST":
            return 200, {"data": {"version": self.assigned_version}}
        if path.endswith("/publish"):
            return 200, {"status": "success"}
        if "/versions?" in path or path.endswith("/versions?page=0&page_size=1"):
            return 200, {"data": [{"version": 1}]}
        if "package-workflows" in path:
            return 200, {"data": {"run_id": self.run_id}}
        if path.endswith("/workflows"):
            return 200, {"data": {"slug": self.slug}}
        return 200, {"data": []}

    @property
    def paths(self) -> list[str]:
        return [path for _method, path, _body in self.calls]

    def body_for(self, fragment: str, *, method: str = "POST") -> Any:
        """The body of the first *method* request whose path contains *fragment*.

        The method filter is load-bearing rather than tidy: ``/versions`` is the
        create *and* the slug-resolve GET beside it, and matching the GET returns
        a ``None`` body that reads as "the starter sent nothing".
        """
        for called_method, path, body in self.calls:
            if called_method == method and fragment in path:
                return body
        raise AssertionError(f"no {method} matched {fragment!r}: {self.paths}")


def _minter(second: int = 1_700_000_000) -> Minter:
    """A minter whose numbers are a function of its inputs, so they can be pinned."""
    return Minter(clock=lambda: second, randbelow=lambda _bound: 42)


def _spec(**overrides: Any) -> AEWorkflowSpec:
    return AEWorkflowSpec(
        **{
            "name": "postgres-e2e-ci-1700000000",
            "seed_dag": _SEED_DAG,
            "payload": _PAYLOAD,
            **overrides,
        }
    )


async def _start(ae: _AE, spec: AEWorkflowSpec, **kwargs: Any) -> AERunHandle:
    client = AEClient("https://tenant.example.com", "tok")
    with patch.object(client, "_request", side_effect=ae.request):
        return await start_via_automation_engine(spec, client=client, **kwargs)


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------


async def test_the_four_writes_happen_in_the_order_ae_requires():
    """Create, then resolve, then seed, then publish, then submit.

    Not an arbitrary order: AE 404s a submit against a slug with no *published*
    version, and 404s a version create against a slug it has not indexed yet.
    """
    ae = _AE()

    await _start(ae, _spec(), minter=_minter())

    assert ae.paths == [
        "/automation/api/v1/workflows",
        "/automation/api/v1/workflows/pg-e2e-1/versions?page=0&page_size=1",
        "/automation/api/v1/workflows/pg-e2e-1/versions",
        "/automation/api/v1/workflows/pg-e2e-1/versions/1700000099/publish",
        "/api/service/package-workflows?submit=true",
    ]


async def test_the_handle_names_the_run_the_slug_and_the_seed():
    ae = _AE()

    handle = await _start(ae, _spec(), minter=_minter())

    assert handle == AERunHandle(
        workflow_slug="pg-e2e-1", run_id="run-abc", seed_version=1_700_000_099
    )


async def test_the_seed_dag_is_sent_verbatim():
    """AE's submit republishes the tenant's own manifest over this, so the seed's
    job is only to make the workflow submittable — which means the harness has no
    business rewriting it on the way through."""
    ae = _AE()

    await _start(ae, _spec(), minter=_minter())

    assert ae.body_for("/versions") == {"version": 1_700_000_000, "dag": _SEED_DAG}


async def test_the_payload_is_sent_verbatim():
    ae = _AE()

    await _start(ae, _spec(), minter=_minter())

    assert ae.body_for("package-workflows") == _PAYLOAD


# ---------------------------------------------------------------------------
# The version, which is where a plausible implementation goes wrong
# ---------------------------------------------------------------------------


async def test_the_published_version_is_the_one_ae_assigned():
    """AE chooses the number; the request is a suggestion.

    Publishing the *requested* number instead 404s whenever AE picked another —
    and on the runs where it silently did not, the handle would name a version
    the harness never published, so the supersede check would compare its seed
    against a version that does not exist.
    """
    ae = _AE(assigned_version=1_700_000_555)

    handle = await _start(ae, _spec(version=1_700_000_000), minter=_minter())

    assert "/versions/1700000555/publish" in ae.paths[3]
    assert handle.seed_version == 1_700_000_555


async def test_an_explicit_version_is_what_gets_requested():
    ae = _AE()

    await _start(ae, _spec(version=99), minter=_minter())

    assert ae.body_for("/versions")["version"] == 99


async def test_the_minted_version_is_the_clock_not_the_ci_run_id():
    """``GITHUB_RUN_ID`` is constant across every leg of one job, so minting from
    it would make a second seed in that job indistinguishable from the first —
    and the supersede check reads the second as the tenant's own manifest."""
    ae = _AE()
    minter = Minter(
        clock=lambda: 1_700_000_000, randbelow=lambda _b: 42, run_id_env="42"
    )

    await _start(ae, _spec(), minter=minter)

    assert ae.body_for("/versions")["version"] == 1_700_000_000


async def test_no_minter_still_starts():
    """The default builds one over the real clock, so a caller with no identity
    concerns does not have to construct one to dispatch."""
    ae = _AE()

    handle = await _start(ae, _spec())

    assert handle.run_id == "run-abc"
    assert handle.seed_version == 1_700_000_099


# ---------------------------------------------------------------------------
# The pre-existing slug
# ---------------------------------------------------------------------------


async def test_a_pre_existing_slug_is_submitted_against_and_never_seeded_over():
    """One request, and it is the submit. Nothing is created and nothing is
    published: the DAG under that slug is not this run's to replace."""
    ae = _AE()

    handle = await _start(ae, _spec(slug="someone-elses"), minter=_minter())

    assert ae.paths == ["/api/service/package-workflows?submit=true"]
    assert handle.workflow_slug == "someone-elses"


async def test_a_pre_existing_slug_reports_no_seed_version():
    """``None`` rather than a number, because nothing was published — which is
    exactly the branch ``_supersedes`` treats as "there is nothing for AE to have
    superseded"."""
    ae = _AE()

    handle = await _start(ae, _spec(slug="someone-elses"), minter=_minter())

    assert handle.seed_version is None


# ---------------------------------------------------------------------------
# The submit's budget
# ---------------------------------------------------------------------------


async def test_the_submit_retry_is_passed_through_when_one_is_given():
    """Sized for a pod cold start rather than for AE overload, and applied to the
    submit's *own* retry loop — a second loop around it would re-enter the
    non-idempotent POST past the credential-name rotation that makes it safe."""
    ae = _AE(overrides={"package-workflows": (503, {"error": "cold"})})
    client = AEClient("https://tenant.example.com", "tok")

    with (
        patch.object(client, "_request", side_effect=ae.request),
        patch(_SLEEP) as sleep,
        pytest.raises(AtlanApiHttpError),
    ):
        await start_via_automation_engine(
            _spec(submit_retry=SubmitRetry(retries=3, sleep_seconds=7)),
            client=client,
            minter=_minter(),
        )

    submits = [path for path in ae.paths if "package-workflows" in path]
    assert len(submits) == 4  # the initial attempt plus three retries
    assert [call.args[0] for call in sleep.call_args_list] == [7, 7, 7]


async def test_without_a_submit_retry_the_clients_own_default_applies():
    ae = _AE(overrides={"package-workflows": (503, {"error": "cold"})})
    client = AEClient("https://tenant.example.com", "tok")

    with (
        patch.object(client, "_request", side_effect=ae.request),
        patch(_SLEEP),
        pytest.raises(AtlanApiHttpError),
    ):
        await start_via_automation_engine(_spec(), client=client, minter=_minter())

    submits = [path for path in ae.paths if "package-workflows" in path]
    assert len(submits) == 5  # submit_workflow's documented retries=4 default


def test_the_cold_start_sizing_calls_the_canonical_derivation():
    """Asserted against the shared function rather than against literals, so this
    cannot pass while the harness has grown a second copy of the arithmetic."""
    from application_sdk.testing.harness.automation_engine import (
        cold_start_submit_kwargs,
    )

    sizing = cold_start_submit_kwargs(600, 10)
    retry = SubmitRetry.for_cold_start(timeout_seconds=600, poll_interval_seconds=10)

    assert retry is not None
    assert retry.retries == sizing["retries"]
    assert retry.sleep_seconds == sizing["retry_sleep_seconds"]


def test_a_disabled_cold_start_budget_is_no_sizing_at_all():
    """``None``, not ``SubmitRetry(0, 0)``: zero retries at a zero gap is a
    *different* instruction from "leave the submit's own defaults alone", and the
    lifted code spelled the second one as an empty kwargs dict."""
    assert (
        SubmitRetry.for_cold_start(timeout_seconds=0, poll_interval_seconds=10) is None
    )


# ---------------------------------------------------------------------------
# What is deliberately not converted
# ---------------------------------------------------------------------------


async def test_an_ae_failure_arrives_as_ae_own_leaf():
    """Not wrapped in a starter-specific error. An operator already knows how to
    read ``AtlanApiHttpError``, and a second classification of the same responses
    is the divergence this project exists to remove."""
    ae = _AE(overrides={"/workflows": (500, {"error": "nope"})})
    client = AEClient("https://tenant.example.com", "tok")

    with (
        patch.object(client, "_request", side_effect=ae.request),
        patch(_SLEEP),
        pytest.raises(AtlanApiHttpError) as caught,
    ):
        await start_via_automation_engine(_spec(), client=client, minter=_minter())

    assert "create_workflow" in str(caught.value)


async def test_the_client_is_not_closed():
    """The transport's lifetime stays with whoever opened it — a caller that goes
    on to poll ``native-status`` through the same client must not find it shut."""
    ae = _AE()
    client = AEClient("https://tenant.example.com", "tok")
    closed: list[bool] = []

    with (
        patch.object(client, "_request", side_effect=ae.request),
        patch.object(client, "aclose", side_effect=lambda: closed.append(True)),
    ):
        await start_via_automation_engine(_spec(), client=client, minter=_minter())

    assert closed == []
