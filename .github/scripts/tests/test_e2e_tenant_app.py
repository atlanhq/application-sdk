"""Tests for .github/scripts/e2e_tenant_app.py and e2e_tenant_api.py.

The HTTP seam is stubbed at ``TenantClient.request`` — the single place every
tenant call funnels through — so the branch logic (converge-by-version, the
credential hint on 401, the scan-gate hint, terminal vs timed-out deployments,
the version-mismatch failure) is exercised for real while nothing leaves the
process.
"""

from __future__ import annotations

import argparse
import builtins
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import e2e_tenant_app as app  # noqa: E402
from e2e_tenant_api import Response, TenantApiError, TenantClient  # noqa: E402

_TENANT = "https://example-tenant.atlan.test"
_APP_ID = "019d1f6b-6fea-7db3-96d8-e61e159d0351"
_IMAGE = "ghcr.io/atlanhq/atlan-openapi-app:sdr-test-abc12345"
_VERSION = "sdr-test-abc12345"
_REPO = "https://github.com/atlanhq/atlan-openapi-app"


# ── Typed HTTP stub ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StubRoute:
    """One canned response, matched on method + a path fragment."""

    method: str
    path_fragment: str
    response: Response


@dataclass
class StubCall:
    method: str
    path: str
    body: dict[str, object] | None


@dataclass
class StubTransport:
    """Serves ``routes`` in order; each route is consumed once unless ``sticky``.

    Ordered-and-consumed rather than a dict keyed by path so a test can script a
    sequence against the SAME path — which is exactly what polling a deployment
    to a terminal state looks like.
    """

    routes: list[StubRoute]
    sticky: list[StubRoute] = field(default_factory=list)
    calls: list[StubCall] = field(default_factory=list)

    # No `self`-of-TenantClient parameter: assigning a BOUND method onto the
    # class means attribute lookup returns an already-bound object, so the
    # descriptor protocol never runs and the TenantClient instance is not
    # prepended. `client.request("GET", path)` arrives here as
    # `transport.request("GET", path)`.
    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        timeout: int = 60,
    ) -> Response:
        self.calls.append(StubCall(method=method, path=path, body=body))
        for index, route in enumerate(self.routes):
            if route.method == method and route.path_fragment in path:
                return self.routes.pop(index).response
        for route in self.sticky:
            if route.method == method and route.path_fragment in path:
                return route.response
        raise AssertionError(f"unstubbed call: {method} {path}")

    def paths(self, method: str) -> list[str]:
        return [c.path for c in self.calls if c.method == method]

    def body_for(self, fragment: str) -> dict[str, object]:
        for call in self.calls:
            if fragment in call.path and call.body is not None:
                return call.body
        raise AssertionError(f"no request body recorded for a path with {fragment!r}")


def _ok(payload: dict[str, object]) -> Response:
    return Response(status=200, body=payload)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2E_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("E2E_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("ATLAN_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    # Never let a test reach a real token endpoint.
    monkeypatch.setattr(app, "mint_oauth_token", lambda *_a, **_k: "stub.jwt.token")


def _wire(monkeypatch: pytest.MonkeyPatch, transport: StubTransport) -> StubTransport:
    monkeypatch.setattr(TenantClient, "request", transport.request)
    return transport


def _install_args(**overrides: object) -> argparse.Namespace:
    values = {
        "base_url": _TENANT,
        "app_id": _APP_ID,
        "image": _IMAGE,
        "version": _VERSION,
        "branch": "chrishehim/fnd-31",
        "tenant": "example-tenant",
        "repo_url": _REPO,
        "deploy_config": "",
        "self_deployed_runtime": False,
        "sdk_version": "",
        "entrypoints": "",
        "app_configs": "",
        "release_model": "",
        "created_by": "",
        "scan_wait_seconds": 0,
        # Both retry budgets default to 0 here, and every test that wants a retry
        # opts in. `time.sleep` is stubbed but `time.monotonic` is not, so a
        # nonzero budget against a transport that never succeeds is a hot loop
        # for that many real seconds — a test must either script a success or
        # keep the budget at 0.
        "publish_retry_seconds": 0,
        "install_retry_seconds": 0,
        "timeout_seconds": 600,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


# ── Converge by version ──────────────────────────────────────────────────────


def test_install_is_a_noop_when_the_tenant_already_runs_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("GET", "/info", _ok({"version": _VERSION}))]),
    )
    outcome = app.install(_install_args())

    assert outcome.skipped is True
    assert outcome.installed_version == _VERSION
    assert transport.paths("POST") == [], (
        "an already-current tenant must not be re-published or re-installed — "
        "that is what makes running this once per (run x cloud) and again on a "
        "re-run safe"
    )


def test_install_proceeds_when_the_app_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 404 on info is the "never installed on this tenant" case — FND-31
    # requirement 2 says install rather than fail.
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute(
                    "POST",
                    "/marketplace/publish",
                    _ok({"version_id": "v1", "release_id": "r1"}),
                ),
                StubRoute("GET", "/releases/", _ok({"status": "scan_pending"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok({"version": _VERSION})),
            ]
        ),
    )
    outcome = app.install(_install_args())

    assert outcome.skipped is False
    assert outcome.deployment_id == "d1"
    assert outcome.installed_version == _VERSION
    assert outcome.release_status == "scan_pending"
    assert transport.body_for("/install") == {
        "version_id": "v1",
        "force_install": True,
    }


def test_install_scopes_the_registration_to_the_one_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", _ok({"version": "older"})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok({"version": _VERSION})),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
        ),
    )
    app.install(_install_args())

    body = transport.body_for("/marketplace/publish")
    assert body["allowed_tenants"] == [
        "example-tenant"
    ], "a per-PR e2e version must be reachable only by its own e2e tenant"
    assert "target_channel" not in body


# ── The scan gate ────────────────────────────────────────────────────────────


def test_install_does_not_wait_for_the_scan_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute(
                    "POST",
                    "/marketplace/publish",
                    _ok({"version_id": "v1", "release_id": "r1"}),
                ),
                StubRoute("GET", "/releases/", _ok({"status": "scan_pending"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok({"version": _VERSION})),
            ]
        ),
    )
    app.install(_install_args(scan_wait_seconds=0))
    # Exactly one release read (the informational one), no poll loop.
    assert len([p for p in transport.paths("GET") if "/releases/" in p]) == 1


def test_scan_rejection_names_the_flag_that_fixes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute(
                    "POST",
                    "/marketplace/publish",
                    _ok({"version_id": "v1", "release_id": "r1"}),
                ),
                StubRoute("GET", "/releases/", _ok({"status": "scan_pending"})),
                StubRoute(
                    "POST",
                    "/install",
                    Response(status=400, body={"detail": "release is scan_pending"}),
                ),
            ]
        ),
    )
    with pytest.raises(app.TenantAppError, match="scan-wait-seconds"):
        app.install(_install_args())


def test_scan_wait_polls_until_the_scan_leaves_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute(
                    "POST",
                    "/marketplace/publish",
                    _ok({"version_id": "v1", "release_id": "r1"}),
                ),
                StubRoute("GET", "/releases/", _ok({"status": "scan_pending"})),
                StubRoute("GET", "/releases/", _ok({"status": "scan_pending"})),
                StubRoute("GET", "/releases/", _ok({"status": "active"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok({"version": _VERSION})),
            ]
        ),
    )
    outcome = app.install(_install_args(scan_wait_seconds=300))
    assert outcome.release_status == "active"
    assert len([p for p in transport.paths("GET") if "/releases/" in p]) == 3


# ── Credential diagnosis ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
def test_publish_rejection_names_the_oauth_pair_not_the_api_key(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # The single most likely misconfiguration is reaching for ATLAN_API_KEY here.
    # The error has to say which of two credentials was rejected.
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute(
                    "POST", "/marketplace/publish", Response(status=status, body={})
                ),
            ]
        ),
    )
    with pytest.raises(app.TenantAppError, match="E2E_OAUTH_CLIENT_ID"):
        app.install(_install_args())


def test_publish_without_a_version_id_fails_rather_than_installing_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute("POST", "/marketplace/publish", _ok({"release_id": "r1"})),
            ]
        ),
    )
    with pytest.raises(app.TenantAppError, match="version_id"):
        app.install(_install_args())


# ── The marketplace service is a shared dependency (FND-760) ─────────────────
# Heracles reports "the marketplace service did not answer me" as HTTP 400
# `code 1000` "Please check your request parameters" — a validation shape for
# something that is not a validation failure. On 2026-08-24 that took four
# connector PRs down in eight minutes across all three clouds. The publish is
# retried through it; a real rejection still fails on the first response.


def _upstream_down(status: int = 400) -> Response:
    """Heracles' body when its own call to the marketplace service did not return.

    Verbatim from the atlan-db2-app / atlan-databricks-app / cloudsql legs, minus
    the requestId — the point of the fixture is that the retryable signal lives in
    `message` and nowhere else.
    """
    return Response(
        status=status,
        body={
            "code": 1000,
            "error": "Please check your request parameters",
            "info": None,
            "message": "error getting response from marketplace service",
        },
    )


def _publish_after(*failures: Response) -> StubTransport:
    """Transport whose publish fails with each of ``failures``, then succeeds."""
    return StubTransport(
        routes=[
            StubRoute("GET", "/info", _ok({})),
            *[StubRoute("POST", "/marketplace/publish", f) for f in failures],
            StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
            StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
            StubRoute("GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})),
            StubRoute("GET", "/info", _ok(_lm_info(_VERSION))),
        ],
        sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
    )


def test_publish_retries_through_the_marketplace_being_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _wire(
        monkeypatch,
        _publish_after(_upstream_down(), _upstream_down(), _upstream_down()),
    )
    outcome = app.install(_install_args(publish_retry_seconds=240))
    assert outcome.deployment_id == "d1"
    assert len([p for p in transport.paths("POST") if "/publish" in p]) == 4


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_publish_retries_the_transient_statuses(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A body-less 5xx/429 is retried on the status alone — no marker needed."""
    transport = _wire(monkeypatch, _publish_after(Response(status=status, body={})))
    app.install(_install_args(publish_retry_seconds=240))
    assert len([p for p in transport.paths("POST") if "/publish" in p]) == 2


def test_a_real_rejection_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this guards: retrying every publish 400.

    Heracles' upstream-failure body IS a 400, so the temptation is to retry the
    status. That would make a genuinely malformed request — and the CI/CD-managed
    provenance guard, which arrives as a 400 too — spend the whole budget before
    reporting the error a human has to read. Only the message discriminates.
    """
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute(
                    "POST",
                    "/marketplace/publish",
                    Response(
                        status=400, body={"message": "version is managed by ci/cd"}
                    ),
                ),
            ]
        ),
    )
    with pytest.raises(app.TenantAppError, match="source_repo"):
        app.install(_install_args(publish_retry_seconds=240))
    assert len([p for p in transport.paths("POST") if "/publish" in p]) == 1


def test_a_credential_rejection_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even carrying the retryable marker. Credentials do not come good on a retry.

    The marker is upstream text, so a reworded or proxied 401 could carry it;
    the status is checked first precisely so that cannot drag an auth failure
    into a four-minute loop and bury the which-credential hint behind it.
    """
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute("POST", "/marketplace/publish", _upstream_down(status=401)),
            ]
        ),
    )
    with pytest.raises(app.TenantAppError, match="E2E_OAUTH_CLIENT_ID"):
        app.install(_install_args(publish_retry_seconds=240))
    assert len([p for p in transport.paths("POST") if "/publish" in p]) == 1


@dataclass
class _PublishTimesOutThen:
    """Raises a transport-level error on the first ``failures`` publishes.

    A StubRoute can only carry a Response, and this fault has none: TenantClient
    RAISES on DNS / connect / read-timeout rather than returning a status. So this
    is the only way to reach `_publish`'s `except TenantApiError` arm — the arm
    that covers the anaplan leg, where the marketplace service hung for the full
    60s client timeout instead of answering.
    """

    inner: StubTransport
    failures: int
    seen: int = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        timeout: int = 60,
    ) -> Response:
        if method == "POST" and "/marketplace/publish" in path:
            self.seen += 1
            if self.seen <= self.failures:
                raise TenantApiError(
                    f"POST {path} could not reach {_TENANT}: "
                    "The read operation timed out"
                )
        return self.inner.request(method, path, body=body, timeout=timeout)


def test_publish_retries_a_transport_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _PublishTimesOutThen(inner=_publish_after(), failures=2)
    monkeypatch.setattr(TenantClient, "request", transport.request)
    outcome = app.install(_install_args(publish_retry_seconds=240))
    assert outcome.deployment_id == "d1"
    assert transport.seen == 3


def test_publish_budget_exhaustion_says_outage_not_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reaching here means the service stayed down, so the body is misleading.

    Taking "check your request parameters" at face value sends the next person to
    read the publish body for a fault that is not in it — which is exactly the
    hour this failure mode has already cost.
    """
    _wire(
        monkeypatch,
        StubTransport(
            routes=[StubRoute("GET", "/info", Response(status=404, body={}))],
            sticky=[StubRoute("POST", "/marketplace/publish", _upstream_down())],
        ),
    )
    with pytest.raises(app.TenantAppError) as excinfo:
        app.install(_install_args(publish_retry_seconds=0))
    message = str(excinfo.value)
    assert "the payload is not the problem" in message
    assert "outage to raise rather than a run to re-trigger" in message


# ── Reconciliation ───────────────────────────────────────────────────────────


def test_deployment_failure_is_fatal_and_pulls_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET",
                    "/deployments/",
                    _ok({"deployment_status": "FAILED", "message": "ImagePullBackOff"}),
                ),
                StubRoute("GET", "/failure", _ok({"reason": "ImagePullBackOff"})),
                StubRoute("GET", "/events", _ok({"events": "Failed to pull image"})),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
        ),
    )
    with pytest.raises(app.TenantAppError, match="ImagePullBackOff"):
        app.install(_install_args())
    # Both routes: the snapshot is what LM captured at failure time, the events
    # are live and exist even when no snapshot does. See the diagnostics section
    # at the bottom of this file.
    for route in ("/failure", "/events"):
        assert any(route in p for p in transport.paths("GET")), (
            f"a failed deploy must pull {route} into the step log — that is the "
            "only pod-level detail CI can see without vcluster"
        )


def test_deployment_timeout_is_a_failure_not_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An accepted-but-unreconciled deploy IS the silent wrong-version failure this
    # change exists to remove, so a timeout must never pass.
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
            ],
            sticky=[
                StubRoute("GET", "/releases/", Response(status=404, body={})),
                # 404 is the real shape here: a deployment that never reached
                # FAILED has no captured snapshot, which is precisely why the
                # diagnostics fall back to live events.
                StubRoute("GET", "/failure", Response(status=404, body={})),
                StubRoute("GET", "/events", _ok({"events": "0s Normal Pulling"})),
            ],
        ),
    )
    with pytest.raises(app.TenantAppError, match="terminal state"):
        app.install(_install_args(timeout_seconds=0))


def test_transient_deployment_read_is_retried_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(status=404, body={})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", Response(status=502, body="bad gateway")
                ),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok({"version": _VERSION})),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
        ),
    )
    assert app.install(_install_args()).installed_version == _VERSION


# ── verify ───────────────────────────────────────────────────────────────────


def _verify_args(expected: str) -> argparse.Namespace:
    return argparse.Namespace(base_url=_TENANT, app_id=_APP_ID, expected=expected)


def test_verify_passes_on_a_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("GET", "/info", _ok({"version": _VERSION}))]),
    )
    assert app.verify(_verify_args(_VERSION)) == _VERSION


def test_verify_fails_naming_both_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[StubRoute("GET", "/info", _ok({"version": "sdr-test-old"}))]
        ),
    )
    with pytest.raises(app.TenantAppError) as excinfo:
        app.verify(_verify_args(_VERSION))
    message = str(excinfo.value)
    assert (
        "sdr-test-old" in message and _VERSION in message
    ), "the whole point is that an operator can see the drift without digging"


def test_verify_fails_when_nothing_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[StubRoute("GET", "/info", Response(status=404, body={}))]
        ),
    )
    with pytest.raises(app.TenantAppError, match="nothing"):
        app.verify(_verify_args(_VERSION))


# ── app_id resolution ────────────────────────────────────────────────────────
# The verify step inside sdr-e2e passes no --app-id: it runs from the app repo
# root, so the script reads atlan.yaml itself rather than a workflow step
# scraping another script's stdout to hand it over.


def test_explicit_app_id_wins() -> None:
    assert app.resolve_app_id(_APP_ID) == _APP_ID


def test_app_id_read_from_atlan_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "atlan.yaml").write_text(
        f"name: openapi\ntype: connector\napp_id: {_APP_ID}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert app.resolve_app_id("") == _APP_ID


def test_missing_atlan_yaml_names_the_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(app.TenantAppError, match="no atlan.yaml"):
        app.resolve_app_id("")


@pytest.mark.parametrize(
    "body",
    [
        "name: openapi\n",
        "name: openapi\napp_id: ''\n",
        "name: openapi\napp_id: '   '\n",
        "- not-a-mapping\n",
    ],
)
def test_atlan_yaml_without_an_app_id_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    # An app with no app_id is not registered in the marketplace, so there is
    # nothing to install or verify against — that must not read as "app_id ''"
    # and then compare equal to an equally-absent installed version.
    (tmp_path / "atlan.yaml").write_text(body, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(app.TenantAppError, match="app_id"):
        app.resolve_app_id("")


# ── GM's CI/CD-managed version guard ─────────────────────────────────────────
# GM (core/app/service.py) rejects a version-create that omits `repo` when the app
# has a source_repo on file — every first-party app. And on a *mismatched* repo it
# UPDATES app.source_repo rather than rejecting, so sending the wrong one silently
# repoints the app's provenance. Echoing GM's own value back avoids both.


@pytest.mark.parametrize(
    "info, expected",
    [
        (
            {"source_repo": "https://github.com/atlanhq/atlan-openapi-app"},
            "https://github.com/atlanhq/atlan-openapi-app",
        ),
        (
            {"sourceRepo": "https://github.com/atlanhq/x"},
            "https://github.com/atlanhq/x",
        ),
        (
            {"app": {"source_repo": "https://github.com/atlanhq/y"}},
            "https://github.com/atlanhq/y",
        ),
        (
            {"data": {"app": {"source_repo": "https://github.com/atlanhq/z"}}},
            "https://github.com/atlanhq/z",
        ),
        ({}, ""),
        ({"source_repo": "  "}, ""),
        ({"source_repo": 7}, ""),
    ],
)
def test_registered_source_repo_extraction(
    info: dict[str, object], expected: str
) -> None:
    assert app._registered_source_repo(info) == expected


def test_registered_repo_is_echoed_back_on_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publish body must carry the repo GM already has on file.

    Without it GM returns "This app's versions are managed by CI/CD" — the actual
    failure observed on the first live run.
    """
    repo = "https://github.com/atlanhq/atlan-openapi-app"
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", _ok({"source_repo": repo})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok({"version": _VERSION})),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
        ),
    )
    app.install(_install_args(repo_url=repo))
    assert transport.body_for("/marketplace/publish")["repo"] == repo


@pytest.mark.parametrize(
    "supplied",
    [
        # Same repo, different spelling: case, trailing slash, .git suffix.
        # These must NOT trip the mismatch guard — GitHub treats them as
        # identical, and GM's registered value is still what gets sent.
        "HTTPS://GITHUB.COM/AtlanHQ/Atlan-OpenAPI-App",
        "https://github.com/atlanhq/atlan-openapi-app/",
        "https://github.com/atlanhq/atlan-openapi-app.git",
        "https://github.com/atlanhq/atlan-openapi-app/.git",
    ],
)
def test_equivalent_repo_spelling_is_not_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, supplied: str
) -> None:
    registered = "https://github.com/atlanhq/atlan-openapi-app"
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", _ok({"source_repo": registered})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok({"version": _VERSION})),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
        ),
    )
    app.install(_install_args(repo_url=supplied))
    # The registered value is sent back byte-for-byte, not the supplied spelling.
    assert transport.body_for("/marketplace/publish")["repo"] == registered


def test_mismatched_repo_is_refused_rather_than_repointing_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GM only blocks CROSS-ORG source_repo changes; a same-org mismatch is
    # silently applied. Sending the running repo instead of the app's would
    # repoint the app and break its real CI/CD gating, so this must not proceed.
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "GET",
                    "/info",
                    _ok(
                        {"source_repo": "https://github.com/atlanhq/atlan-openapi-app"}
                    ),
                )
            ]
        ),
    )
    with pytest.raises(app.TenantAppError, match="source_repo"):
        app.install(
            _install_args(repo_url="https://github.com/atlanhq/application-sdk")
        )


def test_supplied_repo_used_when_gm_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # An app with no source_repo is not CI/CD-managed; GM's `if repo:` branch then
    # SETS it, which is the intended first-registration path.
    supplied = "https://github.com/atlanhq/atlan-openapi-app"
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", _ok({})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok({"version": _VERSION})),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
        ),
    )
    app.install(_install_args(repo_url=supplied))
    assert transport.body_for("/marketplace/publish")["repo"] == supplied


def test_cicd_managed_rejection_is_recognised_and_explained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = (
        "GM returned 409 creating version: This app's versions are managed by "
        "CI/CD. Edit atlan.yaml in the app's repo and merge it."
    )
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", _ok({})),
                StubRoute(
                    "POST",
                    "/marketplace/publish",
                    Response(status=400, body={"detail": detail}),
                ),
            ]
        ),
    )
    with pytest.raises(app.TenantAppError, match="source_repo on file"):
        app.install(_install_args())


# ── LM's real /apps/{id}/info shape ──────────────────────────────────────────
# LM returns {app_id, catalog, installed}, where `installed` is an InstalledApp
# carrying `version_text` (atlan-local-marketplace-app,
# tenant_apps_manager/models/service.py). Neither that nest nor that key was in
# the original guess-list, so the installed-version read never worked — it always
# returned "", which is indistinguishable from "not installed" and therefore read
# as a successful no-op rather than a broken check.


def _lm_info(
    version_text: str, catalog: dict[str, object] | None = None
) -> dict[str, object]:
    """The real envelope, so these tests pin the shipping contract."""
    return {
        "app_id": _APP_ID,
        "catalog": catalog if catalog is not None else {"name": "openapi"},
        "installed": {
            "app_id": _APP_ID,
            "version_id": "01930000-0000-7000-8000-000000000000",
            "version_text": version_text,
            "installed_at": "2026-08-06T00:00:00Z",
            "last_modified_on": "2026-08-06T00:00:00Z",
            "deployment_name": "atlan",
        },
    }


def test_installed_version_read_from_lm_envelope() -> None:
    assert app._extract_version(_lm_info("1.2.3")) == "1.2.3"


def test_installed_nest_wins_over_a_catalog_version() -> None:
    """`installed` must be preferred over the sibling `catalog` block.

    `catalog` describes the app in general; a top-level-or-catalog-first search
    would report a catalogue version as what the tenant is running, and the
    version check would then pass against the wrong thing — the precise failure
    this whole change exists to prevent.
    """
    info = _lm_info("1.2.3", catalog={"name": "openapi", "version": "9.9.9"})
    assert app._extract_version(info) == "1.2.3"


def test_absent_install_block_reads_as_not_installed() -> None:
    assert (
        app._extract_version({"app_id": _APP_ID, "catalog": {}, "installed": None})
        == ""
    )


def test_converge_uses_the_real_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end through install(): the no-op path must trigger off the shape LM
    # actually returns, not just off a flat {"version": ...}.
    transport = _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("GET", "/info", _ok(_lm_info(_VERSION)))]),
    )
    outcome = app.install(_install_args())
    assert outcome.skipped is True
    assert transport.paths("POST") == []


# ── repo is mandatory, and must be the app's own ──────────────────────────────


def test_publish_without_any_repo_is_refused_before_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # GM would reject it anyway; failing here says why, and names --repo-url.
    _wire(monkeypatch, StubTransport(routes=[StubRoute("GET", "/info", _ok({}))]))
    with pytest.raises(app.TenantAppError, match="--repo-url"):
        app.install(_install_args(repo_url=""))


@pytest.mark.parametrize(
    "image, expected",
    [
        (
            "ghcr.io/atlanhq/atlan-openapi-app:sdr-test-abc12345",
            "https://github.com/atlanhq/atlan-openapi-app",
        ),
        (
            "ghcr.io/atlanhq/atlan-mysql-app:main-1234567",
            "https://github.com/atlanhq/atlan-mysql-app",
        ),
        (
            "ghcr.io/atlanhq/atlan-openapi-app@sha256:" + "0" * 64,
            "https://github.com/atlanhq/atlan-openapi-app",
        ),
        # No registry host -> cannot infer; must not guess.
        ("atlan-openapi-app:tag", ""),
        ("", ""),
    ],
)
def test_repo_inferred_from_image(image: str, expected: str) -> None:
    assert app._repo_from_image(image) == expected


@pytest.mark.parametrize(
    "image, expected",
    [
        ("ghcr.io/atlanhq/atlan-openapi-app:tag", True),
        # An explicit port is still a GHCR reference — the spelling that must
        # not slip past the fail-closed guard into the warn-only path.
        ("ghcr.io:443/atlanhq/atlan-openapi-app:tag", True),
        ("GHCR.IO/atlanhq/atlan-openapi-app:tag", True),
        ("ghcr.io/atlanhq/atlan-openapi-app@sha256:" + "0" * 64, True),
        ("123456789012.dkr.ecr.us-east-1.amazonaws.com/atlanhq/app:tag", False),
        # ghcr.io in the path is not ghcr.io in the registry seat.
        ("myregistry.com/ghcr.io/app:tag", False),
        ("atlan-openapi-app:tag", False),
        ("", False),
    ],
)
def test_ghcr_image_classification(image: str, expected: bool) -> None:
    assert app._is_ghcr_image(image) is expected


def test_ghcr_repo_image_mismatch_fails_closed_before_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ghcr.io image whose implied repo disagrees with --repo-url is a wrong
    repo, not a legitimate exception — image name == repo name holds on GHCR, so
    the publish (which would repoint the app's provenance) must never happen.
    """
    transport = _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("GET", "/info", _ok({}))]),
    )
    with pytest.raises(
        app.TenantAppError, match="does not match the repo implied by the image"
    ):
        app.install(
            _install_args(repo_url="https://github.com/atlanhq/application-sdk")
        )
    assert transport.paths("POST") == []


def test_ghcr_image_with_an_explicit_port_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ghcr.io:443/...`` is the same registry as ``ghcr.io/...`` — the exact
    spelling that used to fall through to warn-only and let a wrong same-org
    repo publish. The port must not turn the guard off.
    """
    transport = _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("GET", "/info", _ok({}))]),
    )
    with pytest.raises(
        app.TenantAppError, match="does not match the repo implied by the image"
    ):
        app.install(
            _install_args(
                image="ghcr.io:443/atlanhq/atlan-openapi-app:tag",
                repo_url="https://github.com/atlanhq/application-sdk",
            )
        )
    assert transport.paths("POST") == []


def test_non_ghcr_repo_image_mismatch_warns_but_proceeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Off GHCR the image-name == repo-name convention is not guaranteed, so a
    disagreement stays warn-only: a legitimate exception must still be able to
    publish, and the caller's value is what gets sent.
    """
    transport = _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", _ok({})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok(_lm_info(_VERSION))),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
        ),
    )
    app.install(
        _install_args(
            image=(
                "123456789012.dkr.ecr.us-east-1.amazonaws.com"
                "/atlanhq/atlan-openapi-app:tag"
            ),
            repo_url="https://github.com/atlanhq/application-sdk",
        )
    )
    out = capsys.readouterr().out
    assert (
        "::warning::" in out and "does not match the repo implied by the image" in out
    )
    assert (
        transport.body_for("/marketplace/publish")["repo"]
        == "https://github.com/atlanhq/application-sdk"
    )


# ── LM answers 200 with an error envelope ────────────────────────────────────
# POST .../install returns HTTP 200 carrying {status, status_code, message} for
# its two non-deploying outcomes, so response.ok alone reads a 404 as success.
# LM's snapshot also lags a fresh publish by up to ~5 min, which is why the
# install is retried rather than failed on first miss.


def _install_reply(status: str, code: int, message: str) -> Response:
    return Response(
        status=200, body={"status": status, "status_code": code, "message": message}
    )


_NOT_FOUND = _install_reply(
    "error", 404, "App with ID '019d1f6b-6fea-7db3-96d8-e61e159d0351' not found: x"
)


def _publish_then(*install_replies: Response) -> StubTransport:
    """Transport that gets as far as the install, then serves the given replies."""
    routes = [
        StubRoute("GET", "/info", _ok({})),
        StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
    ]
    routes += [StubRoute("POST", "/install", r) for r in install_replies]
    routes += [
        StubRoute("GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})),
        StubRoute("GET", "/info", _ok(_lm_info(_VERSION))),
    ]
    return StubTransport(
        routes=routes,
        sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
    )


def test_http_200_with_an_error_envelope_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bug this guards: response.ok is True here. Only the in-body status_code
    # says it failed.
    _wire(monkeypatch, _publish_then(_NOT_FOUND))
    with pytest.raises(app.TenantAppError, match="404"):
        app.install(_install_args(install_retry_seconds=0))


def test_install_retries_while_lm_catalog_catches_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh publish is not immediately installable; the retry covers the lag."""
    transport = _wire(
        monkeypatch,
        _publish_then(_NOT_FOUND, _NOT_FOUND, _ok({"deployment_id": "d1"})),
    )
    outcome = app.install(_install_args(install_retry_seconds=600))
    assert outcome.deployment_id == "d1"
    assert len([p for p in transport.paths("POST") if "/install" in p]) == 3


def test_retry_budget_is_respected_and_the_error_explains_the_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _publish_then(_NOT_FOUND))
    with pytest.raises(app.TenantAppError) as excinfo:
        app.install(_install_args(install_retry_seconds=0))
    assert "snapshot" in str(excinfo.value) and "--install-retry-seconds" in str(
        excinfo.value
    )


def test_already_installed_is_a_no_op_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LM starts no deployment in this case, so there is nothing to poll; the
    # version read-back is what decides success.
    transport = _wire(
        monkeypatch,
        _publish_then(_install_reply("success", 200, "App already installed")),
    )
    outcome = app.install(_install_args())
    assert outcome.deployment_id == ""
    assert outcome.installed_version == _VERSION
    assert not any("/deployments/" in p for p in transport.paths("GET"))


def test_success_without_a_deployment_id_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not "already installed", not an error, but nothing to poll either — that is
    # unexplained, and must not read as a completed install.
    _wire(monkeypatch, _publish_then(_install_reply("success", 200, "queued")))
    with pytest.raises(app.TenantAppError, match="no deployment_id"):
        app.install(_install_args())


@pytest.mark.parametrize(
    "code, message, expected_not_found",
    [
        (404, "App with ID 'x' not found: y", True),
        (200, "App with ID 'x' not found: y", True),
        (500, "internal error", False),
        (200, "App already installed", False),
    ],
)
def test_not_found_detection(code: int, message: str, expected_not_found: bool) -> None:
    reply = app._InstallReply.parse(_install_reply("error", code, message))
    assert reply.not_found is expected_not_found


# ── Version extraction across LM shapes ──────────────────────────────────────


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"version": "a"}, "a"),
        ({"installed_version": "b"}, "b"),
        ({"app_version": "c"}, "c"),
        ({"current_version": "d"}, "d"),
        ({"install": {"version": "e"}}, "e"),
        ({"deployment": {"installed_version": "f"}}, "f"),
        ({}, ""),
        ({"version": "   "}, ""),
        ({"version": 3}, ""),
    ],
)
def test_version_extraction(payload: dict[str, object], expected: str) -> None:
    assert app._extract_version(payload) == expected


def test_version_extraction_degrades_on_a_self_referential_payload() -> None:
    """``data`` is exactly the key a JSON wrapper envelope uses for self-similar
    nesting, so the walk must be depth-bounded: a cyclic payload reads as "not
    installed" ("") rather than crashing the step with a RecursionError.
    """
    payload: dict[str, object] = {"app_id": _APP_ID}
    payload["data"] = payload
    assert app._extract_version(payload) == ""


def test_version_extraction_reads_a_version_within_the_depth_bound() -> None:
    """The bound must not throw away a findable version: nests are searched at
    every level up to it."""
    payload: dict[str, object] = {"data": None}
    inner = payload
    for _ in range(app._WALK_MAX_DEPTH - 1):
        nested: dict[str, object] = {}
        inner["data"] = nested
        inner = nested
    inner["version"] = "1.2.3"
    assert app._extract_version(payload) == "1.2.3"


def test_registered_source_repo_degrades_on_a_self_referential_payload() -> None:
    """Same bound on the repo walk: a cyclic ``data`` envelope reads as "no
    registered repo" ("") instead of a RecursionError."""
    info: dict[str, object] = {"app_id": _APP_ID}
    info["data"] = info
    assert app._registered_source_repo(info) == ""


# ── Outputs ──────────────────────────────────────────────────────────────────


def test_outputs_are_written_to_github_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))
    _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("GET", "/info", _ok({"version": _VERSION}))]),
    )

    assert (
        app.main(
            [
                "verify",
                "--base-url",
                _TENANT,
                "--app-id",
                _APP_ID,
                "--expected",
                _VERSION,
            ]
        )
        == 0
    )
    assert f"installed_version={_VERSION}" in target.read_text()


def test_main_returns_1_and_annotates_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[StubRoute("GET", "/info", _ok({"version": "sdr-test-old"}))]
        ),
    )
    assert (
        app.main(
            [
                "verify",
                "--base-url",
                _TENANT,
                "--app-id",
                _APP_ID,
                "--expected",
                _VERSION,
            ]
        )
        == 1
    )
    assert "::error::" in capsys.readouterr().err


def test_transport_failure_surfaces_as_an_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*_a: object, **_k: object) -> Response:
        raise TenantApiError("could not reach tenant")

    monkeypatch.setattr(TenantClient, "request", _boom)
    assert (
        app.main(
            [
                "verify",
                "--base-url",
                _TENANT,
                "--app-id",
                _APP_ID,
                "--expected",
                _VERSION,
            ]
        )
        == 1
    )
    assert "could not reach tenant" in capsys.readouterr().err


# ── Driver-side input validation ─────────────────────────────────────────────
#
# app_id is a free-text workflow input that lands in a request path, and the
# base URL takes the OAuth secret — both are validated before any API call.


def test_install_rejects_a_malformed_app_id_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _wire(monkeypatch, StubTransport(routes=[]))
    with pytest.raises(TenantApiError, match="invalid app_id"):
        app.install(_install_args(app_id="../../admin"))
    assert transport.calls == [], "no tenant call may leave before validation"


def test_verify_rejects_a_plaintext_base_url_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _wire(monkeypatch, StubTransport(routes=[]))
    with pytest.raises(TenantApiError, match="invalid tenant base URL"):
        app.verify(
            argparse.Namespace(base_url="http://x", app_id=_APP_ID, expected="1")
        )
    assert transport.calls == []


# ── Error-body rendering ─────────────────────────────────────────────────────


def test_render_body_truncates_a_verbose_error_page() -> None:
    body = "x" * 10000
    rendered = app._render_body(body)
    assert len(rendered) <= app._ERROR_BODY_CHARS + len("…(truncated)")
    assert rendered.endswith("…(truncated)")


def test_render_body_leaves_a_short_body_intact() -> None:
    assert app._render_body({"error": "nope"}) == repr({"error": "nope"})


def test_publish_error_body_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(404, {})),
                StubRoute("POST", "/publish", Response(500, "y" * 10000)),
            ]
        ),
    )
    with pytest.raises(app.TenantAppError) as excinfo:
        app.install(_install_args())
    assert len(str(excinfo.value)) < 5000


# ── Tenant-ID scoping ────────────────────────────────────────────────────────


def test_install_refuses_a_hostname_as_the_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail before publishing, not after the install cannot find the version.

    A hostname in `allowed_tenants` publishes successfully and produces a release
    visible to no tenant, so the symptom appears one call later as "version not
    found" — with the tenant's real versions listed, which reads like a lag rather
    than a scoping mistake. Three live runs were spent on that.
    """
    transport = _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("GET", "/info", _ok({}))]),
    )
    with pytest.raises(TenantApiError, match="hostname"):
        app.install(_install_args(tenant="e2e-azure-main.atlan.com"))
    assert not any("/marketplace/publish" in p for p in transport.paths("POST")), (
        "the bad tenant id must be caught BEFORE the publish, or it leaves a "
        "release behind that is visible to nobody"
    )


# ── Failure diagnostics ──────────────────────────────────────────────────────
# The previous implementation printed `json.dumps(payload, sort_keys=True)[:8000]`,
# which sorts `pod_describe` ahead of `pod_events` — so the cut landed before the
# events on every failure, and the events are the one section that names an
# image-pull problem. Three FND-31 live runs were misdiagnosed behind that
# truncation, so the ordering, the budget's visibility and the events fallback are
# asserted rather than assumed.


_EVENTS_TEXT = (
    "LAST SEEN   TYPE      REASON   OBJECT          MESSAGE\n"
    "30s         Warning   Failed   pod/openapi-0   Failed to pull image "
    '"ghcr.io/atlanhq/atlan-openapi-app:sdr-test-abc12345": no matching manifest '
    "for linux/arm64 in the manifest list entries"
)


def _snapshot_response(**fields: str) -> Response:
    """LM's /failure shape: the snapshot nested under `snapshot`."""
    return _ok({"app_id": _APP_ID, "deployment_id": "dep-1", "snapshot": fields})


def _dump(monkeypatch: pytest.MonkeyPatch, *routes: StubRoute) -> StubTransport:
    transport = _wire(monkeypatch, StubTransport(routes=list(routes)))
    app._dump_failure(TenantClient(base_url=_TENANT, bearer="t"), _APP_ID)
    return transport


def test_pod_events_are_printed_before_pod_describe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _dump(
        monkeypatch,
        StubRoute(
            "GET",
            "/failure",
            _snapshot_response(
                failure_reason="HelmRelease not ready",
                pod_events=_EVENTS_TEXT,
                pod_describe="Name: openapi-0\n" + "detail\n" * 50,
            ),
        ),
        StubRoute("GET", "/events", _ok({"events": ""})),
    )
    out = capsys.readouterr().out
    assert out.index("--- pod_events") < out.index("--- pod_describe"), (
        "pod_describe must not precede pod_events: with a budget in play, "
        "whichever comes first is what survives, and the events are the section "
        "that names an unpullable image"
    )
    assert "no matching manifest for linux/arm64" in out


def test_the_platform_mismatch_names_its_cause(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _dump(
        monkeypatch,
        StubRoute("GET", "/failure", Response(404, {"detail": "none"})),
        StubRoute("GET", "/events", _ok({"events": _EVENTS_TEXT})),
    )
    out = capsys.readouterr().out
    assert "::error::" in out and "multi-arch" in out
    assert "platforms" in out, "the hint must name the input that fixes it"


def test_no_platform_hint_on_an_unrelated_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _dump(
        monkeypatch,
        StubRoute(
            "GET",
            "/failure",
            _snapshot_response(
                failure_reason="CrashLoopBackOff",
                pod_logs="Traceback...\nKeyError: 'credential_guid'",
            ),
        ),
        StubRoute("GET", "/events", _ok({"events": "nothing to report"})),
    )
    out = capsys.readouterr().out
    assert "multi-arch" not in out, (
        "a hint that fires on every failure is noise; it must key on the "
        "registry's own wording"
    )
    assert "KeyError: 'credential_guid'" in out


def test_live_events_are_read_when_no_snapshot_was_captured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A timeout leaves no snapshot, which is when diagnostics matter most."""
    transport = _dump(
        monkeypatch,
        StubRoute("GET", "/failure", Response(404, {"detail": "no snapshot"})),
        StubRoute("GET", "/events", _ok({"events": _EVENTS_TEXT})),
    )
    out = capsys.readouterr().out
    assert "live namespace events" in out
    assert "no matching manifest" in out
    assert any("/events" in path for path in transport.paths("GET"))


def test_diagnostics_never_mask_the_real_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both diagnostic routes failing must warn, not raise over the real cause."""

    def _boom(*_a: object, **_k: object) -> Response:
        raise TenantApiError("tenant unreachable")

    monkeypatch.setattr(TenantClient, "request", _boom)
    app._dump_failure(TenantClient(base_url=_TENANT, bearer="t"), _APP_ID)
    assert capsys.readouterr().out.count("::warning::") == 2


def test_an_unrecognised_snapshot_field_is_still_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The section list must not become a whitelist that hides new data."""
    _dump(
        monkeypatch,
        StubRoute(
            "GET",
            "/failure",
            _snapshot_response(
                failure_reason="nope", flux_suspend_reason="operator-suspended"
            ),
        ),
        StubRoute("GET", "/events", _ok({"events": ""})),
    )
    out = capsys.readouterr().out
    assert "flux_suspend_reason" in out and "operator-suspended" in out


def test_a_flat_snapshot_body_is_read_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """LM nests under `snapshot`; tolerate a flat body rather than printing nothing."""
    _dump(
        monkeypatch,
        StubRoute("GET", "/failure", _ok({"pod_events": _EVENTS_TEXT})),
        StubRoute("GET", "/events", _ok({"events": ""})),
    )
    assert "no matching manifest" in capsys.readouterr().out


def test_an_over_long_section_says_what_it_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app._print_block("pod_logs", "\n".join(f"line {i}" for i in range(500)), 10)
    out = capsys.readouterr().out
    assert "showing tail 10 of 500 lines" in out, (
        "a silent cut is what hid the events for three runs; the budget must "
        "announce itself"
    )
    assert "line 499" in out and "line 0\n" not in out


def test_a_section_within_budget_is_printed_whole(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app._print_block("pod_events", "a\nb\nc", 10)
    out = capsys.readouterr().out
    assert "showing" not in out
    assert out.splitlines()[1:] == ["a", "b", "c"]


def test_an_empty_section_prints_no_header(capsys: pytest.CaptureFixture[str]) -> None:
    app._print_block("pod_logs", "   \n\n", 10)
    assert capsys.readouterr().out == ""


# ── Whose pod is actually failing ────────────────────────────────────────────
# LM's health check is namespace-scoped: it reports "Pods failed in namespace
# <ns>: <pod>" for ANY unhealthy pod in the app's namespace, so an orphan left by
# an earlier version fails every later install to that tenant. Observed on the
# first green multi-arch install — our pods pulled the image in 12.4s and were
# scaled to zero by KEDA, while a pod from an earlier attempt sat in
# ImagePullBackOff on a different tag (x1048 over 3h59m) and took the verdict with
# it.
#
# The direction that matters is the SAFE one: our own image among the failures, or
# a read-back that disagrees, must still fail. The override is narrow.

_ORPHAN = "ghcr.io/atlanhq/atlan-openapi-app:sdr-test-1c232889"
_ORPHAN_EVENTS = (
    f'9s Normal BackOff pod/openapi-d4849884b-7cwdp Back-off pulling image "{_ORPHAN}"\n'
    f"20s Warning Failed pod/openapi-d4849884b-7cwdp Error: ImagePullBackOff\n"
    f'75s Normal Pulled pod/openapi-worker-abc Successfully pulled image "{_IMAGE}" in 12.4s'
)


def test_a_successfully_pulled_image_is_not_read_as_failing() -> None:
    """The inverting mistake: our image appears in the same event stream.

    It is there on a `Successfully pulled` line. Counting that as a failure would
    make every namespace look like ours is broken, which turns the override off
    exactly when it is needed.
    """
    assert app.failing_images(_ORPHAN_EVENTS) == [_ORPHAN]
    assert app.foreign_failure(_ORPHAN_EVENTS, _IMAGE) == [_ORPHAN]


def test_our_own_image_failing_is_never_foreign() -> None:
    events = f'Failed to pull image "{_IMAGE}": manifest unknown'
    assert app.foreign_failure(events, _IMAGE) == [], (
        "when our own image is among the failures the verdict IS about us, and "
        "downgrading it would turn a real broken install into a green run"
    )


_DIGEST = "sha256:" + "a1" * 32


def test_our_image_failing_by_digest_is_never_foreign() -> None:
    """Kubelet can report the failure pinned while --image arrives as a tag.

    Exact string equality reads that as foreign — the one misread this override
    must never make, since it turns a broken install of OUR image green.
    """
    repo = _IMAGE.rpartition(":")[0]
    for pinned in (f"{repo}@{_DIGEST}", f"{_IMAGE}@{_DIGEST}"):
        events = f'Failed to pull image "{pinned}": manifest unknown'
        assert app.foreign_failure(events, _IMAGE) == []


def test_our_image_failing_by_tag_is_never_foreign_when_we_pass_a_digest() -> None:
    """The same ambiguity, mirrored: we hand over a digest, kubelet names the tag."""
    repo, _, tag = _IMAGE.rpartition(":")
    ours = f"{repo}@{_DIGEST}"
    events = f'Back-off pulling image "{repo}:{tag}"'
    assert app.foreign_failure(events, ours) == []


def test_a_mix_of_our_digest_and_an_orphan_is_never_foreign() -> None:
    """One ambiguous own failure keeps the whole verdict ours, orphans or not."""
    repo = _IMAGE.rpartition(":")[0]
    events = (
        f'Back-off pulling image "{_ORPHAN}"\n'
        f'Failed to pull image "{repo}@{_DIGEST}": manifest unknown'
    )
    assert app.foreign_failure(events, _IMAGE) == []


def test_another_tag_of_our_own_repository_is_foreign() -> None:
    """The override's whole point is distinguishing tags of the SAME repository."""
    other_tag = _IMAGE.rpartition(":")[0] + ":sdr-test-older999"
    events = f'Back-off pulling image "{other_tag}"'
    assert app.foreign_failure(events, _IMAGE) == [other_tag]


def test_image_repository_identity() -> None:
    repo = _IMAGE.rpartition(":")[0]
    assert app._image_repository(_IMAGE) == repo
    assert app._image_repository(f"{repo}@{_DIGEST}") == repo
    assert app._image_repository(f"{_IMAGE}@{_DIGEST}") == repo
    # An untagged reference must not lose its final segment to the tag rule.
    assert app._image_repository(repo) == repo


def test_image_tag_extraction() -> None:
    repo, _, tag = _IMAGE.rpartition(":")
    assert app._image_tag(_IMAGE) == tag
    assert app._image_tag(f"{_IMAGE}@{_DIGEST}") == tag
    # A colon-free or digest-only reference has NO tag — rpartition on one hands
    # back the whole string, which must not be read as a tag.
    assert app._image_tag(repo) == ""
    assert app._image_tag(f"{repo}@{_DIGEST}") == ""


def test_a_registry_port_is_never_read_as_a_tag() -> None:
    """``ghcr.io:5000/org/repo`` carries its colon left of the final slash.

    Reading that port colon as the tag separator parses the repository as bare
    ``ghcr.io`` — which compares unequal to our own ported reference and reads
    OUR failing image as foreign, the one misread the override must never make.
    """
    ported = "ghcr.io:5000/atlanhq/atlan-openapi-app"
    assert app._image_repository(f"{ported}:sdr-test-abc123") == ported
    assert app._image_repository(f"{ported}@{_DIGEST}") == ported
    assert app._image_repository(f"{ported}:sdr-test-abc123@{_DIGEST}") == ported
    assert app._image_repository(ported) == ported
    assert app._image_tag(f"{ported}:sdr-test-abc123") == "sdr-test-abc123"
    assert app._image_tag(f"{ported}@{_DIGEST}") == ""
    assert app._image_tag(f"{ported}:sdr-test-abc123@{_DIGEST}") == "sdr-test-abc123"
    assert app._image_tag(ported) == ""


def test_our_ported_image_failing_by_digest_is_never_foreign() -> None:
    """The ported-registry misread, end to end: tag-form --image, digest-form failure."""
    ported = "ghcr.io:5000/atlanhq/atlan-openapi-app"
    events = f'Failed to pull image "{ported}@{_DIGEST}": manifest unknown'
    assert app.foreign_failure(events, f"{ported}:sdr-test-abc123") == []


def test_a_mix_of_ours_and_an_orphan_is_never_foreign() -> None:
    events = (
        f'Back-off pulling image "{_ORPHAN}"\n'
        f'Failed to pull image "{_IMAGE}": manifest unknown'
    )
    assert app.foreign_failure(events, _IMAGE) == []


def test_unreadable_diagnostics_are_never_foreign() -> None:
    # Nothing identifiable failing means no evidence to override LM with.
    for text in ("", "deployment failed", "Pods failed in namespace openapi-app"):
        assert app.foreign_failure(text, _IMAGE) == []


def test_an_orphan_failure_passes_when_the_readback_agrees(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workaround, and it still rests on direct evidence."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(404, {})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET",
                    "/deployments/",
                    _ok(
                        {
                            "deployment_status": "FAILED",
                            "message": "Pods failed in namespace openapi-app: openapi-d4849884b-7cwdp",
                        }
                    ),
                ),
                StubRoute("GET", "/failure", Response(404, {})),
                StubRoute("GET", "/events", _ok({"events": _ORPHAN_EVENTS})),
                # The authority: the tenant really does serve what we installed.
                StubRoute("GET", "/info", _ok({"version": _VERSION})),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(404, {}))],
        ),
    )
    outcome = app.install(_install_args())
    assert outcome.installed_version == _VERSION
    out = capsys.readouterr().out
    # The orphan must be named on the WARNING line itself: `_ORPHAN` also shows
    # up in the echoed diagnostic events, so a whole-output `in` check passes
    # even if the ::warning:: never mentioned the foreign image.
    assert any("::warning::" in line and _ORPHAN in line for line in out.splitlines())
    assert "cleaned up" in out, (
        "a tolerated orphan must still be reported — it fails this check on "
        "every future install until someone deletes it"
    )


def test_an_orphan_failure_still_fails_when_the_readback_disagrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The override moves the decision to the read-back; it does not skip it."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(404, {})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute("GET", "/deployments/", _ok({"deployment_status": "FAILED"})),
                StubRoute("GET", "/failure", Response(404, {})),
                StubRoute("GET", "/events", _ok({"events": _ORPHAN_EVENTS})),
                # Tenant still on the old version: the install did NOT take.
                StubRoute("GET", "/info", _ok({"version": "older"})),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(404, {}))],
        ),
    )
    with pytest.raises(app.TenantAppError, match="cannot be confirmed"):
        app.install(_install_args())


def test_a_timeout_is_never_downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only LM's FAILED verdict is namespace-scoped. A timeout is our problem.

    `DeploymentFailed` exists to keep these apart: catching the base error here
    would let an accepted-but-never-reconciled deploy pass whenever the namespace
    happened to contain an orphan.
    """
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(404, {})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
            ],
            sticky=[
                StubRoute("GET", "/releases/", Response(404, {})),
                StubRoute("GET", "/failure", Response(404, {})),
                StubRoute("GET", "/events", _ok({"events": _ORPHAN_EVENTS})),
            ],
        ),
    )
    with pytest.raises(app.TenantAppError, match="terminal state"):
        app.install(_install_args(timeout_seconds=0))


def test_deployment_failed_is_a_tenant_app_error() -> None:
    # Callers catching TenantAppError (main(), the workflows) must keep working.
    assert issubclass(app.DeploymentFailed, app.TenantAppError)


# ── "unknown" is not a version ───────────────────────────────────────────────
# LM's own code: `version_text = attributes.get("atlanAppCurrentVersion",
# "unknown")` (tenant_apps_manager/store/tenant_app_store.py), commented
# "Semantic version (if available)". The Atlas attribute is optional, so a
# perfectly reconciled install can still report the placeholder — a live azure
# tenant did exactly that after a SUCCEEDED deployment.


@pytest.mark.parametrize("placeholder", ["unknown", "UNKNOWN", " unknown ", "n/a"])
def test_a_placeholder_reads_as_no_version(placeholder: str) -> None:
    payload = {"installed": {"version_text": placeholder}}
    assert app._extract_version(payload) == "", (
        "a placeholder must read as absent. Returning it turns 'the tenant "
        "cannot tell us what it runs' into 'the tenant runs something called "
        "unknown', which reads like a mismatch and hides the real problem"
    )


def test_a_real_version_alongside_a_placeholder_still_wins() -> None:
    # Order matters: version_text is checked first and is the placeholder here.
    payload = {"installed": {"version_text": "unknown", "version": _VERSION}}
    assert app._extract_version(payload) == _VERSION


# ── Reading app_id without PyYAML ────────────────────────────────────────────
# The two call sites do not share an interpreter: prepare-tenant runs on the
# runner's system Python (PyYAML present), the per-leg verify runs after
# `uv sync` has put a project venv on PATH (PyYAML absent). Requiring the package
# meant the version check — the last gate before pytest — died on an import in
# every e2e leg while working perfectly in the job before it.


@pytest.fixture
def _no_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `import yaml` fail, as it does inside a connector's venv."""
    real_import = builtins.__import__

    def _fail(name: str, *args: object, **kwargs: object) -> object:
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _fail)


@pytest.mark.usefixtures("_no_yaml")
def test_app_id_is_read_without_pyyaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "atlan.yaml").write_text(
        f"name: openapi\ndisplay_name: OpenAPI\napp_id: {_APP_ID}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert app.resolve_app_id("") == _APP_ID


@pytest.mark.parametrize(
    "line, expected",
    [
        (f"app_id: {_APP_ID}", _APP_ID),
        (f'app_id: "{_APP_ID}"', _APP_ID),
        (f"app_id: '{_APP_ID}'", _APP_ID),
        (f"app_id:    {_APP_ID}   # the registered id", _APP_ID),
        (f"app_id:\t{_APP_ID}", _APP_ID),
    ],
)
def test_the_scan_handles_the_shapes_atlan_yaml_uses(line: str, expected: str) -> None:
    assert app._scan_app_id(f"name: openapi\n{line}\nport: 8000\n") == expected


def test_an_indented_app_id_is_never_picked_up() -> None:
    """The wrong-app direction, and the only one that could pass silently.

    A nested `app_id` belongs to some inner stanza. Installing against it would
    look entirely successful while targeting a different app.
    """
    text = "name: openapi\nentrypoints:\n  - name: sync\n    app_id: 00000000-dead-beef-0000-000000000000\n"
    assert app._scan_app_id(text) == ""


def test_a_commented_out_app_id_is_not_read() -> None:
    assert app._scan_app_id(f"# app_id: {_APP_ID}\nname: openapi\n") == ""


def test_a_missing_app_id_scans_to_empty() -> None:
    assert app._scan_app_id("name: openapi\nport: 8000\n") == ""


@pytest.mark.usefixtures("_no_yaml")
def test_a_missing_app_id_still_errors_without_pyyaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The fallback must not turn "no app_id" into a silent empty id.
    (tmp_path / "atlan.yaml").write_text("name: openapi\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(app.TenantAppError, match="no app_id"):
        app.resolve_app_id("")


@pytest.mark.usefixtures("_no_yaml")
def test_a_mis_scan_cannot_reach_a_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """validate_app_id is the backstop: a non-UUID never becomes a request path."""
    (tmp_path / "atlan.yaml").write_text("app_id: not-a-uuid\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TenantApiError, match="invalid app_id"):
        app.verify(argparse.Namespace(base_url=_TENANT, app_id="", expected=_VERSION))


def test_pyyaml_is_still_preferred_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A real parser handles shapes the scan does not; it stays the primary path.
    (tmp_path / "atlan.yaml").write_text(
        f"app_id: {_APP_ID}\nnested:\n  app_id: wrong\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert app.resolve_app_id("") == _APP_ID


# ── Resolving the version through the catalog ────────────────────────────────
# Taken from a live azure tenant whose version_text was the placeholder:
#
#   catalog.app_version.version_id = '019fdc06-dbe3-7992-93a0-1791575164b3'
#   catalog.app_version.version    = 'sdr-test-1024d47f'
#   installed.version_id           = '019fdc06-dbe3-7992-93a0-1791575164b3'
#
# Matching UUIDs make this exact identity. A MISMATCH must never resolve: /info
# describes the LATEST catalog version, so reading its string regardless would
# report the newest version as installed — the silent wrong-version pass FND-31
# exists to prevent.

_UUID = "019fdc06-dbe3-7992-93a0-1791575164b3"


def _info(installed_id: str, version: str = _VERSION, text: str = "unknown") -> dict:  # type: ignore[type-arg]
    return {
        "catalog": {"app_version": {"version_id": _UUID, "version": version}},
        "installed": {"version_id": installed_id, "version_text": text},
    }


def test_a_matching_uuid_resolves_the_real_version() -> None:
    assert app.resolve_version_via_catalog(_info(_UUID)) == _VERSION


def test_a_mismatched_uuid_never_resolves() -> None:
    assert app.resolve_version_via_catalog(_info("some-other-uuid")) == "", (
        "the catalog describes the LATEST version, not necessarily the installed "
        "one. Resolving on a mismatch would report the newest version as "
        "installed — a silent wrong-version pass."
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"installed": {"version_id": _UUID}},  # no catalog
        {
            "catalog": {"app_version": {"version_id": _UUID, "version": "v"}}
        },  # no installed
        {"catalog": {"app_version": "not-a-dict"}, "installed": {"version_id": _UUID}},
        {
            "catalog": {"app_version": {"version": "v"}},
            "installed": {"version_id": _UUID},
        },
    ],
)
def test_an_incomplete_payload_never_resolves(payload: dict) -> None:  # type: ignore[type-arg]
    assert app.resolve_version_via_catalog(payload) == ""


def test_an_absent_installed_uuid_never_resolves() -> None:
    # Both sides empty would otherwise compare equal and resolve on nothing.
    assert app.resolve_version_via_catalog(_info("")) == ""
    assert (
        app.resolve_version_via_catalog(
            {
                "catalog": {"app_version": {"version_id": "", "version": "v"}},
                "installed": {"version_id": ""},
            }
        )
        == ""
    )


def test_a_placeholder_in_the_catalog_does_not_resolve() -> None:
    assert app.resolve_version_via_catalog(_info(_UUID, version="unknown")) == ""


def test_the_readback_uses_the_catalog_when_version_text_is_a_placeholder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: the exact shape the live tenant returned."""
    _wire(
        monkeypatch,
        StubTransport(routes=[StubRoute("GET", "/info", _ok(_info(_UUID)))]),
    )
    resolved = app._installed_version(
        TenantClient(base_url=_TENANT, bearer="t"), _APP_ID
    )
    assert resolved == _VERSION
    out = capsys.readouterr().out
    assert "resolved to" in out and "version_id" in out, (
        "resolving via a fallback must say so — a reader comparing this against "
        "the tenant UI should know which field the answer came from"
    )
    assert "no version could be read" not in out


def test_the_info_shape_is_dumped_when_no_version_can_be_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point: decide the next step from the payload, not a guess."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "GET",
                    "/info",
                    _ok(
                        {
                            "app_id": _APP_ID,
                            "catalog": {
                                "app_name": "openapi",
                                "app_version": {
                                    "version_id": "019fdc06-dbe3-7992",
                                    "version": _VERSION,
                                    "image_url": _IMAGE,
                                    "config": "a: 1\nb: 2\n",
                                },
                            },
                            # A DIFFERENT UUID, so the catalog cannot resolve it
                            # — the catalog only ever describes the latest
                            # version. This is the unresolvable case, which is
                            # exactly when the dump has to appear.
                            "installed": {
                                "version_id": "a-different-uuid",
                                "version_text": "unknown",
                                "deployment_name": "production",
                            },
                        }
                    ),
                )
            ]
        ),
    )
    assert (
        app._installed_version(TenantClient(base_url=_TENANT, bearer="t"), _APP_ID)
        == ""
    )
    out = capsys.readouterr().out
    assert "app info (no version could be read)" in out
    # The identifiers a reader needs to work out what the tenant runs.
    assert "installed.version_id = 'a-different-uuid'" in out
    assert f"catalog.app_version.version = '{_VERSION}'" in out
    assert f"catalog.app_version.image_url = '{_IMAGE}'" in out
    # NOT the config blob: burying the identifiers is how the last diagnostic
    # gap happened.
    assert "a: 1" not in out


def test_no_dump_when_the_version_reads_fine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "GET", "/info", _ok({"installed": {"version_text": _VERSION}})
                )
            ]
        ),
    )
    app._installed_version(TenantClient(base_url=_TENANT, bearer="t"), _APP_ID)
    assert "no version could be read" not in capsys.readouterr().out


def test_verify_distinguishes_unreadable_from_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unverifiable tenant and a wrong-version tenant need different hunts."""
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute(
                    "GET", "/info", _ok({"installed": {"version_text": "unknown"}})
                )
            ]
        ),
    )
    with pytest.raises(app.TenantAppError) as excinfo:
        app.verify(
            argparse.Namespace(base_url=_TENANT, app_id=_APP_ID, expected=_VERSION)
        )
    message = str(excinfo.value)
    assert "did not report a version at all" in message
    assert "concurrent e2e run" not in message, (
        "the concurrent-run hint is for a genuine mismatch; offering it here "
        "sends the reader after a race that did not happen"
    )


def test_install_fails_when_it_cannot_confirm_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare-tenant must fail here rather than leave it to every leg.

    It went green while reporting a placeholder, and both e2e legs then failed on
    their own version check — N confusing failures instead of one clear one. This
    job's whole purpose is leaving the tenant on the version under test, so it has
    to confirm that before reporting success.
    """
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(404, {})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                # Reconciled fine, but reports a placeholder no catalog can resolve.
                StubRoute("GET", "/info", _ok(_info("a-different-uuid"))),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(404, {}))],
        ),
    )
    with pytest.raises(app.TenantAppError) as excinfo:
        app.install(_install_args())
    message = str(excinfo.value)
    assert "cannot be confirmed" in message
    assert (
        "no usable version" in message
    ), "an unverifiable tenant and a wrong-version tenant need different hunts"


def test_install_succeeds_when_the_catalog_resolves_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", Response(404, {})),
                StubRoute("POST", "/marketplace/publish", _ok({"version_id": "v1"})),
                StubRoute("POST", "/install", _ok({"deployment_id": "d1"})),
                StubRoute(
                    "GET", "/deployments/", _ok({"deployment_status": "SUCCEEDED"})
                ),
                StubRoute("GET", "/info", _ok(_info(_UUID))),
            ],
            sticky=[StubRoute("GET", "/releases/", Response(404, {}))],
        ),
    )
    assert app.install(_install_args()).installed_version == _VERSION


def test_verify_still_names_the_race_on_a_real_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        StubTransport(
            routes=[
                StubRoute("GET", "/info", _ok({"installed": {"version_text": "older"}}))
            ]
        ),
    )
    with pytest.raises(app.TenantAppError, match="concurrent e2e run"):
        app.verify(
            argparse.Namespace(base_url=_TENANT, app_id=_APP_ID, expected=_VERSION)
        )
