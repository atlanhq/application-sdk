"""Tests for .github/scripts/e2e_tenant_api.py.

Two things matter here beyond the obvious parsing: that a credential can never
leak into a log line, and that an unreadable response fails loudly rather than
silently reading as "no version" (which would compare equal to another absent
version and pass).
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import e2e_tenant_api as api  # noqa: E402

_TENANT = "https://example-tenant.atlan.test"
_SECRET = "super-secret-value"


# ── Response ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status, ok",
    [(200, True), (201, True), (299, True), (300, False), (404, False), (500, False)],
)
def test_ok_covers_exactly_the_2xx_range(status: int, ok: bool) -> None:
    assert api.Response(status=status, body={}).ok is ok


def test_data_unwraps_the_envelope_when_present() -> None:
    response = api.Response(status=200, body={"data": {"version_id": "v1"}})
    assert response.data() == {"version_id": "v1"}


def test_data_returns_the_object_when_there_is_no_envelope() -> None:
    # Heracles wraps some payloads and returns others bare; both must work.
    response = api.Response(status=200, body={"version_id": "v1"})
    assert response.data() == {"version_id": "v1"}


def test_data_ignores_a_non_object_envelope() -> None:
    response = api.Response(status=200, body={"data": "not-an-object", "x": 1})
    assert response.data() == {"data": "not-an-object", "x": 1}


def test_json_raises_on_a_text_body() -> None:
    # A gateway error page instead of JSON must not read as an empty object —
    # that would look like "installed version absent" and compare equal to
    # another absent version.
    response = api.Response(status=502, body="<html>bad gateway</html>")
    with pytest.raises(api.TenantApiError, match="JSON object"):
        response.json()


# ── Client basics ────────────────────────────────────────────────────────────


def test_trailing_slash_is_stripped_so_paths_do_not_double_up() -> None:
    assert api.TenantClient(base_url=f"{_TENANT}/", bearer="t").base_url == _TENANT


# ── Base-URL validation ──────────────────────────────────────────────────────
#
# The client POSTs the OAuth client secret to {base_url}/... and sends every
# bearer there, so a misconfigured matrix value must fail before any request.


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example-tenant.atlan.test",  # plaintext would leak the pair
        "https://",  # no host
        "not-a-url",
        "https://user:pw@example-tenant.atlan.test",  # userinfo
        "https://example-tenant.atlan.test?x=1",  # query
        "https://example-tenant.atlan.test#frag",  # fragment
    ],
)
def test_base_url_is_rejected_before_any_request(base_url: str) -> None:
    with pytest.raises(api.TenantApiError, match="invalid tenant base URL"):
        api.TenantClient(base_url=base_url, bearer="t")


def test_mint_rejects_a_plaintext_base_url() -> None:
    with pytest.raises(api.TenantApiError, match="invalid tenant base URL"):
        api.mint_oauth_token("http://example-tenant.atlan.test", "i", "s")


# ── Path-segment handling ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "app_id",
    [
        "019d1f6b-6fea-7db3-96d8-e61e159d0351",
        "019D1F6B-6FEA-7DB3-96D8-E61E159D0351",  # upper-case hex is still a UUID
    ],
)
def test_validate_app_id_accepts_uuid_shapes(app_id: str) -> None:
    assert api.validate_app_id(app_id) == app_id


@pytest.mark.parametrize(
    "app_id",
    [
        "",
        "not-a-uuid",
        "../../admin",  # would rewrite the request path if formatted verbatim
        "019d1f6b-6fea-7db3-96d8-e61e159d0351/extra",
        "019d1f6b-6fea-7db3-96d8-e61e159d0351?force=true",
    ],
)
def test_validate_app_id_rejects_non_uuid_shapes(app_id: str) -> None:
    with pytest.raises(api.TenantApiError, match="invalid app_id"):
        api.validate_app_id(app_id)


def test_path_segment_quotes_everything_but_unreserved() -> None:
    assert api.path_segment("a/b?c#d") == "a%2Fb%3Fc%23d"
    assert api.path_segment("plain-value_1.2~3") == "plain-value_1.2~3"


def test_repr_never_contains_the_bearer() -> None:
    client = api.TenantClient(base_url=_TENANT, bearer=_SECRET)
    assert _SECRET not in repr(client)


def test_transport_failure_names_the_host_not_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(api.urllib.request, "urlopen", _boom)
    client = api.TenantClient(base_url=_TENANT, bearer=_SECRET)
    with pytest.raises(api.TenantApiError) as excinfo:
        client.get("/api/service/marketplace/apps/x/info")
    message = str(excinfo.value)
    assert _TENANT in message
    assert _SECRET not in message


# ── Tenant ID vs hostname ────────────────────────────────────────────────────
# `allowed_tenants` is matched EXACTLY by GM against the tenant's vcluster
# instance name. A hostname publishes fine and yields a release visible to no
# tenant, so the failure lands much later as "version not found" on install —
# three live runs were lost to that, hence a fail-fast check.


@pytest.mark.parametrize(
    "tenant_id",
    [
        "markeznp37",
        "home-mt",
        "e2e-azure-main",
        # Vcluster instance names are DNS subdomains and may legally contain
        # dots; only a scheme or a known Atlan host suffix marks a hostname.
        "team.a",
        "tenant.example.com",
    ],
)
def test_valid_tenant_ids_pass(tenant_id: str) -> None:
    assert api.validate_tenant_id(tenant_id) == tenant_id


@pytest.mark.parametrize(
    "bad",
    [
        "e2e-azure-main.atlan.com",
        "https://e2e-azure-main.atlan.com",
        "e2e-azure-main.atlan.dev",
    ],
)
def test_hostname_shaped_tenant_ids_are_refused(bad: str) -> None:
    with pytest.raises(api.TenantApiError, match="hostname"):
        api.validate_tenant_id(bad)


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_tenant_id_is_refused(empty: str) -> None:
    # Empty would publish a release scoped to nothing at all.
    with pytest.raises(api.TenantApiError, match="tenant"):
        api.validate_tenant_id(empty)


def test_tenant_id_is_trimmed() -> None:
    assert api.validate_tenant_id("  markeznp37  ") == "markeznp37"


# ── Token mint ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "client_id, client_secret, missing",
    [("", "s", "client_id"), ("i", "", "client_secret"), ("  ", "s", "client_id")],
)
def test_mint_refuses_empty_credentials_naming_the_field(
    client_id: str, client_secret: str, missing: str
) -> None:
    # An empty bearer would fail later as an opaque 401 far from its cause; the
    # usual root cause is the tenant-matrix secret not being shared with the repo.
    with pytest.raises(api.TenantApiError, match=missing):
        api.mint_oauth_token(_TENANT, client_id, client_secret)


def test_mint_error_reports_the_status_but_not_the_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A token endpoint's error body can echo request parameters, so only the
    # status is safe to surface.
    def _unauthorized(*_a: object, **_k: object) -> None:
        raise urllib.error.HTTPError(
            url=_TENANT,
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    monkeypatch.setattr(api.urllib.request, "urlopen", _unauthorized)
    with pytest.raises(api.TenantApiError) as excinfo:
        api.mint_oauth_token(_TENANT, "client-id", _SECRET)
    message = str(excinfo.value)
    assert "401" in message
    assert _SECRET not in message


def test_mint_reports_key_names_when_access_token_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        status = 200

        def read(self) -> bytes:
            return json.dumps({"refresh_token": _SECRET, "scope": "x"}).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    with pytest.raises(api.TenantApiError) as excinfo:
        api.mint_oauth_token(_TENANT, "client-id", "client-secret")
    message = str(excinfo.value)
    # Names the keys so the shape is diagnosable; never the values, one of which
    # is itself a credential.
    assert "refresh_token" in message
    assert _SECRET not in message


# ── Token role readout ───────────────────────────────────────────────────────


def _jwt(payload: dict[str, object]) -> str:
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"header.{encoded}.signature"


def test_roles_are_reported_sorted() -> None:
    token = _jwt({"realm_access": {"roles": ["b-role", "a-role"]}})
    client = api.TenantClient(base_url=_TENANT, bearer=token)
    assert client.token_roles() == ["a-role", "b-role"]


@pytest.mark.parametrize(
    "bearer",
    [
        "not-a-jwt",
        "only.two",
        "header.!!!not-base64!!!.sig",
        pytest.param(_jwt({}), id="no-realm-access-claim"),
        pytest.param(
            _jwt({"realm_access": "wrong-type"}), id="realm-access-not-object"
        ),
        pytest.param(
            _jwt({"realm_access": {"roles": "wrong-type"}}), id="roles-not-list"
        ),
    ],
)
def test_roles_degrade_to_empty_rather_than_raising(bearer: str) -> None:
    # An opaque API key is not a JWT. This is a diagnostic only — never a gate —
    # so an unreadable token must not break the call it was meant to explain.
    assert api.TenantClient(base_url=_TENANT, bearer=bearer).token_roles() == []


def test_padding_is_restored_for_base64url_without_it() -> None:
    # Keycloak strips '=' padding; a naive decode raises on those tokens.
    payload = {"realm_access": {"roles": ["r"]}, "pad": "abcde"}
    token = _jwt(payload)
    assert "=" not in token.split(".")[1]
    assert api.TenantClient(base_url=_TENANT, bearer=token).token_roles() == ["r"]
