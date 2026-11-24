"""E2e base for apps whose work is an HTTP route on the tenant.

:class:`BaseE2ETest` and :class:`~application_sdk.testing.e2e.SystemAppE2ETest`
start a workflow run and wait for it. An app whose real work is a web API (a
request goes in, the answer comes back, no run) has nothing for them to start.
:class:`RouteE2ETest` is the base for that shape: the suite calls the app's
routes through the tenant's ingress, the same way the UI or an agent does, and
checks the answers.

It submits no DAG and creates no Connection, so it is not a
:class:`BaseE2ETest` subclass and declares no ``mode``. CI installs this PR's
build on the tenant and checks the running pod's build identity before every
leg (``e2e_tenant_app.py verify``), so the suite starts against the right code.

What a suite declares:

* ``ingress_path`` — the app's ingress prefix on the tenant, for example
  ``"/launchpad-app/api"``. ``self.api`` sends every request below it.
* ``readiness_path`` — optional. A route, relative to ``ingress_path``, that
  answers 2xx once the app serves. The first test of the class waits for it,
  so a pod still reconciling or waking from zero is not read as a failure.

What a suite gets:

* ``self.api`` — an ``httpx.Client`` on ``<base_url><ingress_path>`` that sends
  the e2e API key as a bearer token.
* ``self.anonymous`` — an ``httpx.Client`` on the tenant base URL with no
  credentials, for pages and for checking that a route refuses anonymous calls.
* :meth:`RouteE2ETest.add_cleanup` — undo steps that run after each test,
  last added first, even when the test failed.
* :meth:`RouteE2ETest.expect_status` — assert a status code, with the method,
  path and body in the failure message, and return the response.
* :attr:`RouteE2ETest.run_label` — a label naming this CI run, for records the
  suite writes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import ClassVar, ParamSpec

import httpx

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e._errors import (
    MissingHarnessClassAttrError,
    MissingHarnessEnvError,
    RouteNotReadyError,
)
from application_sdk.testing.harness._errors import MissingTenantEnvError
from application_sdk.testing.harness.identity import read_tenant_auth

logger = get_logger(__name__)

__all__ = ["RouteE2ETest"]

_BODY_EXCERPT_CHARS = 500

_P = ParamSpec("_P")


class RouteE2ETest:
    """Pytest base for an app's route suite on the tenant."""

    ingress_path: ClassVar[str] = ""
    readiness_path: ClassVar[str | None] = None
    readiness_timeout_seconds: ClassVar[float] = 300.0
    readiness_interval_seconds: ClassVar[float] = 5.0
    request_timeout_seconds: ClassVar[float] = 30.0
    _route_ready: ClassVar[bool] = False

    base_url: str
    api: httpx.Client
    anonymous: httpx.Client

    def setup_method(self, method: object) -> None:
        """Build the clients and, once per class, wait for the app to serve.

        Raises:
            MissingHarnessEnvError: ``ATLAN_BASE_URL`` or ``ATLAN_API_KEY`` is
                absent or blank.
            MissingHarnessClassAttrError: ``ingress_path`` is not set.
            RouteNotReadyError: ``readiness_path`` never answered 2xx within
                ``readiness_timeout_seconds``.
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
            headers={"Authorization": f"Bearer {auth.api_key}"},
            timeout=timeout,
        )
        self.anonymous = self._client(base_url=auth.base_url, timeout=timeout)
        if type(self).readiness_path and not type(self).__dict__.get(
            "_route_ready", False
        ):
            self._wait_until_ready(prefix)
            type(self)._route_ready = True

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
        for name in ("api", "anonymous"):
            client = getattr(self, name, None)
            if client is not None:
                client.close()
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("route e2e cleanups failed", failures)

    def add_cleanup(
        self, fn: Callable[_P, object], *args: _P.args, **kwargs: _P.kwargs
    ) -> None:
        """Register an undo step that runs after this test, pass or fail."""
        self._cleanups.append(lambda: fn(*args, **kwargs))

    def expect_status(
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
        headers: dict[str, str] | None = None,
    ) -> httpx.Client:
        return httpx.Client(base_url=base_url, timeout=timeout, headers=headers)

    def _wait_until_ready(self, prefix: str) -> None:
        cls = type(self)
        path = cls.readiness_path or ""
        url = f"{self.base_url}{prefix}{path}"
        started = time.monotonic()
        attempts = 0
        last_error = ""
        while True:
            attempts += 1
            try:
                response = self.api.get(path)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.is_success:
                    logger.info(
                        "Route e2e: %s answered %s after %d attempt(s)",
                        url,
                        response.status_code,
                        attempts,
                    )
                    return
                last_error = f"HTTP {response.status_code}"
            elapsed = time.monotonic() - started
            if elapsed + cls.readiness_interval_seconds > cls.readiness_timeout_seconds:
                raise RouteNotReadyError(
                    message=(
                        f"{url} did not answer 2xx within "
                        f"{cls.readiness_timeout_seconds:.0f}s (last: {last_error})"
                    ),
                    url=url,
                    attempts=attempts,
                    elapsed_seconds=round(elapsed, 1),
                    last_error=last_error,
                )
            time.sleep(cls.readiness_interval_seconds)
