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
