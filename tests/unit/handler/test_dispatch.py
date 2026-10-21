"""Per-entrypoint handler hooks answer identically over HTTP and SDR.

Both surfaces resolve ``test_auth`` / ``fetch_metadata`` through
``application_sdk.handler._dispatch.resolve_handler_op``. Before that, the SDR
activities called the app-level ``Handler`` directly, so an
``app.<segment>.handler`` hook ran over HTTP and was silently skipped over SDR.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application_sdk.errors.leaves import InvalidInputError
from application_sdk.execution._temporal.sdr import (
    SDR_FETCH_METADATA_ACTIVITY,
    SDR_TEST_AUTH_ACTIVITY,
    build_sdr_activities,
)
from application_sdk.handler._dispatch import mirror_template_key, resolve_handler_op
from application_sdk.handler.base import Handler
from application_sdk.handler.context import HandlerContext
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    HandlerCredential,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)
from application_sdk.handler.routes import register_handler_routes

_APP_NAME = "dispatch-app"
_ENTRYPOINT = "asset-export"
_CREDENTIALS = [
    {"key": "username", "value": "u"},
    {"key": "password", "value": "p"},
]


@dataclass(frozen=True)
class _Call:
    """What a hook saw — the fields that must match across surfaces."""

    op: str
    input_type: str
    entrypoint: str
    credentials: tuple[tuple[str, str], ...]
    ctx_app_name: str
    ctx_credentials: tuple[tuple[str, str], ...]
    object_filter: str | None


def _creds(items: list[HandlerCredential]) -> tuple[tuple[str, str], ...]:
    return tuple((c.key, c.value) for c in items)


class _AppLevelHandler(Handler):
    """App-level handler: must not run when a per-entrypoint hook exists."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise AssertionError("app-level test_auth ran instead of the hook")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        raise AssertionError("app-level fetch_metadata ran instead of the hook")


@pytest.fixture
def hook_calls(monkeypatch: pytest.MonkeyPatch) -> list[_Call]:
    """Install ``app.asset_export.handler`` with recording hooks."""
    calls: list[_Call] = []

    def _record(op: str, input: AuthInput | MetadataInput, ctx: HandlerContext) -> None:
        calls.append(
            _Call(
                op=op,
                input_type=type(input).__name__,
                entrypoint=input.entrypoint,
                credentials=_creds(input.credentials),
                ctx_app_name=ctx.app_name,
                ctx_credentials=_creds(ctx.credentials),
                object_filter=getattr(input, "object_filter", None),
            )
        )

    async def test_auth(input: AuthInput, ctx: HandlerContext) -> AuthOutput:
        _record("test_auth", input, ctx)
        return AuthOutput(status=AuthStatus.SUCCESS, message="hook")

    async def fetch_metadata(
        input: MetadataInput, ctx: HandlerContext
    ) -> MetadataOutput:
        _record("fetch_metadata", input, ctx)
        return SqlMetadataOutput(objects=[])

    for parent in ("app", "app.asset_export"):
        if parent not in sys.modules:
            pkg = types.ModuleType(parent)
            pkg.__path__ = []  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, parent, pkg)
    module = types.ModuleType("app.asset_export.handler")
    module.test_auth = test_auth  # type: ignore[attr-defined]
    module.fetch_metadata = fetch_metadata  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.asset_export.handler", module)
    return calls


def _over_http(op: str) -> None:
    app = FastAPI()
    register_handler_routes(app, _AppLevelHandler(), app_name=_APP_NAME)
    path = {
        "test_auth": "/workflows/v1/auth",
        "fetch_metadata": "/workflows/v1/metadata",
    }[op]
    body: dict[str, Any] = {"credentials": _CREDENTIALS, "entrypoint": _ENTRYPOINT}
    if op == "fetch_metadata":
        body["metadataTemplateKey"] = "tags"
    resp = TestClient(app).post(path, json=body)
    assert resp.status_code == 200, resp.text


async def _over_sdr(op: str) -> None:
    activities = build_sdr_activities(_AppLevelHandler(), app_name=_APP_NAME)
    by_name = {getattr(a, "__temporal_activity_definition").name: a for a in activities}
    credentials = [HandlerCredential(**c) for c in _CREDENTIALS]
    if op == "test_auth":
        await by_name[SDR_TEST_AUTH_ACTIVITY](
            AuthInput(credentials=credentials, entrypoint=_ENTRYPOINT)
        )
    else:
        await by_name[SDR_FETCH_METADATA_ACTIVITY](
            MetadataInput.model_validate(
                {
                    "credentials": _CREDENTIALS,
                    "entrypoint": _ENTRYPOINT,
                    "metadataTemplateKey": "tags",
                }
            )
        )


@pytest.mark.parametrize("op", ["test_auth", "fetch_metadata"])
async def test_per_entrypoint_hook_fires_identically_over_http_and_sdr(
    op: str, hook_calls: list[_Call]
) -> None:
    _over_http(op)
    await _over_sdr(op)

    assert len(hook_calls) == 2, hook_calls
    http_call, sdr_call = hook_calls
    assert http_call.op == op
    assert http_call == sdr_call
    assert http_call.entrypoint == _ENTRYPOINT
    assert http_call.ctx_app_name == _APP_NAME
    assert http_call.ctx_credentials == (("username", "u"), ("password", "p"))
    if op == "fetch_metadata":
        # The widget key is mirrored onto the legacy field on both surfaces.
        assert http_call.object_filter == "tags"


class TestResolveHandlerOp:
    async def test_empty_entrypoint_answers_with_the_handler(
        self, hook_calls: list[_Call]
    ) -> None:
        class _Ok(_AppLevelHandler):
            async def test_auth(self, input: AuthInput) -> AuthOutput:
                return AuthOutput(status=AuthStatus.SUCCESS, message="handler")

        run = resolve_handler_op(_Ok(), "test_auth", "")
        result = await run(AuthInput(), _ctx())
        assert result.message == "handler"
        assert hook_calls == []

    async def test_entrypoint_without_a_hook_module_answers_with_the_handler(
        self,
    ) -> None:
        class _Ok(_AppLevelHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                return SqlMetadataOutput(objects=[])

        run = resolve_handler_op(_Ok(), "fetch_metadata", "no-such-entrypoint")
        result = await run(MetadataInput(), _ctx())
        assert result.objects == []

    def test_malformed_entrypoint_is_refused(self) -> None:
        with pytest.raises(InvalidInputError):
            resolve_handler_op(_AppLevelHandler(), "test_auth", "../evil")

    async def test_sdr_refuses_a_malformed_entrypoint(self) -> None:
        activities = build_sdr_activities(_AppLevelHandler(), app_name=_APP_NAME)
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        with pytest.raises(InvalidInputError):
            await by_name[SDR_TEST_AUTH_ACTIVITY](AuthInput(entrypoint="../evil"))


class TestMirrorTemplateKey:
    def test_copies_key_onto_empty_object_filter_without_mutating(self) -> None:
        original = MetadataInput.model_validate({"metadataTemplateKey": "tags"})
        mirrored = mirror_template_key(original)
        assert mirrored.object_filter == "tags"
        assert original.object_filter != "tags"

    def test_keeps_an_explicit_object_filter(self) -> None:
        original = MetadataInput.model_validate(
            {"metadataTemplateKey": "tags", "object_filter": "connectors"}
        )
        assert mirror_template_key(original) is original


def _ctx() -> HandlerContext:
    from datetime import UTC, datetime
    from uuid import uuid4

    return HandlerContext(
        app_name=_APP_NAME, request_id=uuid4(), started_at=datetime.now(UTC)
    )
