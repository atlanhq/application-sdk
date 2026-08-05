#!/usr/bin/env python3
"""Tenant HTTP client for the e2e marketplace flows: token mint + request helper.

Why this exists
---------------
FND-31 makes the e2e pipeline install the app under test onto the target tenant,
so the DAG the tests exercise is the version in the PR rather than whatever was
last hand-deployed there. That needs three tenant calls (register a version,
install it, poll the deployment) plus a credential the marketplace routes
accept. This module owns the transport for all of them so the probe driver and
the eventual install driver share ONE implementation of the awkward parts:
which credential to use, how to mint it, and how to surface an API error.

Deliberately stdlib-only. These run in CI jobs that have not yet installed the
SDK (the install has to happen *before* the harness runs), so importing
``application_sdk.credentials.oauth`` is not available at the point of use.

Credentials
-----------
Two different credentials, and the distinction is load-bearing:

* ``ATLAN_API_KEY`` — the long-lived API key. Used by the harness for AE and
  Atlas calls. Its service account carries ``realm-admin``.
* ``SDR_CLIENT_ID`` / ``SDR_CLIENT_SECRET`` — an OAuth client. **This** is the
  credential the marketplace publish route expects; the API key is not
  authorised for it.

The token is minted with a ``client_credentials`` grant against the tenant's
Keycloak, at the same URL the SDK uses (``application_sdk/credentials/atlan.py``,
``application_sdk/execution/_temporal/auth.py``):

    {base_url}/auth/realms/default/protocol/openid-connect/token

Secret hygiene
--------------
The access token is a credential. It is never printed, never returned in a
result object destined for the log, and never written to ``$GITHUB_ENV``. The
only thing callers may surface is :meth:`TenantClient.token_roles`, which
reports the *claim names* a token carries so an authorisation failure is
diagnosable without exposing the bearer. Claims are read by base64-decoding the
JWT payload segment WITHOUT signature verification — this is a diagnostic
readout of a token we just minted ourselves, not an authentication decision, so
there is nothing to verify against and no trust being extended.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Literal

_HTTP_TIMEOUT = 60
_USER_AGENT = "atlan-application-sdk-e2e-tenant/1.0"

#: Heracles mounts its service routes under this prefix.
_SERVICE_PREFIX = "/api/service"

#: Marketplace routes, mirroring `atlan-cli/pkg/voyager/endpoints.go` and
#: `heracles/api/marketplace.json`. Kept as format strings so a caller cannot
#: assemble a path by string-concatenating an unvalidated id into a URL.
PUBLISH_PATH = f"{_SERVICE_PREFIX}/marketplace/publish"
INSTALL_PATH = f"{_SERVICE_PREFIX}/marketplace/tenant/default/apps/{{app_id}}/install"
APP_INFO_PATH = f"{_SERVICE_PREFIX}/marketplace/apps/{{app_id}}/info"
DEPLOYMENT_PATH = f"{_SERVICE_PREFIX}/marketplace/apps/deployments/{{deployment_id}}"
APP_EVENTS_PATH = f"{_SERVICE_PREFIX}/marketplace/apps/{{app_id}}/events"
APP_FAILURE_PATH = f"{_SERVICE_PREFIX}/marketplace/apps/{{app_id}}/failure"
RELEASE_SCAN_PATH = (
    f"{_SERVICE_PREFIX}/marketplace/apps/{{app_id}}/releases/{{release_id}}"
)

#: AE submit / create. `submit=false` creates the workflow without executing it,
#: which still drives Heracles' server-side manifest fetch from the deployed pod.
PACKAGE_WORKFLOWS_PATH = f"{_SERVICE_PREFIX}/package-workflows"

TOKEN_PATH = "/auth/realms/default/protocol/openid-connect/token"

Method = Literal["GET", "POST", "PUT"]


class TenantApiError(RuntimeError):
    """A tenant call failed in a way the caller cannot recover from.

    Carries the status and the parsed body so a driver can render a diagnosable
    message. Never carries a credential.
    """

    def __init__(self, message: str, *, status: int = 0, body: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Response:
    """One tenant HTTP response.

    ``body`` is the parsed JSON when the response was JSON, else the raw text.
    Non-2xx is returned rather than raised: every caller here branches on the
    status (a 409 on install means "already installed", a 4xx on publish means
    "wrong credential"), so raising would just force it back out of an
    exception.
    """

    status: int
    body: object

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> dict[str, object]:
        """Return the body as a JSON object, or raise if it is not one."""
        if isinstance(self.body, dict):
            return self.body
        raise TenantApiError(
            f"expected a JSON object body, got {type(self.body).__name__}",
            status=self.status,
            body=self.body,
        )

    def data(self) -> dict[str, object]:
        """Return the ``data`` envelope when present, else the whole object.

        Heracles wraps some payloads in ``{"data": {...}}`` and returns others
        bare; every caller wants the inner object either way.
        """
        parsed = self.json()
        inner = parsed.get("data")
        return inner if isinstance(inner, dict) else parsed


@dataclass
class TenantClient:
    """Calls one tenant's Heracles routes with one bearer credential.

    Args:
        base_url: Tenant base URL, e.g. ``https://e2e-azure-main.atlan.com``.
            A trailing slash is stripped.
        bearer: The token to send. Use :meth:`with_oauth_token` to build a
            client authenticated by the OAuth client pair (required for
            publish), or pass the API key directly for routes that accept it.
    """

    base_url: str
    bearer: str = field(repr=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    # -- construction ---------------------------------------------------

    @classmethod
    def with_oauth_token(
        cls, base_url: str, client_id: str, client_secret: str
    ) -> TenantClient:
        """Mint a ``client_credentials`` token and return a client using it.

        This is the constructor the marketplace publish route needs: its
        authorisation is on the OAuth client, not on the API key's service
        account.
        """
        return cls(
            base_url=base_url,
            bearer=mint_oauth_token(base_url, client_id, client_secret),
        )

    # -- transport ------------------------------------------------------

    def request(
        self,
        method: Method,
        path: str,
        *,
        body: dict[str, object] | None = None,
        timeout: int = _HTTP_TIMEOUT,
    ) -> Response:
        """Perform one request. Returns non-2xx rather than raising.

        A transport-level failure (DNS, connection refused, timeout) IS raised:
        unlike an HTTP status it carries no information the caller can branch
        on, and retrying is the caller's decision to make explicitly.
        """
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — https URL built from a validated base + a module-owned path constant
        req.add_header("Authorization", f"Bearer {self.bearer}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — see above
                return Response(status=resp.status, body=_parse(resp.read()))
        except urllib.error.HTTPError as exc:
            return Response(status=exc.code, body=_parse(exc.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TenantApiError(
                f"{method} {path} could not reach {self.base_url}: {exc}"
            ) from exc

    def get(self, path: str, **kwargs: object) -> Response:
        return self.request("GET", path, **kwargs)  # type: ignore[arg-type]

    def post(self, path: str, **kwargs: object) -> Response:
        return self.request("POST", path, **kwargs)  # type: ignore[arg-type]

    # -- diagnostics ----------------------------------------------------

    def token_roles(self) -> list[str]:
        """Return the realm roles the bearer carries, for diagnostics only.

        An empty list means the token has no ``realm_access.roles`` claim (or is
        not a JWT — an opaque API key, say), NOT that it is unauthorised. Never
        gate on this; it exists so a 401/403 on publish can be reported as
        "the OAuth client's roles are X" instead of an unexplained rejection.
        """
        claims = _jwt_claims(self.bearer)
        realm_access = claims.get("realm_access")
        if not isinstance(realm_access, dict):
            return []
        roles = realm_access.get("roles")
        if not isinstance(roles, list):
            return []
        return sorted(str(r) for r in roles)


def mint_oauth_token(base_url: str, client_id: str, client_secret: str) -> str:
    """Exchange a client-credentials pair for an access token.

    Raises rather than returning an empty string: every caller needs the token,
    and an empty bearer would fail later as an opaque 401 far from its cause.
    """
    missing = [
        name
        for name, value in (("client_id", client_id), ("client_secret", client_secret))
        if not str(value or "").strip()
    ]
    if missing:
        raise TenantApiError(
            f"cannot mint an OAuth token: {', '.join(missing)} is empty. The "
            "e2e leg resolves these from E2E_TENANT_MATRIX_JSON; an empty value "
            "usually means the secret is not shared with this repository."
        )

    url = f"{base_url.rstrip('/')}{TOKEN_PATH}"
    payload = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    req = urllib.request.Request(url, data=payload, method="POST")  # noqa: S310 — https URL from the caller-validated tenant base
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 — see above
            parsed = _parse(resp.read())
    except urllib.error.HTTPError as exc:
        # The error body can echo request parameters, so report only the status.
        # A 401 here is a bad/rotated client secret; a 404 is usually a wrong
        # realm or a tenant URL that isn't an Atlan tenant.
        raise TenantApiError(
            f"OAuth token mint rejected with HTTP {exc.code} at {url}. 401 means "
            "the client id/secret pair is wrong or rotated; 404 means the realm "
            "or tenant URL is wrong.",
            status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TenantApiError(f"OAuth token mint could not reach {url}: {exc}") from exc

    if not isinstance(parsed, dict):
        raise TenantApiError("OAuth token response was not a JSON object")
    token = parsed.get("access_token")
    if not isinstance(token, str) or not token:
        # Name the keys, never the values — a token response's values are
        # credentials.
        raise TenantApiError(
            "OAuth token response carried no access_token " f"(keys: {sorted(parsed)})"
        )
    return token


def _parse(raw: bytes) -> object:
    """Parse a response body as JSON, falling back to text."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode(errors="replace")


def _jwt_claims(token: str) -> dict[str, object]:
    """Base64-decode a JWT's payload segment. Returns {} for a non-JWT.

    No signature verification, deliberately: this is a readout of a token this
    process just minted, used only to name claims in a diagnostic. It makes no
    authentication or authorisation decision, so there is no trust boundary here.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore stripped base64url padding
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
