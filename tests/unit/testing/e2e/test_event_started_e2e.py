"""Unit tests for EventStartedE2ETest (FND-5375)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from application_sdk.testing.e2e import (
    EventStartedE2ETest,
    SystemAppE2ETest,
    TenantPool,
    trigger_workflow_slug,
)
from application_sdk.testing.e2e._errors import (
    EventGatewayRejectedError,
    EventRunNotStartedError,
    EventTriggerNotInstalledError,
    HarnessMethodNotImplementedError,
    MissingHarnessClassAttrError,
    TenantPoolMismatchError,
)
from application_sdk.testing.e2e.tenant_pool import TENANT_POOL_ENV
from application_sdk.testing.harness.automation_engine.retry import RunLookup
from application_sdk.testing.harness.automation_engine.wire import PublishedVersion
from tests.unit.testing._ae_fakes import FakeAE

_SLUG = "example-event-sync"


class _FakeAE(FakeAE):
    """FakeAE whose ``slug_resolves`` answer a test can script."""

    def __init__(self, *args: Any, resolves: bool | None = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._resolves = resolves

    async def slug_resolves(self, slug: str) -> bool | None:
        self._record("slug_resolves", locals())
        return self._resolves


class _EventSuite(EventStartedE2ETest):
    connector_short_name = "example"
    trigger_entrypoint = "event-sync"
    connection_admin_roles = ("admin-role-guid",)
    event_run_timeout_seconds = 120
    event_run_poll_seconds = 5

    def __init__(self) -> None:
        self.sent_at: list[datetime] = []

    async def send_event(self) -> None:
        self.sent_at.append(datetime.now(timezone.utc))


class _SystemEventSuite(_EventSuite, SystemAppE2ETest):
    pass


@pytest.fixture
def tenant_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("ATLAN_BASE_URL", "https://test.example.invalid")
    monkeypatch.setenv("ATLAN_API_KEY", "test-token")
    monkeypatch.setenv("GITHUB_RUN_ID", "9999999")
    monkeypatch.delenv(TENANT_POOL_ENV, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# trigger_workflow_slug mirrors Local Marketplace's workflow_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("app", "entrypoint", "expected"),
    [
        pytest.param("openlineage", "generic-lineage", "openlineage-generic-lineage"),
        pytest.param("atlan-auth", "event-sync", "atlan-auth-event-sync"),
        pytest.param("My App", "check_run_sync", "my-app-check-run-sync"),
        pytest.param("app.", ".entry", "app-entry", id="dots-collapse-to-one-hyphen"),
        pytest.param("a@b", "c!", "ab-c", id="other-characters-are-dropped"),
        pytest.param("x" * 70, "y", "x" * 63, id="truncated-to-63"),
        pytest.param("x" * 62, "y", "x" * 62, id="no-trailing-hyphen-after-truncation"),
    ],
)
def test_slug_matches_the_local_marketplace_rule(
    app: str, entrypoint: str, expected: str
) -> None:
    assert trigger_workflow_slug(app, entrypoint) == expected


def test_a_pinned_slug_wins_over_the_derived_one() -> None:
    class _Pinned(_EventSuite):
        ae_workflow_slug = "pinned-slug"

    assert _Pinned().trigger_workflow_slug() == "pinned-slug"
    assert _EventSuite().trigger_workflow_slug() == _SLUG


# ---------------------------------------------------------------------------
# Setup: required attributes and tenant pool
# ---------------------------------------------------------------------------


def test_runs_on_the_connector_pool_without_argo_names(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    harness = _EventSuite()
    harness.setup_method()
    assert type(harness)._tenant_pool is TenantPool.CONNECTOR


def test_requires_the_trigger_entrypoint(tenant_env: pytest.MonkeyPatch) -> None:
    class _NoEntrypoint(EventStartedE2ETest):
        connector_short_name = "example"

    with pytest.raises(MissingHarnessClassAttrError) as exc:
        _NoEntrypoint().setup_method()
    assert exc.value.field == "trigger_entrypoint"


def test_is_refused_on_the_system_pool_by_itself(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    tenant_env.setenv(TENANT_POOL_ENV, "system")
    with pytest.raises(TenantPoolMismatchError):
        _EventSuite().setup_method()


def test_a_system_app_suite_runs_on_the_system_pool(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    tenant_env.setenv(TENANT_POOL_ENV, "system")
    harness = _SystemEventSuite()
    harness.setup_method()
    assert type(harness)._tenant_pool is TenantPool.SYSTEM


# ---------------------------------------------------------------------------
# Bootstrap: resolve the tenant's trigger workflow, publish nothing
# ---------------------------------------------------------------------------


async def test_bootstrap_resolves_the_slug_and_writes_nothing() -> None:
    harness = _EventSuite()
    fake = _FakeAE(published=PublishedVersion(version=3, dag={}))
    harness._ae = fake  # type: ignore[assignment]

    assert await harness._bootstrap_workflow() == _SLUG
    assert fake.methods == ["slug_resolves", "get_published_version"]
    assert fake.args("slug_resolves", "slug") == [_SLUG]


async def test_bootstrap_fails_before_sending_when_the_workflow_is_absent() -> None:
    harness = _EventSuite()
    fake = _FakeAE(resolves=False)
    harness._ae = fake  # type: ignore[assignment]

    with pytest.raises(EventTriggerNotInstalledError, match=_SLUG):
        await harness._bootstrap_workflow()
    assert harness.sent_at == []


async def test_bootstrap_carries_on_when_the_slug_read_is_inconclusive() -> None:
    harness = _EventSuite()
    harness._ae = _FakeAE(resolves=None)  # type: ignore[assignment]

    assert await harness._bootstrap_workflow() == _SLUG


# ---------------------------------------------------------------------------
# Submit: send the event, adopt the run
# ---------------------------------------------------------------------------


async def test_submit_sends_then_adopts_the_run_created_since() -> None:
    harness = _EventSuite()
    fake = _FakeAE(run_lookup=RunLookup(run_id="run-from-event", conclusive=True))
    harness._ae = fake  # type: ignore[assignment]

    before = datetime.now(timezone.utc)
    run_id = await harness._submit({"envelope": "unused"}, slug=_SLUG)

    assert run_id == "run-from-event"
    assert len(harness.sent_at) == 1
    # Nothing is submitted: the only AE call is the read.
    assert fake.methods == ["find_run_created_since"]
    (call,) = fake.calls_to("find_run_created_since")
    assert call["slug"] == _SLUG
    assert call["timeout_seconds"] == 120
    assert call["interval_seconds"] == 5
    # The floor is taken before the send, so a run the event starts at once is
    # never older than it.
    assert before <= call["since"] <= harness.sent_at[0]


async def test_submit_raises_an_assertion_when_ae_lists_no_run() -> None:
    harness = _EventSuite()
    harness._ae = _FakeAE(run_lookup=RunLookup(conclusive=True))  # type: ignore[assignment]

    with pytest.raises(EventRunNotStartedError, match="did not start a run") as exc:
        await harness._submit({}, slug=_SLUG)
    assert isinstance(exc.value, AssertionError)


async def test_submit_says_so_when_the_run_list_was_unreadable() -> None:
    harness = _EventSuite()
    harness._ae = _FakeAE(run_lookup=RunLookup(conclusive=False))  # type: ignore[assignment]

    with pytest.raises(EventRunNotStartedError, match="could not be read"):
        await harness._submit({}, slug=_SLUG)


async def test_send_event_must_be_overridden() -> None:
    class _NoSend(EventStartedE2ETest):
        connector_short_name = "example"
        trigger_entrypoint = "event-sync"

    harness = _NoSend()
    harness._ae = _FakeAE()  # type: ignore[assignment]
    with pytest.raises(HarnessMethodNotImplementedError):
        await harness._submit({}, slug=_SLUG)


async def test_a_system_app_suite_still_adopts_rather_than_submits() -> None:
    harness = _SystemEventSuite()
    fake = _FakeAE(run_lookup=RunLookup(run_id="run-from-event", conclusive=True))
    harness._ae = fake  # type: ignore[assignment]

    assert await harness._submit({}, slug=_SLUG) == "run-from-event"
    assert "submit_published_version" not in fake.methods


def test_the_adopted_run_is_not_tracked_for_teardown() -> None:
    harness = _EventSuite()
    harness._unsettled_run_ids = []
    harness._track_unsettled_run("run-from-event")
    assert harness._unsettled_run_ids == []


# ---------------------------------------------------------------------------
# post_event
# ---------------------------------------------------------------------------


def _gateway_suite(handler: Any) -> _EventSuite:
    class _Suite(_EventSuite):
        def _event_gateway_transport(self) -> httpx.AsyncBaseTransport | None:
            return httpx.MockTransport(handler)

    return _Suite()


async def test_post_event_sends_json_with_the_run_api_key(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await _gateway_suite(handler).post_event("/example/api/v1/x", {"id": "e-1"})

    (request,) = seen
    assert str(request.url) == "https://test.example.invalid/events/example/api/v1/x"
    assert request.headers["authorization"] == "Bearer test-token"
    assert json.loads(request.content) == {"id": "e-1"}


async def test_post_event_headers_override_the_default_authorization(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    await _gateway_suite(handler).post_event(
        "example", {}, headers={"Authorization": "Token other"}
    )
    assert seen[0].headers["authorization"] == "Token other"


async def test_post_event_raises_on_a_rejected_event(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(EventGatewayRejectedError, match="HTTP 401"):
        await _gateway_suite(lambda _: httpx.Response(401)).post_event("example", {})


async def test_post_event_raises_when_the_gateway_is_unreachable(
    tenant_env: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EventGatewayRejectedError, match="did not reach"):
        await _gateway_suite(handler).post_event("example", {})
