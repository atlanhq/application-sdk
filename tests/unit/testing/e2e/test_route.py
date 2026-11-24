"""Unit tests for :class:`application_sdk.testing.e2e.route.RouteE2ETest`."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, ClassVar

import httpx
import pytest

from application_sdk.testing.e2e import route as route_mod
from application_sdk.testing.e2e._errors import (
    MissingHarnessClassAttrError,
    MissingHarnessEnvError,
    RouteNotReadyError,
)
from application_sdk.testing.e2e.route import RouteE2ETest

BASE_URL = "https://tenant.example.com"
API_KEY = "e2e-key"


def _suite(
    handler: Callable[[httpx.Request], httpx.Response],
    **attrs: Any,
) -> type[RouteE2ETest]:
    """A RouteE2ETest subclass whose clients answer from *handler*."""

    class Suite(RouteE2ETest):
        ingress_path: ClassVar[str] = "/demo-app/api"

        def _client(
            self,
            *,
            base_url: str,
            timeout: httpx.Timeout,
            headers: dict[str, str] | None = None,
        ) -> httpx.Client:
            return httpx.Client(
                transport=httpx.MockTransport(handler),
                base_url=base_url,
                timeout=timeout,
                headers=headers,
            )

    for name, value in attrs.items():
        setattr(Suite, name, value)
    return Suite


@pytest.fixture
def tenant_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ATLAN_BASE_URL", f"{BASE_URL}/")
    monkeypatch.setenv("ATLAN_API_KEY", API_KEY)
    yield


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Freeze time: ``sleep`` advances a fake clock instead of waiting."""
    now = [0.0]
    monkeypatch.setattr(route_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        route_mod.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    return now


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"path": request.url.path})


class TestSetup:
    def test_missing_tenant_env_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_BASE_URL", raising=False)
        monkeypatch.setenv("ATLAN_API_KEY", API_KEY)
        with pytest.raises(MissingHarnessEnvError):
            _suite(_ok)().setup_method(None)

    @pytest.mark.usefixtures("tenant_env")
    def test_unset_ingress_path_is_refused(self) -> None:
        with pytest.raises(MissingHarnessClassAttrError) as raised:
            _suite(_ok, ingress_path="")().setup_method(None)
        assert raised.value.field == "ingress_path"

    @pytest.mark.usefixtures("tenant_env")
    @pytest.mark.parametrize(
        "ingress", ["/demo-app/api", "demo-app/api/", " /demo-app/api/ "]
    )
    def test_api_sends_below_the_ingress_with_the_api_key(self, ingress: str) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        suite = _suite(handler, ingress_path=ingress)()
        suite.setup_method(None)
        suite.api.get("/v1/state")
        suite.anonymous.get("/demo-app/index.html")
        suite.teardown_method(None)

        assert str(seen[0].url) == f"{BASE_URL}/demo-app/api/v1/state"
        assert seen[0].headers["Authorization"] == f"Bearer {API_KEY}"
        assert str(seen[1].url) == f"{BASE_URL}/demo-app/index.html"
        assert "Authorization" not in seen[1].headers
        assert suite.base_url == BASE_URL


@pytest.mark.usefixtures("tenant_env")
class TestReadiness:
    def test_no_readiness_path_sends_nothing_at_setup(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        _suite(handler)().setup_method(None)
        assert seen == []

    def test_waits_through_unready_answers_then_passes(
        self, clock: list[float]
    ) -> None:
        answers = iter([503, 502, 200])
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(next(answers))

        _suite(handler, readiness_path="/v1/state")().setup_method(None)
        assert seen == ["/demo-app/api/v1/state"] * 3
        assert clock[0] == 10.0

    def test_readiness_is_checked_once_per_class(self, clock: list[float]) -> None:
        del clock
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200)

        suite_cls = _suite(handler, readiness_path="/v1/state")
        suite_cls().setup_method(None)
        suite_cls().setup_method(None)
        assert len(seen) == 1

    def test_never_ready_raises_with_the_last_answer(self, clock: list[float]) -> None:
        suite_cls = _suite(
            lambda request: httpx.Response(404),
            readiness_path="/v1/state",
            readiness_timeout_seconds=12.0,
            readiness_interval_seconds=5.0,
        )
        with pytest.raises(RouteNotReadyError) as raised:
            suite_cls().setup_method(None)
        err = raised.value
        assert err.url == f"{BASE_URL}/demo-app/api/v1/state"
        assert err.attempts == 3
        assert err.last_error == "HTTP 404"
        assert clock[0] == 10.0
        assert not suite_cls._route_ready

    def test_transport_errors_count_as_not_ready(self, clock: list[float]) -> None:
        del clock

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        suite_cls = _suite(
            handler,
            readiness_path="/v1/state",
            readiness_timeout_seconds=6.0,
            readiness_interval_seconds=5.0,
        )
        with pytest.raises(RouteNotReadyError) as raised:
            suite_cls().setup_method(None)
        assert raised.value.last_error == "ConnectError: connection refused"


@pytest.mark.usefixtures("tenant_env")
class TestCleanup:
    def test_cleanups_run_last_added_first(self) -> None:
        order: list[str] = []
        suite = _suite(_ok)()
        suite.setup_method(None)
        suite.add_cleanup(order.append, "first")
        suite.add_cleanup(order.append, "second")
        suite.teardown_method(None)
        assert order == ["second", "first"]
        assert suite.api.is_closed
        assert suite.anonymous.is_closed

    def test_one_failing_cleanup_still_runs_the_rest_and_is_raised(self) -> None:
        order: list[str] = []

        def boom() -> None:
            raise RuntimeError("delete failed")

        suite = _suite(_ok)()
        suite.setup_method(None)
        suite.add_cleanup(order.append, "first")
        suite.add_cleanup(boom)
        suite.add_cleanup(order.append, "third")
        with pytest.raises(RuntimeError, match="delete failed"):
            suite.teardown_method(None)
        assert order == ["third", "first"]
        assert suite.api.is_closed

    def test_several_failing_cleanups_raise_together(self) -> None:
        def boom(message: str) -> None:
            raise RuntimeError(message)

        suite = _suite(_ok)()
        suite.setup_method(None)
        suite.add_cleanup(boom, "a")
        suite.add_cleanup(boom, "b")
        with pytest.raises(ExceptionGroup) as raised:
            suite.teardown_method(None)
        assert sorted(str(e) for e in raised.value.exceptions) == ["a", "b"]

    def test_teardown_after_failed_setup_does_not_mask_the_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_API_KEY")
        suite = _suite(_ok)()
        with pytest.raises(MissingHarnessEnvError):
            suite.setup_method(None)
        suite.teardown_method(None)


@pytest.mark.usefixtures("tenant_env")
class TestExpectStatus:
    def _response(self, status: int, **kwargs: Any) -> httpx.Response:
        request = httpx.Request("PUT", f"{BASE_URL}/demo-app/api/v1/steps/x")
        return httpx.Response(status, request=request, **kwargs)

    def test_returns_the_response_for_chaining(self) -> None:
        suite = _suite(_ok)()
        response = self._response(200, json={"a": 1})
        assert suite.expect_status(response) is response
        assert suite.expect_status(response).json() == {"a": 1}

    def test_accepts_the_expected_non_200_status(self) -> None:
        suite = _suite(_ok)()
        response = self._response(204)
        assert suite.expect_status(response, status=204) is response

    def test_wrong_status_names_method_path_and_body(self) -> None:
        suite = _suite(_ok)()
        with pytest.raises(AssertionError) as raised:
            suite.expect_status(self._response(422, text="bad answer"))
        message = str(raised.value)
        assert "PUT /demo-app/api/v1/steps/x returned 422, expected 200" in message
        assert "bad answer" in message


def test_run_label(monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _suite(_ok)()
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    assert suite.run_label == "e2e run 12345"
    monkeypatch.delenv("GITHUB_RUN_ID")
    assert suite.run_label == "e2e run local"
