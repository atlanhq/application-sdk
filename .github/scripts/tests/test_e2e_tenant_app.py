"""Tests for .github/scripts/e2e_tenant_app.py and e2e_tenant_api.py.

The HTTP seam is stubbed at ``TenantClient.request`` — the single place every
tenant call funnels through — so the branch logic (converge-by-version, the
credential hint on 401, the scan-gate hint, terminal vs timed-out deployments,
the version-mismatch failure) is exercised for real while nothing leaves the
process.
"""

from __future__ import annotations

import argparse
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
        "repo_url": "",
        "deploy_config": "",
        "self_deployed_runtime": False,
        "sdk_version": "",
        "entrypoints": "",
        "app_configs": "",
        "release_model": "",
        "created_by": "",
        "scan_wait_seconds": 0,
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
            ],
            sticky=[StubRoute("GET", "/releases/", Response(status=404, body={}))],
        ),
    )
    with pytest.raises(app.TenantAppError, match="ImagePullBackOff"):
        app.install(_install_args())
    assert any("/failure" in p for p in transport.paths("GET")), (
        "a failed deploy must pull LM's diagnostics into the step log — that is "
        "the only pod-level detail CI can see without vcluster"
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
                StubRoute("GET", "/failure", Response(status=404, body={})),
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
    app.install(_install_args(repo_url=""))
    assert transport.body_for("/marketplace/publish")["repo"] == repo


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
        app.install(_install_args(repo_url=""))


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
