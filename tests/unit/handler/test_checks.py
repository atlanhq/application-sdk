"""Unit tests for the thin user-publish-check client (HYP-829).

checks.check_user_publish_preflight is a small async wrapper around Heracles'
``POST /workflows/preflight/user-publish-check``. The user-enabled and
publish-permission logic itself lives server-side in Heracles, so these tests
only cover the client contract: URL/payload/headers, response parsing, and the
non-200 error path.
"""

from __future__ import annotations

import json as _json

import httpx
import pytest

from application_sdk.handler.checks import (
    _default_heracles_url,
    check_user_publish_preflight,
)


def _patch_async_client(monkeypatch, handler):
    """Route httpx.AsyncClient through a MockTransport using the given handler."""
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real_async_client(
            *a, **{**k, "transport": httpx.MockTransport(handler)}
        ),
    )


class TestCheckUserPublishPreflight:
    async def test_posts_expected_payload_and_returns_parsed_body(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = _json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "passed": True,
                    "failed_checks": [],
                    "message": "All user publish preflight checks passed.",
                },
            )

        _patch_async_client(monkeypatch, handler)

        result = await check_user_publish_preflight(
            user_id="user-abc",
            connection_qualified_name="default/snowflake/123",
            bearer_token="svc-tok",
            heracles_url="http://heracles:5201",
        )

        assert result["passed"] is True
        assert captured["url"] == (
            "http://heracles:5201/workflows/preflight/user-publish-check"
        )
        assert captured["auth"] == "Bearer svc-tok"
        assert captured["body"] == {
            "user_id": "user-abc",
            "connection_qualified_name": "default/snowflake/123",
        }

    async def test_trailing_slash_in_heracles_url_is_normalised(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"passed": True, "failed_checks": [], "message": ""})

        _patch_async_client(monkeypatch, handler)

        await check_user_publish_preflight(
            user_id="u",
            connection_qualified_name="c",
            bearer_token="t",
            heracles_url="http://heracles:5201/",
        )
        assert captured["url"] == (
            "http://heracles:5201/workflows/preflight/user-publish-check"
        )

    async def test_passes_through_failed_verdict(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "passed": False,
                    "failed_checks": ["UserEnabled", "AtlanPublishPermission"],
                    "message": "User user-bob failed publish preflight checks",
                },
            )

        _patch_async_client(monkeypatch, handler)

        result = await check_user_publish_preflight(
            user_id="user-bob",
            connection_qualified_name="default/snowflake/123",
            bearer_token="svc-tok",
        )

        assert result["passed"] is False
        assert result["failed_checks"] == ["UserEnabled", "AtlanPublishPermission"]

    async def test_raises_on_non_200(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="service unavailable")

        _patch_async_client(monkeypatch, handler)

        with pytest.raises(ValueError) as exc_info:
            await check_user_publish_preflight(
                user_id="user-abc",
                connection_qualified_name="default/snowflake/123",
                bearer_token="svc-tok",
            )

        assert "503" in str(exc_info.value)


class TestDefaultHeraclesURL:
    def test_prefers_internal_url(self, monkeypatch):
        monkeypatch.setenv("ATLAN_INTERNAL_HERACLES_URL", "http://internal:5201")
        monkeypatch.setenv("HERACLES_URL", "http://other:5201")
        assert _default_heracles_url() == "http://internal:5201"

    def test_falls_back_to_heracles_url(self, monkeypatch):
        monkeypatch.delenv("ATLAN_INTERNAL_HERACLES_URL", raising=False)
        monkeypatch.setenv("HERACLES_URL", "http://other:5201")
        assert _default_heracles_url() == "http://other:5201"

    def test_localhost_default(self, monkeypatch):
        monkeypatch.delenv("ATLAN_INTERNAL_HERACLES_URL", raising=False)
        monkeypatch.delenv("HERACLES_URL", raising=False)
        assert _default_heracles_url() == "http://localhost:5201"
