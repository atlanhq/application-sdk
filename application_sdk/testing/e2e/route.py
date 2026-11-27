"""E2e base for apps whose work is an HTTP route on the tenant.

:class:`BaseE2ETest` and :class:`~application_sdk.testing.e2e.SystemAppE2ETest`
start a workflow run and wait for it. An app whose real work is a web API (a
request goes in, the answer comes back, no run) has nothing for them to start.
:class:`RouteE2ETest` is the base for that shape: the suite calls the app's
routes through the tenant's ingress, the same way the UI or an agent does, and
checks the answers.

It submits no DAG and creates no Connection, so it is not a
:class:`BaseE2ETest` subclass and declares no ``mode``. Subclassing would not
just be unneeded, it would break a route suite: pytest would collect the
inherited ``test_full_dag_runs_end_to_end`` and submit a DAG the app does not
have; ``setup_method`` would raise for the unset ``argo_package_name`` and
``argo_template_name``; and teardown would track seeded connections and AE runs
that never exist. What the two bases share (tenant auth) lives in
:mod:`application_sdk.testing.harness`, not on :class:`BaseE2ETest`. CI installs this PR's
build on the tenant and checks the running pod's build identity before every
leg (``e2e_tenant_app.py verify``), so the suite starts against the right code.

What a suite declares:

* ``ingress_path`` — the app's ingress prefix on the tenant, for example
  ``"/launchpad-app/api"``. ``self.api`` sends every request below it.
* ``readiness_path`` — optional. A route, relative to ``ingress_path``, that
  answers 2xx once the app serves. The first test of the class waits for it,
  so a pod still reconciling or waking from zero is not read as a failure.
  The wait ends at ``readiness_timeout_seconds``: each probe's timeout is cut
  to the time left, and an answer that lands after the deadline does not
  count. If it never answers, the rest of the class fails at once instead of
  waiting again.

What a suite gets:

* ``self.api`` — an ``httpx.Client`` on ``<base_url><ingress_path>`` that sends
  the e2e API key as a bearer token. The key is bound to the tenant's origin: a
  request to any other scheme, host or port (an absolute URL, or an absolute
  ``readiness_path``) raises
  :class:`~application_sdk.testing.e2e._errors.OffTenantRequestError` before
  anything is sent.
* ``self.anonymous`` — an ``httpx.Client`` on the tenant base URL with no
  credentials, for pages and for checking that a route refuses anonymous calls.
* :meth:`RouteE2ETest.add_cleanup` — undo steps that run after each test,
  last added first, even when the test failed.
* :meth:`RouteE2ETest.assert_status` — assert a status code, with the method,
  path and body in the failure message, and return the response.
* :attr:`RouteE2ETest.run_label` — a label naming this CI run, for records the
  suite writes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Generator
from typing import ClassVar, ParamSpec

import httpx

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    MissingHarnessClassAttrError,
    MissingHarnessEnvError,
    OffTenantRequestError,
    RouteNotReadyError,
)
from application_sdk.testing.harness._errors import MissingTenantEnvError
from application_sdk.testing.harness.identity import read_tenant_auth

logger = get_logger(__name__)

__all__ = ["RouteE2ETest"]

_BODY_EXCERPT_CHARS = 500

_P = ParamSpec("_P")


def _origin(url: httpx.URL) -> str:
    """``scheme://host[:port]`` of *url*; httpx drops a scheme's default port."""
    port = f":{url.port}" if url.port is not None else ""
    return f"{url.scheme}://{url.host}{port}"


class _TenantBearerAuth(httpx.Auth):
    """Bearer auth that only ever signs requests to the tenant's own origin.

    A client-level ``Authorization`` header rides on every request the client
    sends, including one to an absolute URL on another host. Signing here
    instead checks each request first and refuses one that leaves the tenant,
    so the e2e API key cannot be sent anywhere else.
    """

    def __init__(self, api_key: str, tenant: httpx.URL) -> None:
        self._header = f"Bearer {api_key}"
        self._origin = _origin(tenant)

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        origin = _origin(request.url)
        if origin != self._origin:
            raise OffTenantRequestError(
                message=(
                    f"Refusing to send the e2e API key to {origin}: self.api only "
                    f"calls the tenant ({self._origin}). Use self.anonymous or a "
                    "client of your own for other hosts."
                ),
                url=origin,
                tenant_origin=self._origin,
            )
        request.headers["Authorization"] = self._header
        yield request


class RouteE2ETest:
    """Pytest base for an app's route suite on the tenant."""

    ingress_path: ClassVar[str] = ""
    readiness_path: ClassVar[str | None] = None
    readiness_timeout_seconds: ClassVar[float] = 300.0
    readiness_interval_seconds: ClassVar[float] = 5.0
    request_timeout_seconds: ClassVar[float] = 30.0
    _route_ready: ClassVar[bool] = False
    _route_not_ready: ClassVar[RouteNotReadyError | None] = None

    base_url: str
    api: httpx.Client
    anonymous: httpx.Client

    def setup_method(self, method: object) -> None:
        """Build the clients and, once per class, wait for the app to serve.

        Raises:
            MissingHarnessEnvError: ``ATLAN_BASE_URL`` or ``ATLAN_API_KEY`` is
                absent or blank.
            MissingHarnessClassAttrError: ``ingress_path`` is not set.
            OffTenantRequestError: ``readiness_path`` is an absolute URL on
                another host.
            RouteNotReadyError: ``readiness_path`` never answered 2xx within
                ``readiness_timeout_seconds``, in this test or an earlier one of
                the same class.
        """
        del method
        self._cleanups: list[Callable[[], object]] = []
        try:
            auth = read_tenant_auth(os.environ)
        except MissingTenantEnvError as missing:
            raise MissingHarnessEnvError(
                message=(
                    "Route e2e suites need ATLAN_BASE_URL + ATLAN_API_KEY: the "
                    "tenant to call and the e2e API key that signs in to it."
                ),
                field=missing.field,
                cause=missing,
            ) from missing
        prefix = type(self).ingress_path.strip().rstrip("/")
        if not prefix:
            raise MissingHarnessClassAttrError(
                message=f"{type(self).__name__}: class attribute 'ingress_path' must be set",
                field="ingress_path",
            )
        if not prefix.startswith("/"):
            prefix = f"/{prefix}"
        timeout = httpx.Timeout(type(self).request_timeout_seconds)
        self.base_url = auth.base_url
        self.api = self._client(
            base_url=f"{auth.base_url}{prefix}",
            auth=_TenantBearerAuth(auth.api_key, httpx.URL(auth.base_url)),
            timeout=timeout,
        )
        self.anonymous = self._client(base_url=auth.base_url, timeout=timeout)
        cls = type(self)
        if cls.readiness_path and not cls.__dict__.get("_route_ready", False):
            failed = cls.__dict__.get("_route_not_ready")
            try:
                if failed is not None:
                    raise failed.with_traceback(None)
                self._wait_until_ready(prefix)
            except RouteNotReadyError as exc:
                cls._route_not_ready = exc
                self._close_clients()
                raise
            except BaseException:
                self._close_clients()
                raise
            cls._route_ready = True

    def teardown_method(self, method: object) -> None:
        """Run the cleanups, last added first, then close the clients.

        Every cleanup runs even when an earlier one fails. One failure is
        re-raised as it is; several are raised together as an
        ``ExceptionGroup``.
        """
        del method
        failures: list[Exception] = []
        for cleanup in reversed(getattr(self, "_cleanups", [])):
            try:
                cleanup()
            except Exception as exc:
                logger.warning("Route e2e cleanup failed: %s", exc, exc_info=True)
                failures.append(exc)
        self._cleanups = []
        self._close_clients()
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("route e2e cleanups failed", failures)

    def add_cleanup(
        self, fn: Callable[_P, object], *args: _P.args, **kwargs: _P.kwargs
    ) -> None:
        """Register an undo step that runs after this test, pass or fail."""
        self._cleanups.append(lambda: fn(*args, **kwargs))

    def assert_status(
        self, response: httpx.Response, status: int = 200
    ) -> httpx.Response:
        """Assert *response* has *status* and return it, for ``.json()`` chaining.

        Raises:
            AssertionError: The status differs. The message names the method,
                the path, both statuses and the start of the body.
        """
        assert response.status_code == status, (
            f"{response.request.method} {response.request.url.path} returned "
            f"{response.status_code}, expected {status}: "
            f"{response.text[:_BODY_EXCERPT_CHARS]}"
        )
        return response

    @property
    def run_label(self) -> str:
        """``e2e run <GITHUB_RUN_ID>``, or ``e2e run local`` outside CI."""
        return f"e2e run {os.environ.get('GITHUB_RUN_ID', 'local')}"

    def _client(
        self,
        *,
        base_url: str,
        timeout: httpx.Timeout,
        auth: httpx.Auth | None = None,
    ) -> httpx.Client:
        return httpx.Client(base_url=base_url, timeout=timeout, auth=auth)

    def _close_clients(self) -> None:
        for name in ("api", "anonymous"):
            client = getattr(self, name, None)
            if client is not None:
                client.close()

    def _wait_until_ready(self, prefix: str) -> None:
        """Probe ``readiness_path`` until it answers 2xx, within the deadline.

        The deadline is ``readiness_timeout_seconds`` from the first probe. Each
        probe's timeout is the smaller of ``request_timeout_seconds`` and the
        time left, so a stalled endpoint cannot hold the wait past it, and a 2xx
        that lands after it does not count.
        """
        cls = type(self)
        path = cls.readiness_path or ""
        url = f"{self.base_url}{prefix}{path}"
        started = time.monotonic()
        deadline = started + cls.readiness_timeout_seconds
        attempts = 0
        last_error = "no probe fit in the timeout"
        while True:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                attempts += 1
                probe_timeout = httpx.Timeout(
                    min(cls.request_timeout_seconds, remaining)
                )
                try:
                    response = self.api.get(path, timeout=probe_timeout)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                else:
                    late = time.monotonic() > deadline
                    if response.is_success and not late:
                        logger.info(
                            "Route e2e: %s answered %s after %d attempt(s)",
                            url,
                            response.status_code,
                            attempts,
                        )
                        return
                    last_error = f"HTTP {response.status_code}" + (
                        " after the deadline" if late else ""
                    )
            now = time.monotonic()
            if now + cls.readiness_interval_seconds > deadline:
                raise RouteNotReadyError(
                    message=(
                        f"{url} did not answer 2xx within "
                        f"{cls.readiness_timeout_seconds:.0f}s (last: {last_error})"
                    ),
                    url=url,
                    attempts=attempts,
                    elapsed_seconds=round(now - started, 1),
                    last_error=last_error,
                )
            time.sleep(cls.readiness_interval_seconds)
