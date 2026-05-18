"""Unit tests for handler.checks — check_user_enabled and check_atlan_publish_permission.

All HTTP calls are mocked with unittest.mock so no real network is required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from application_sdk.handler.checks import (
    check_atlan_publish_permission,
    check_user_enabled,
)
from application_sdk.handler.contracts import PreflightCheck

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HERACLES = "http://heracles.test"
TOKEN_URL = "http://keycloak.test/token"
CLIENT_ID = "atlan-backend"
CLIENT_SECRET = "secret"
USER_ID = "user-uuid-1234"
BEARER = "svc-token"
CONN_QN = "default/hive/1234567890"


def _mock_response(status_code: int, json_body: object) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


def _async_client_ctx(mock_resp: MagicMock) -> MagicMock:
    """Return a context manager whose .get/.post returns mock_resp."""
    client = AsyncMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.post = AsyncMock(return_value=mock_resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


# ---------------------------------------------------------------------------
# check_user_enabled
# ---------------------------------------------------------------------------


class TestCheckUserEnabled:
    async def test_enabled_user_passes(self):
        resp = _mock_response(
            200, {"username": "alice", "email": "alice@acme.com", "enabled": True}
        )
        ctx, client = _async_client_ctx(resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient", return_value=ctx
        ):
            result = await check_user_enabled(USER_ID, BEARER, heracles_url=HERACLES)

        assert isinstance(result, PreflightCheck)
        assert result.passed is True
        assert result.name == "UserEnabled"
        assert "alice" in result.message
        client.get.assert_awaited_once()

    async def test_disabled_user_fails(self):
        resp = _mock_response(
            200, {"username": "bob", "email": "bob@acme.com", "enabled": False}
        )
        ctx, _ = _async_client_ctx(resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient", return_value=ctx
        ):
            result = await check_user_enabled(USER_ID, BEARER, heracles_url=HERACLES)

        assert result.passed is False
        assert "disabled" in result.message.lower()

    async def test_heracles_404_returns_failed(self):
        resp = _mock_response(404, {"error": "not found"})
        ctx, _ = _async_client_ctx(resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient", return_value=ctx
        ):
            result = await check_user_enabled(USER_ID, BEARER, heracles_url=HERACLES)

        assert result.passed is False
        assert "404" in result.message

    async def test_missing_enabled_field_treated_as_disabled(self):
        # User record with no 'enabled' key — falsy → disabled
        resp = _mock_response(200, {"username": "ghost"})
        ctx, _ = _async_client_ctx(resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient", return_value=ctx
        ):
            result = await check_user_enabled(USER_ID, BEARER, heracles_url=HERACLES)

        assert result.passed is False

    async def test_network_error_returns_failed(self):
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient", return_value=ctx
        ):
            result = await check_user_enabled(USER_ID, BEARER, heracles_url=HERACLES)

        assert result.passed is False
        assert "connection refused" in result.message


# ---------------------------------------------------------------------------
# check_atlan_publish_permission
# ---------------------------------------------------------------------------


class TestCheckAtlanPublishPermission:
    def _make_clients(
        self,
        user_resp: MagicMock,
        token_resp: MagicMock,
        eval_resp: MagicMock,
    ):
        """Three sequential httpx.AsyncClient contexts."""
        clients = []

        for resp in [user_resp, token_resp, eval_resp]:
            client = AsyncMock()
            client.get = AsyncMock(return_value=resp)
            client.post = AsyncMock(return_value=resp)
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=client)
            ctx.__aexit__ = AsyncMock(return_value=False)
            clients.append(ctx)

        side_effects = iter(clients)
        return side_effects

    async def test_all_permissions_granted(self):
        user_resp = _mock_response(200, {"username": "alice", "enabled": True})
        token_resp = _mock_response(200, {"access_token": "impersonation-tok"})
        eval_resp = _mock_response(
            200,
            [
                {"action": "ENTITY_CREATE", "allowed": True},
                {"action": "ENTITY_UPDATE", "allowed": True},
                {"action": "ENTITY_DELETE", "allowed": True},
            ],
        )
        side_effects = self._make_clients(user_resp, token_resp, eval_resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient",
            side_effect=side_effects,
        ):
            results = await check_atlan_publish_permission(
                USER_ID,
                BEARER,
                CONN_QN,
                heracles_url=HERACLES,
                token_url=TOKEN_URL,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
            )

        assert len(results) == 2
        assert all(r.passed for r in results)
        names = {r.name for r in results}
        assert "UserEnabled" in names
        assert "AtlanPublishPermission" in names

    async def test_user_disabled_both_checks_fail(self):
        user_resp = _mock_response(200, {"username": "bob", "enabled": False})
        # Token exchange returns 403 for disabled user
        token_resp = _mock_response(403, {"error": "disabled"})
        eval_resp = _mock_response(200, [])  # never reached
        side_effects = self._make_clients(user_resp, token_resp, eval_resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient",
            side_effect=side_effects,
        ):
            results = await check_atlan_publish_permission(
                USER_ID,
                BEARER,
                CONN_QN,
                heracles_url=HERACLES,
                token_url=TOKEN_URL,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
            )

        user_check = next(r for r in results if r.name == "UserEnabled")
        perm_check = next(r for r in results if r.name == "AtlanPublishPermission")
        assert user_check.passed is False
        assert perm_check.passed is False
        assert "disabled" in perm_check.message.lower()

    async def test_permission_denied_fails(self):
        user_resp = _mock_response(200, {"username": "carol", "enabled": True})
        token_resp = _mock_response(200, {"access_token": "impersonation-tok"})
        eval_resp = _mock_response(
            200,
            [
                {"action": "ENTITY_CREATE", "allowed": False},
                {"action": "ENTITY_UPDATE", "allowed": False},
                {"action": "ENTITY_DELETE", "allowed": True},
            ],
        )
        side_effects = self._make_clients(user_resp, token_resp, eval_resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient",
            side_effect=side_effects,
        ):
            results = await check_atlan_publish_permission(
                USER_ID,
                BEARER,
                CONN_QN,
                heracles_url=HERACLES,
                token_url=TOKEN_URL,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
            )

        perm_check = next(r for r in results if r.name == "AtlanPublishPermission")
        assert perm_check.passed is False
        assert "ENTITY_CREATE" in perm_check.message
        assert "ENTITY_UPDATE" in perm_check.message

    async def test_no_impersonation_creds_skips_permission_check(self):
        user_resp = _mock_response(200, {"username": "dave", "enabled": True})
        ctx, _ = _async_client_ctx(user_resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient", return_value=ctx
        ):
            results = await check_atlan_publish_permission(
                USER_ID,
                BEARER,
                CONN_QN,
                heracles_url=HERACLES,
                # no token_url / client_id / client_secret
            )

        perm_check = next(r for r in results if r.name == "AtlanPublishPermission")
        assert perm_check.passed is True
        assert "skipped" in perm_check.message.lower()

    async def test_bad_client_secret_fails_permission_check(self):
        user_resp = _mock_response(200, {"username": "eve", "enabled": True})
        token_resp = _mock_response(401, {"error": "invalid_client"})
        eval_resp = _mock_response(200, [])
        side_effects = self._make_clients(user_resp, token_resp, eval_resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient",
            side_effect=side_effects,
        ):
            results = await check_atlan_publish_permission(
                USER_ID,
                BEARER,
                CONN_QN,
                heracles_url=HERACLES,
                token_url=TOKEN_URL,
                client_id=CLIENT_ID,
                client_secret="wrong-secret",
            )

        perm_check = next(r for r in results if r.name == "AtlanPublishPermission")
        assert perm_check.passed is False

    async def test_empty_connection_qname_runs_with_empty_entity_list(self):
        # With empty connection_qname the code still does the full token exchange
        # and calls /evaluates — which returns an empty list (no denied entries).
        user_resp = _mock_response(200, {"username": "frank", "enabled": True})
        token_resp = _mock_response(200, {"access_token": "impersonation-tok"})
        eval_resp = _mock_response(200, [])  # no entries → no denials → passes
        side_effects = self._make_clients(user_resp, token_resp, eval_resp)

        with patch(
            "application_sdk.handler.checks.httpx.AsyncClient",
            side_effect=side_effects,
        ):
            results = await check_atlan_publish_permission(
                USER_ID,
                BEARER,
                "",
                heracles_url=HERACLES,
                token_url=TOKEN_URL,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
            )

        perm_check = next(r for r in results if r.name == "AtlanPublishPermission")
        assert perm_check.passed is True
