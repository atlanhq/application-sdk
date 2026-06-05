"""Unit tests for the handler FastAPI service."""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from application_sdk.contracts.base import Input, Output
from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.contracts import (
    ApiMetadataObject,
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    EventFilterRule,
    EventTriggerConfig,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
    SubscriptionConfig,
)
from application_sdk.handler.service import (
    _flatten_to_pairs,
    _normalize_credentials,
    _wrap_response,
    create_app_handler_service,
)

# ---------------------------------------------------------------------------
# Test Handler implementation
# ---------------------------------------------------------------------------


class _TestHandler(Handler):
    """Minimal handler for testing — returns SqlMetadataOutput."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _ApiTreeHandler(Handler):
    """Handler that returns ApiMetadataOutput (BI connector path)."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return ApiMetadataOutput(
            objects=[
                ApiMetadataObject(value="tag-1", title="Tag One", node_type="tag"),
                ApiMetadataObject(value="tag-2", title="Tag Two", node_type="tag"),
            ]
        )


class _AuthFailedHandler(Handler):
    """Handler that returns AuthStatus.FAILED (no exception)."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.FAILED, message="bad credentials")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _AuthExpiredHandler(Handler):
    """Handler that returns AuthStatus.EXPIRED."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.EXPIRED, message="token expired")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _AuthInvalidCredsHandler(Handler):
    """Handler that returns AuthStatus.INVALID_CREDENTIALS."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.INVALID_CREDENTIALS, message="wrong key")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _AuthUnhandledExceptionHandler(Handler):
    """Handler whose test_auth raises an unexpected exception."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise RuntimeError("unexpected db connection error")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _FailingHandler(Handler):
    """Handler that raises HandlerError."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise HandlerError("auth failed")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        raise HandlerError("preflight failed")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        raise HandlerError("metadata failed")


# ---------------------------------------------------------------------------
# Module-level contract types for routing tests
# (locally-defined dataclasses fail get_type_hints() when
#  'from __future__ import annotations' is active)
# ---------------------------------------------------------------------------


# Plain Pydantic subclasses — @dataclass would generate a conflicting __init__
# that breaks Pydantic's __pydantic_fields_set__ initialisation.
class _RoutingInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    name: str = ""


class _RoutingOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    result: str = ""


# Contracts for TestGetResultMultiEntrypointOutputType — must be at module
# level because get_type_hints() resolves annotation strings against module
# globals when 'from __future__ import annotations' is active.
class _AlphaInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    pass


class _AlphaOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    alpha_field: str = ""


class _BetaInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    pass


class _BetaOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    beta_field: str = ""


# ---------------------------------------------------------------------------
# Helper: create test client
# ---------------------------------------------------------------------------


def _make_client(handler: Handler | None = None) -> TestClient:
    handler = handler or _TestHandler()
    app = create_app_handler_service(handler, app_name="test-app")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthEndpoints:
    """Tests for /health and /server/ready."""

    def test_health_endpoint_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_server_health_alias_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/server/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_ready_endpoint_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/server/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_ready_alias_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAuthEndpoint:
    """Tests for POST /workflows/v1/auth.

    Covers every AuthStatus, envelope format, credential passthrough,
    HandlerError, and unhandled exceptions — ensures the contract between
    the SDK and frontend never silently breaks again.
    """

    # -- Success ----------------------------------------------------------

    def test_auth_success_returns_200(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": [], "connection_id": "test-conn"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["status"] == "success"
        assert body["message"] == "auth ok"

    def test_auth_success_envelope_has_all_fields(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        body = response.json()
        assert "success" in body
        assert "message" in body
        assert "data" in body
        # data must contain AuthOutput fields
        data = body["data"]
        assert "status" in data
        assert "identities" in data
        assert "scopes" in data

    def test_auth_with_credentials_succeeds(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "credentials": [{"key": "api_key", "value": "secret123"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["success"] is True

    # -- Failure (every non-SUCCESS AuthStatus) ---------------------------

    def test_auth_failed_returns_401(self) -> None:
        client = _make_client(handler=_AuthFailedHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["data"]["status"] == "failed"
        assert body["message"] == "bad credentials"

    def test_auth_expired_returns_401(self) -> None:
        client = _make_client(handler=_AuthExpiredHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["data"]["status"] == "expired"
        assert body["message"] == "token expired"

    def test_auth_invalid_credentials_returns_401(self) -> None:
        client = _make_client(handler=_AuthInvalidCredsHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["data"]["status"] == "invalid_credentials"
        assert body["message"] == "wrong key"

    # -- Failure envelope still has full structure ------------------------

    def test_auth_failure_envelope_matches_success_shape(self) -> None:
        """A failure response must have the same top-level keys as success
        so that consumers can parse it uniformly."""
        client = _make_client(handler=_AuthFailedHandler())
        body = client.post("/workflows/v1/auth", json={"credentials": []}).json()
        assert set(body.keys()) == {"success", "message", "data"}
        assert set(body["data"].keys()) >= {"status", "message", "identities", "scopes"}

    # -- Exception handling -----------------------------------------------

    def test_auth_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 500
        assert "detail" in response.json()

    def test_auth_unhandled_exception_returns_500(self) -> None:
        """Unexpected exceptions (not HandlerError) must return 500 with a
        generic message — no stack traces leaked to the client."""
        client = _make_client(handler=_AuthUnhandledExceptionHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 500
        body = response.json()
        assert body["detail"] == "Internal server error"
        assert "RuntimeError" not in str(body)

    # -- AuthStatus.http_status / is_success properties -------------------

    def test_auth_status_http_codes(self) -> None:
        assert AuthStatus.SUCCESS.http_status == 200
        assert AuthStatus.FAILED.http_status == 401
        assert AuthStatus.EXPIRED.http_status == 401
        assert AuthStatus.INVALID_CREDENTIALS.http_status == 401

    def test_auth_status_is_success(self) -> None:
        assert AuthStatus.SUCCESS.is_success is True
        assert AuthStatus.FAILED.is_success is False
        assert AuthStatus.EXPIRED.is_success is False
        assert AuthStatus.INVALID_CREDENTIALS.is_success is False

    def test_every_auth_status_has_http_code(self) -> None:
        """If someone adds a new AuthStatus without updating the map, this
        test catches it."""
        for status in AuthStatus:
            assert isinstance(
                status.http_status, int
            ), f"{status} missing from _AUTH_STATUS_HTTP_CODES"


class TestPreflightEndpoint:
    """Tests for POST /workflows/v1/check."""

    def test_preflight_success(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        # _TestHandler returns no checks, so envelope success is false
        # (envelope success now means "preflight produced checks" — a handler
        # that returns zero checks is treated as a preflight-system failure
        # so the widget falls back to the top-level "Check failed" path).
        assert body["success"] is False
        # v2 format: data is a dict of check results keyed by camelCase name.
        assert body["data"] == {}
        assert body["message"] == "ready"

    def test_preflight_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )
        assert response.status_code == 500

    def test_preflight_metadata_forwarded_to_handler(self) -> None:
        """metadata survives _normalize_credentials and reaches the handler.

        Uses Format 2 credentials (nested dict) — the format heracles sends for
        native-app preflight calls — to verify that {**body, "credentials": ...}
        preserves the metadata key through normalization.
        """
        received: list[dict] = []

        class _MetadataCapture(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                received.append(dict(input.metadata))
                return PreflightOutput(status=PreflightStatus.READY, message="ready")

        client = _make_client(handler=_MetadataCapture())
        response = client.post(
            "/workflows/v1/check",
            # Format 2: credentials as a nested dict, which is what heracles sends
            # for native-app preflight calls. _normalize_credentials converts this
            # to v3 list via {**body, "credentials": ...}, metadata must survive.
            json={
                "credentials": {"connectorConfigName": "atlan-connectors-dbt"},
                "metadata": {
                    "extraction-type": "objectstore",
                    "manifest-source": "atlan",
                    "core-extract-output-prefix": "artifacts/dbt/prod",
                },
            },
        )
        assert response.status_code == 200
        assert received == [
            {
                "extraction-type": "objectstore",
                "manifest-source": "atlan",
                "core-extract-output-prefix": "artifacts/dbt/prod",
            }
        ]

    def test_preflight_metadata_absent_defaults_to_empty(self) -> None:
        """Callers that don't send metadata get an empty dict — no regression."""
        received: list[dict] = []

        class _MetadataCapture(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                received.append(dict(input.metadata))
                return PreflightOutput(status=PreflightStatus.READY, message="ready")

        client = _make_client(handler=_MetadataCapture())
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )
        assert response.status_code == 200
        assert received == [{}]

    # ------------------------------------------------------------------
    # Per-check v2-shape fields (DBBI-665 / WARE-1250 / finishes BLDX-901)
    # ------------------------------------------------------------------
    #
    # The SageV2 widget at
    # atlan-frontend/src/workflowsv2/components/dynamicForm2/widget/SageV2.vue:271-273
    # renders ``checkResult.success ? successMessage : failureMessage`` with
    # no fallback to ``message``. PR #1228 (BLDX-901) finished two of the
    # three sub-mismatches but dropped this rename; these tests pin the
    # finished shape so it does not regress again.

    def test_preflight_check_emits_v2_success_fields(self) -> None:
        """A passing check carries the message in successMessage; failureMessage is empty."""

        class _OneCheck(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.READY,
                    checks=[
                        PreflightCheck(
                            name="apiVersion",
                            passed=True,
                            message="API version 3.17 is supported",
                        )
                    ],
                )

        client = _make_client(handler=_OneCheck())
        body = client.post("/workflows/v1/check", json={"credentials": []}).json()
        entry = body["data"]["apiVersion"]
        assert entry["success"] is True
        assert entry["message"] == "API version 3.17 is supported"
        assert entry["successMessage"] == "API version 3.17 is supported"
        assert entry["failureMessage"] == ""

    def test_preflight_check_emits_v2_failure_fields(self) -> None:
        """A failing check carries the message in failureMessage; successMessage is empty.

        This is the DBBI-665 / IEEE case — without ``failureMessage`` the
        SageV2 widget renders an empty "Hide details" panel even though the
        handler set a real message on the check.
        """

        class _OneCheck(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    checks=[
                        PreflightCheck(
                            name="metadataAPI",
                            passed=False,
                            message="Metadata GraphQL API returned no sites",
                        )
                    ],
                )

        client = _make_client(handler=_OneCheck())
        body = client.post("/workflows/v1/check", json={"credentials": []}).json()
        entry = body["data"]["metadataAPI"]
        assert entry["success"] is False
        assert entry["message"] == "Metadata GraphQL API returned no sites"
        assert entry["failureMessage"] == "Metadata GraphQL API returned no sites"
        assert entry["successMessage"] == ""

    def test_preflight_check_multiple_checks_v2_fields_per_check(self) -> None:
        """Mixed pass/fail set — each check entry carries its own v2 fields."""

        class _ThreeChecks(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.PARTIAL,
                    checks=[
                        PreflightCheck(
                            name="apiVersion",
                            passed=True,
                            message="API version 3.17 is supported",
                        ),
                        PreflightCheck(
                            name="viewCapability",
                            passed=True,
                            message="Projects accessible — 5 project(s) found",
                        ),
                        PreflightCheck(
                            name="metadataAPI",
                            passed=False,
                            message="Metadata GraphQL API returned 404",
                        ),
                    ],
                )

        client = _make_client(handler=_ThreeChecks())
        data = client.post("/workflows/v1/check", json={"credentials": []}).json()[
            "data"
        ]

        assert data["apiVersion"]["successMessage"].startswith("API version")
        assert data["apiVersion"]["failureMessage"] == ""
        assert data["viewCapability"]["successMessage"].startswith("Projects")
        assert data["viewCapability"]["failureMessage"] == ""
        assert data["metadataAPI"]["failureMessage"] == (
            "Metadata GraphQL API returned 404"
        )
        assert data["metadataAPI"]["successMessage"] == ""
        # ``message`` is preserved on every entry for any v3-shape consumer.
        for name in ("apiVersion", "viewCapability", "metadataAPI"):
            assert data[name]["message"]

    def test_preflight_check_empty_message_yields_empty_v2_fields(self) -> None:
        """A check with no message yields empty strings in all message fields."""

        class _OneCheck(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    checks=[PreflightCheck(name="connectivity", passed=False)],
                )

        client = _make_client(handler=_OneCheck())
        entry = client.post("/workflows/v1/check", json={"credentials": []}).json()[
            "data"
        ]["connectivity"]
        assert entry["success"] is False
        assert entry["message"] == ""
        assert entry["successMessage"] == ""
        assert entry["failureMessage"] == ""

    # ------------------------------------------------------------------
    # Envelope ``success`` reports preflight execution, not per-check pass
    # (DBBI-665 root cause).
    # ------------------------------------------------------------------
    #
    # The previous behaviour ``success = (status == READY)`` made every
    # PARTIAL/NOT_READY response surface as a blank "Check failed" panel
    # because SageV2.vue:249 short-circuits on ``!response.success`` and
    # never iterates the per-check data. These tests pin the new contract:
    # envelope success is true as long as preflight produced any checks.

    def test_preflight_envelope_success_true_when_all_checks_pass(self) -> None:
        """Status READY → envelope success true (regression guard)."""

        class _OneCheck(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.READY,
                    checks=[
                        PreflightCheck(name="apiVersion", passed=True, message="ok")
                    ],
                )

        body = (
            _make_client(handler=_OneCheck())
            .post("/workflows/v1/check", json={"credentials": []})
            .json()
        )
        assert body["success"] is True

    def test_preflight_envelope_success_true_when_a_check_fails(self) -> None:
        """Status NOT_READY → envelope success TRUE so SageV2 still renders per-check rows.

        This is the DBBI-665 case: previously a failed check forced envelope
        success=false, which made the widget skip the per-check loop entirely
        and show a blank "Hide details" panel. Pinning envelope success=true
        whenever checks ran lets the per-check rows render.
        """

        class _OneCheck(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    checks=[
                        PreflightCheck(
                            name="apiVersion",
                            passed=False,
                            message="Could not connect to Tableau",
                        )
                    ],
                )

        body = (
            _make_client(handler=_OneCheck())
            .post("/workflows/v1/check", json={"credentials": []})
            .json()
        )
        assert body["success"] is True
        # Per-check detail is still on the wire so the widget can render it.
        assert body["data"]["apiVersion"]["success"] is False
        assert body["data"]["apiVersion"]["failureMessage"] == (
            "Could not connect to Tableau"
        )

    def test_preflight_envelope_success_true_on_partial_status(self) -> None:
        """Status PARTIAL → envelope success TRUE.

        Mixed pass/fail must surface to the UI; a single failing check
        cannot collapse the entire response.
        """

        class _Mixed(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.PARTIAL,
                    checks=[
                        PreflightCheck(name="apiVersion", passed=True, message="ok"),
                        PreflightCheck(
                            name="metadataAPI",
                            passed=False,
                            message="GraphQL 404",
                        ),
                    ],
                )

        body = (
            _make_client(handler=_Mixed())
            .post("/workflows/v1/check", json={"credentials": []})
            .json()
        )
        assert body["success"] is True
        assert body["data"]["apiVersion"]["success"] is True
        assert body["data"]["metadataAPI"]["failureMessage"] == "GraphQL 404"

    def test_preflight_envelope_success_false_when_no_checks_run(self) -> None:
        """No checks emitted → envelope success FALSE — preflight-system failure.

        A handler that returns zero checks signals that preflight itself
        couldn't execute (e.g. the client failed to construct). The widget
        falls back to the top-level "Check failed" branch — same UX as a
        thrown exception, just without the 500.
        """

        class _ZeroChecks(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                return PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    checks=[],
                    message="couldn't build client",
                )

        body = (
            _make_client(handler=_ZeroChecks())
            .post("/workflows/v1/check", json={"credentials": []})
            .json()
        )
        assert body["success"] is False
        assert body["data"] == {}


class TestMetadataEndpoint:
    """Tests for POST /workflows/v1/metadata."""

    def test_metadata_sql_empty_returns_empty_list(self) -> None:
        """SqlMetadataOutput with no objects → empty list in data."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == []

    def test_metadata_sql_returns_flat_rows(self) -> None:
        """SqlMetadataOutput → [{TABLE_CATALOG, TABLE_SCHEMA}] for sqltree widget."""

        class _SQLHandler(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                return SqlMetadataOutput(
                    objects=[
                        SqlMetadataObject(
                            TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="FINANCE"
                        ),
                        SqlMetadataObject(
                            TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="SALES"
                        ),
                    ]
                )

        client = _make_client(handler=_SQLHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == [
            {"TABLE_CATALOG": "DEFAULT", "TABLE_SCHEMA": "FINANCE"},
            {"TABLE_CATALOG": "DEFAULT", "TABLE_SCHEMA": "SALES"},
        ]
        assert (
            "message" not in body
        )  # omitted on success — fixes frontend filter dropdowns

    def test_metadata_api_returns_tree_nodes(self) -> None:
        """ApiMetadataOutput → [{value, title, node_type, children}] for apitree widget."""
        client = _make_client(handler=_ApiTreeHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == [
            {"value": "tag-1", "title": "Tag One", "node_type": "tag", "children": []},
            {"value": "tag-2", "title": "Tag Two", "node_type": "tag", "children": []},
        ]
        assert (
            "message" not in body
        )  # omitted on success — fixes frontend filter dropdowns

    def test_metadata_api_nested_children(self) -> None:
        """ApiMetadataOutput with nested children serializes the full tree."""

        class _NestedHandler(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                return ApiMetadataOutput(
                    objects=[
                        ApiMetadataObject(
                            value="proj-1",
                            title="Project A",
                            node_type="project",
                            children=[
                                ApiMetadataObject(
                                    value="ws-1",
                                    title="Workspace 1",
                                    node_type="workspace",
                                ),
                                ApiMetadataObject(
                                    value="ws-2",
                                    title="Workspace 2",
                                    node_type="workspace",
                                ),
                            ],
                        ),
                    ]
                )

        client = _make_client(handler=_NestedHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["value"] == "proj-1"
        assert len(data[0]["children"]) == 2
        assert data[0]["children"][0]["value"] == "ws-1"
        assert data[0]["children"][1]["node_type"] == "workspace"

    def test_metadata_template_key_forwarded_to_handler(self) -> None:
        received: list[str] = []

        class _MetadataTemplateCapture(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                received.append(input.metadata_template_key)
                return ApiMetadataOutput(objects=[])

        client = _make_client(handler=_MetadataTemplateCapture())
        response = client.post(
            "/workflows/v1/metadata",
            json={
                "credentials": {"host": "db.example.com"},
                "metadata_template_key": "feedbacks",
            },
        )

        assert response.status_code == 200
        assert received == ["feedbacks"]

    def test_metadata_non_canonical_keys_do_not_populate_template_key(self) -> None:
        received: list[str] = []

        class _MetadataTemplateCapture(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                received.append(input.metadata_template_key)
                return ApiMetadataOutput(objects=[])

        client = _make_client(handler=_MetadataTemplateCapture())
        for key in ("metadataTemplateKey", "type"):
            response = client.post(
                "/workflows/v1/metadata",
                json={
                    "credentials": {"host": "db.example.com"},
                    key: "feedbacks",
                },
            )

            assert response.status_code == 200

        assert received == ["", ""]

    def test_metadata_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 500


class TestStartWorkflowEndpoint:
    """Tests for POST /workflows/v1/start."""

    def test_start_workflow_without_temporal_config_returns_503(self) -> None:
        # Service created without app_class → not configured
        client = _make_client()
        response = client.post(
            "/workflows/v1/start",
            json={"name": "test"},
        )
        assert response.status_code == 503

    def test_start_workflow_with_app_class_but_no_temporal_host_returns_503(
        self,
    ) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry
        from application_sdk.contracts.base import Input, Output

        AppRegistry.reset()
        TaskRegistry.reset()

        try:

            @dataclass
            class _StartInput(Input, allow_unbounded_fields=True):
                name: str = ""

            @dataclass
            class _StartOutput(Output, allow_unbounded_fields=True):
                result: str = ""

            class _StartApp(App):
                async def run(self, input: _StartInput) -> _StartOutput:
                    return _StartOutput()

            # No temporal_host → is_configured() returns False
            handler = _TestHandler()
            app = create_app_handler_service(
                handler,
                app_name="start-test",
                app_class=_StartApp,
                temporal_host="",  # empty → not configured
            )
            client = TestClient(app)
            response = client.post("/workflows/v1/start", json={"name": "world"})
            assert response.status_code == 503
        finally:
            AppRegistry.reset()
            TaskRegistry.reset()


class TestErrorInfoDisclosure:
    """Regression: error responses must not leak internal exception details.

    The security fix removed exception interpolation from 4 HTTP error
    handlers. These tests verify the generic messages are returned.
    """

    def test_start_workflow_type_error_returns_generic_message(self) -> None:
        """TypeError handler on /start must not leak internal type info."""
        # Send invalid JSON that triggers a TypeError in workflow start
        client = _make_client()
        response = client.post(
            "/workflows/v1/start",
            json={"deliberately": "wrong_shape"},
        )
        # May return 400 (TypeError) or 503 (not configured) depending on setup
        # Either way, response body must not contain Python exception details
        body_str = str(response.json())
        assert "Traceback" not in body_str
        assert "TypeError" not in body_str

    def test_handler_error_500_does_not_leak_exception(self) -> None:
        """Handler endpoint errors should return generic messages."""
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 500
        body_str = str(response.json())
        assert "Traceback" not in body_str
        assert "File " not in body_str


class TestStartWorkflowRouting:
    """Tests for /workflows/v1/start entry-point routing logic."""

    def setup_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def _make_routed_client(self, app_cls: type, *, patch_temporal: bool = True):  # type: ignore[return]
        """Create a TestClient wired to app_cls with Temporal mocked out."""
        from unittest.mock import AsyncMock, MagicMock, patch

        handler = _TestHandler()
        svc = create_app_handler_service(
            handler,
            app_name="routing-test",
            app_class=app_cls,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-123"
        mock_handle.result_run_id = "run-abc"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        client = TestClient(svc, raise_server_exceptions=False)
        return client, patcher

    def test_single_ep_no_workflow_type_auto_selects(self) -> None:
        """Single-entry-point app accepts request with no workflow_type."""
        # Use module-level types — locally-defined dataclasses can't be resolved
        # by get_type_hints() because 'from __future__ import annotations' is active.
        from application_sdk.app.base import App

        class _SingleEpApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_SingleEpApp)
        try:
            response = client.post("/workflows/v1/start", json={"name": "hello"})
            assert response.status_code == 200
        finally:
            patcher.stop()

    def test_multi_ep_entrypoint_query_param_routes_correctly(self) -> None:
        """Multi-entry-point app with ?entrypoint= query param returns 200."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_MultiEpApp)
        try:
            response = client.post(
                "/workflows/v1/start?entrypoint=extract",
                json={"name": "x"},
            )
            assert response.status_code == 200
        finally:
            patcher.stop()

    def test_multi_ep_body_workflow_type_fallback_routes_correctly(self) -> None:
        """Multi-entry-point app falls back to body 'workflow_type' when ?entrypoint= absent."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpFallbackApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_MultiEpFallbackApp)
        try:
            with pytest.warns(DeprecationWarning, match="workflow_type.*deprecated"):
                response = client.post(
                    "/workflows/v1/start",
                    json={"workflow_type": "extract", "name": "x"},
                )
            assert response.status_code == 200
        finally:
            patcher.stop()

    def test_query_param_takes_precedence_over_body_workflow_type(self) -> None:
        """?entrypoint= wins over body 'workflow_type' when both are provided."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _PrecedenceApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        handler = _TestHandler()
        svc = create_app_handler_service(
            handler,
            app_name="precedence-test",
            app_class=_PrecedenceApp,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-prec"
        mock_handle.result_run_id = "run-prec"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        tc = TestClient(svc, raise_server_exceptions=False)
        try:
            # ?entrypoint=extract wins over body workflow_type=load;
            # the canonical query-param path must NOT emit a DeprecationWarning.
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                response = tc.post(
                    "/workflows/v1/start?entrypoint=extract",
                    json={"workflow_type": "load", "name": "x"},
                )
            assert response.status_code == 200
            assert mock_client.start_workflow.call_count == 1
            # The started workflow name must end in ':extract', not ':load'
            started_name = mock_client.start_workflow.call_args[0][0]
            assert ":load" not in started_name, f"load was dispatched: {started_name!r}"
            assert started_name.endswith(
                ":extract"
            ), f"Expected :extract, got {started_name!r}"
            assert not any(
                issubclass(w.category, DeprecationWarning) for w in caught
            ), "Canonical ?entrypoint= path must not emit DeprecationWarning"
        finally:
            patcher.stop()

    def test_unknown_entrypoint_query_param_returns_400(self) -> None:
        """Providing an unrecognised ?entrypoint= returns 400 without leaking names."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _TwoEpApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_TwoEpApp)
        try:
            response = client.post(
                "/workflows/v1/start?entrypoint=does-not-exist",
                json={"name": "x"},
            )
            assert response.status_code == 400
            detail = response.json().get("detail", "")
            assert "extract" not in detail
            assert "load" not in detail
        finally:
            patcher.stop()

    def test_multi_ep_no_entrypoint_dispatches_auto_default(self) -> None:
        """Multi-entry-point app with no ?entrypoint= dispatches to the auto-default.

        When multiple @entrypoints exist with no explicit default, the first
        alphabetically (via dir()) is auto-marked default at registration.
        Omitting ?entrypoint= therefore returns 200 and dispatches that ep.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _AnotherMultiEpApp(App):
            @entrypoint
            async def step_a(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def step_b(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_AnotherMultiEpApp)
        try:
            response = client.post("/workflows/v1/start", json={"name": "x"})
            assert response.status_code == 200
        finally:
            patcher.stop()


class TestStartWorkflowInvocability:
    """Tests that every entry-point combination is reachable via /workflows/v1/start.

    Covers:
      - run()-only: no ?entrypoint= AND ?entrypoint=run
      - @entrypoint-only (single): no ?entrypoint= AND ?entrypoint={name}
      - @entrypoints (multiple): ?entrypoint= for each AND no ?entrypoint= uses auto-default
      - mixed run() + @entrypoint: default (run, no ?entrypoint=), explicit by name
      - mixed run() + @entrypoints (multiple): same rules
    """

    def setup_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def _make_routed_client(self, app_cls: type):  # type: ignore[return]
        from unittest.mock import AsyncMock, MagicMock, patch

        handler = _TestHandler()
        svc = create_app_handler_service(
            handler,
            app_name="inv-test",
            app_class=app_cls,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-1"
        mock_handle.result_run_id = "run-1"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        client = TestClient(svc, raise_server_exceptions=False)
        return client, patcher, mock_client

    def _started_workflow_name(self, mock_client: object) -> str:
        from unittest.mock import MagicMock

        assert isinstance(mock_client, MagicMock)
        return mock_client.start_workflow.call_args[0][0]

    # ── run()-only ────────────────────────────────────────────────────────

    def test_run_only_no_param_is_200(self) -> None:
        """run()-only app: omitting ?entrypoint= dispatches successfully."""
        from application_sdk.app.base import App

        class _RunOnlyInvApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_RunOnlyInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            # Implicit run() uses bare app-name workflow type (no colon suffix)
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    def test_run_only_explicit_run_param_is_200(self) -> None:
        """run()-only app: ?entrypoint=run dispatches to the same implicit entry point."""
        from application_sdk.app.base import App

        class _RunOnlyExplicitApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_RunOnlyExplicitApp)
        try:
            resp = client.post("/workflows/v1/start?entrypoint=run", json={"name": "x"})
            assert resp.status_code == 200
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    # ── @entrypoint-only (single) ─────────────────────────────────────────

    def test_single_ep_no_param_is_200(self) -> None:
        """Single @entrypoint app: omitting ?entrypoint= dispatches via len==1 default."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _SingleEpInvApp(App):
            @entrypoint
            async def ingest(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_SingleEpInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":ingest")
        finally:
            patcher.stop()

    def test_single_ep_explicit_param_is_200(self) -> None:
        """Single @entrypoint app: ?entrypoint={name} dispatches correctly."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _SingleEpExplicitApp(App):
            @entrypoint
            async def ingest(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_SingleEpExplicitApp)
        try:
            resp = client.post(
                "/workflows/v1/start?entrypoint=ingest", json={"name": "x"}
            )
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":ingest")
        finally:
            patcher.stop()

    # ── @entrypoints (multiple) ───────────────────────────────────────────

    def test_multi_ep_no_param_dispatches_auto_default(self) -> None:
        """Multiple @entrypoints, none explicit default: ?entrypoint= omitted uses auto-default.

        Auto-default is the first alphabetically: alpha-ep precedes beta-ep.
        """
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpInvApp(App):
            @entrypoint
            async def alpha_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def beta_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MultiEpInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":alpha-ep")
        finally:
            patcher.stop()

    def test_multi_ep_explicit_alpha_param_is_200(self) -> None:
        """Multiple @entrypoints: ?entrypoint=alpha-ep dispatches to alpha-ep."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpAlphaApp(App):
            @entrypoint
            async def alpha_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def beta_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MultiEpAlphaApp)
        try:
            resp = client.post(
                "/workflows/v1/start?entrypoint=alpha-ep", json={"name": "x"}
            )
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":alpha-ep")
        finally:
            patcher.stop()

    def test_multi_ep_explicit_beta_param_is_200(self) -> None:
        """Multiple @entrypoints: ?entrypoint=beta-ep dispatches to beta-ep."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpBetaApp(App):
            @entrypoint
            async def alpha_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def beta_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MultiEpBetaApp)
        try:
            resp = client.post(
                "/workflows/v1/start?entrypoint=beta-ep", json={"name": "x"}
            )
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":beta-ep")
        finally:
            patcher.stop()

    def test_multi_ep_explicit_default_no_param_dispatches_marked(self) -> None:
        """Multiple @entrypoints with one explicit default: ?entrypoint= omitted uses marked."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpMarkedApp(App):
            @entrypoint
            async def alpha_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint(default=True)
            async def beta_ep(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MultiEpMarkedApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":beta-ep")
        finally:
            patcher.stop()

    # ── mixed run() + single @entrypoint ─────────────────────────────────

    def test_mixed_single_no_param_dispatches_run(self) -> None:
        """Mixed app: ?entrypoint= omitted dispatches to run() (permanent default)."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedSingleInvApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MixedSingleInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            # run() is implicit → bare app-name, no colon
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    def test_mixed_single_run_param_dispatches_run(self) -> None:
        """Mixed app: ?entrypoint=run dispatches to run()."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedRunParamApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MixedRunParamApp)
        try:
            resp = client.post("/workflows/v1/start?entrypoint=run", json={"name": "x"})
            assert resp.status_code == 200
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    def test_mixed_single_explicit_ep_param_dispatches_ep(self) -> None:
        """Mixed app: ?entrypoint={name} dispatches to the named @entrypoint."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedEpParamApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MixedEpParamApp)
        try:
            resp = client.post(
                "/workflows/v1/start?entrypoint=extract", json={"name": "x"}
            )
            assert resp.status_code == 200
            assert self._started_workflow_name(mock_client).endswith(":extract")
        finally:
            patcher.stop()

    # ── mixed run() + multiple @entrypoints ──────────────────────────────

    def test_mixed_multi_no_param_dispatches_run(self) -> None:
        """Mixed app with multiple @entrypoints: run() remains default when omitted."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedMultiInvApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def publish(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher, mock_client = self._make_routed_client(_MixedMultiInvApp)
        try:
            resp = client.post("/workflows/v1/start", json={"name": "x"})
            assert resp.status_code == 200
            assert ":" not in self._started_workflow_name(mock_client)
        finally:
            patcher.stop()

    def test_mixed_multi_explicit_ep_params_each_dispatch_correctly(self) -> None:
        """Mixed app with multiple @entrypoints: each is reachable by name."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MixedMultiEachApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def publish(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        for ep_name, ep_suffix in [
            ("run", None),
            ("extract", ":extract"),
            ("publish", ":publish"),
        ]:
            client, patcher, mock_client = self._make_routed_client(_MixedMultiEachApp)
            try:
                resp = client.post(
                    f"/workflows/v1/start?entrypoint={ep_name}", json={"name": "x"}
                )
                assert (
                    resp.status_code == 200
                ), f"?entrypoint={ep_name} returned {resp.status_code}"
                wf_name = self._started_workflow_name(mock_client)
                if ep_suffix is None:
                    assert (
                        ":" not in wf_name
                    ), f"?entrypoint={ep_name}: expected bare name (no colon), got {wf_name!r}"
                else:
                    assert wf_name.endswith(
                        ep_suffix
                    ), f"?entrypoint={ep_name}: expected suffix {ep_suffix!r}, got {wf_name!r}"
            finally:
                patcher.stop()


class TestWrapResponse:
    """Tests for _wrap_response helper."""

    def test_basic_structure(self) -> None:
        result = _wrap_response({"key": "value"})
        assert result["success"] is True
        assert "message" not in result  # omitted when empty (frontend dropdowns)
        assert result["data"] == {"key": "value"}

    def test_custom_message(self) -> None:
        result = _wrap_response({}, message="All good")
        assert result["message"] == "All good"

    def test_failure_response(self) -> None:
        result = _wrap_response({"error": "oops"}, success=False, message="failed")
        assert result["success"] is False
        assert result["message"] == "failed"

    def test_list_data(self) -> None:
        result = _wrap_response([{"a": 1}, {"b": 2}], message="ok")
        assert result["data"] == [{"a": 1}, {"b": 2}]
        assert result["message"] == "ok"


class TestRunIdPathParam:
    """Tests for stop and status endpoints using slash-containing run_ids."""

    def test_status_with_slash_run_id(self) -> None:
        # Without a configured Temporal client the service returns 503,
        # but the route itself must resolve (not 404/405).
        client = _make_client()
        response = client.get("/workflows/v1/status/my-workflow/some/slashed/run-id")
        # 503 = route resolved but Temporal not configured; proves :path matched
        assert response.status_code == 503

    def test_stop_with_slash_run_id(self) -> None:
        client = _make_client()
        response = client.post("/workflows/v1/stop/my-workflow/some/slashed/run-id")
        assert response.status_code == 503


class TestConfigMapEndpoints:
    """Tests for GET /workflows/v1/configmap/{id} and /configmaps."""

    def test_configmap_not_found_returns_404_and_logs_available_stems(
        self, tmp_path: Path
    ) -> None:
        from application_sdk.handler import service as svc_module

        (tmp_path / "sap-hana.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            with patch("application_sdk.handler.service.logger") as mock_logger:
                response = client.get("/workflows/v1/configmap/saphana")
            assert response.status_code == 404
            assert response.json()["detail"] == "ConfigMap 'saphana' not found"
            mock_logger.warning.assert_called_once_with(
                "ConfigMap not found: requested=%s available=%s",
                "saphana",
                ["sap-hana"],
            )
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_returns_wrapped_k8s_shape(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        raw = {"config": {"key": "value"}}
        (tmp_path / "my-config.json").write_text(json.dumps(raw))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmap/my-config")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["kind"] == "ConfigMap"
            assert data["apiVersion"] == "v1"
            assert data["metadata"]["name"] == "my-config"
            parsed_config = json.loads(data["data"]["config"])
            assert parsed_config == {"key": "value"}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_lists_stems_excluding_manifest(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        (tmp_path / "config-a.json").write_text("{}")
        (tmp_path / "config-b.json").write_text("{}")
        (tmp_path / "manifest.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            assert "config-a" in configmaps
            assert "config-b" in configmaps
            assert "manifest" not in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_empty_when_dir_missing(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        missing = tmp_path / "nonexistent"
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = missing
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            assert response.json()["data"]["configmaps"] == []
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


class TestWorkflowConfigValidation:
    """Tests for _CONFIG_KEY_RE validation on /workflows/v1/config/{config_id}."""

    @pytest.mark.parametrize(
        "config_id",
        [
            "../foo",
            "a/b",
            "a.b",
            "",
            "a" * 129,
            "%2e%2e",
            "a b",
        ],
    )
    def test_get_config_rejects_invalid_config_id(self, config_id: str) -> None:
        client = _make_client()
        response = client.get(f"/workflows/v1/config/{config_id}")
        assert response.status_code in {
            400,
            404,
            422,
        }, f"Expected rejection for {config_id!r}, got {response.status_code}"

    @pytest.mark.parametrize(
        "config_id",
        [
            "../foo",
            "a/b",
            "a.b",
            "",
            "a" * 129,
            "%2e%2e",
            "a b",
        ],
    )
    def test_post_config_rejects_invalid_config_id(self, config_id: str) -> None:
        client = _make_client()
        response = client.post(
            f"/workflows/v1/config/{config_id}",
            json={"key": "value"},
        )
        assert response.status_code in {
            400,
            404,
            422,
        }, f"Expected rejection for {config_id!r}, got {response.status_code}"

    @pytest.mark.parametrize(
        "type_param",
        ["../etc", "a/b", "a.b", "a" * 129],
    )
    def test_get_config_rejects_invalid_type_param(self, type_param: str) -> None:
        client = _make_client()
        response = client.get(
            "/workflows/v1/config/valid-id",
            params={"type": type_param},
        )
        assert (
            response.status_code == 422
        ), f"Expected 422 from FastAPI pattern validator for type={type_param!r}, got {response.status_code}"

    @pytest.mark.parametrize(
        "type_param",
        ["../etc", "a/b", "a.b", "a" * 129],
    )
    def test_post_config_rejects_invalid_type_param(self, type_param: str) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/config/valid-id",
            params={"type": type_param},
            json={"key": "value"},
        )
        assert (
            response.status_code == 422
        ), f"Expected 422 from FastAPI pattern validator for type={type_param!r}, got {response.status_code}"

    @pytest.mark.parametrize(
        "config_id",
        ["abc123", "ABC_123", "a-b", "a" * 128],
    )
    def test_get_config_accepts_valid_config_id(self, config_id: str) -> None:
        """Valid config_ids pass the regex check (result is 503/404 without a store, not 400)."""
        client = _make_client()
        response = client.get(f"/workflows/v1/config/{config_id}")
        assert (
            response.status_code != 400
        ), f"Valid config_id {config_id!r} was wrongly rejected"


class TestDaprSubscribeEndpoint:
    """Tests for GET /dapr/subscribe."""

    def test_subscribe_empty_when_no_triggers_or_subs(self) -> None:
        client = _make_client()
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        assert response.json() == []

    def test_subscribe_includes_event_trigger(self) -> None:
        handler = _TestHandler()
        trigger = EventTriggerConfig(
            event_id="my-trigger",
            event_type="metadata_extraction",
            event_name="extraction_requested",
            event_filters=[
                EventFilterRule(path="event.data.type", operator="==", value="meta")
            ],
        )
        app = create_app_handler_service(
            handler, app_name="test-app", event_triggers=[trigger]
        )
        client = TestClient(app)
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        subs = response.json()
        assert len(subs) == 1
        sub = subs[0]
        assert sub["topic"] == "metadata_extraction"
        routes = sub["routes"]
        assert routes["rules"][0]["path"] == "/events/v1/event/my-trigger"
        assert routes["default"] == "/events/v1/drop"

    def test_subscribe_includes_subscription_config(self) -> None:
        handler = _TestHandler()

        async def my_handler(request: object) -> dict:
            return {"status": "SUCCESS"}

        sub = SubscriptionConfig(
            component_name="my-pubsub",
            topic="my-topic",
            route="my-route",
            handler=my_handler,
        )
        app = create_app_handler_service(
            handler, app_name="test-app", subscriptions=[sub]
        )
        client = TestClient(app)
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        subs = response.json()
        assert len(subs) == 1
        assert subs[0]["pubsubname"] == "my-pubsub"
        assert subs[0]["topic"] == "my-topic"
        assert subs[0]["route"] == "/subscriptions/v1/my-route"


class TestDropEventEndpoint:
    """Tests for POST /events/v1/drop."""

    def test_drop_returns_drop_status(self) -> None:
        client = _make_client()
        response = client.post("/events/v1/drop")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "DROP"
        assert body["success"] is False


class TestDynamicSubscriptionRoutes:
    """Tests for dynamic /subscriptions/v1/{route} endpoints."""

    def test_subscription_route_is_registered(self) -> None:
        handler = _TestHandler()

        async def my_handler() -> dict:
            return {"status": "SUCCESS"}

        sub = SubscriptionConfig(
            component_name="pubsub",
            topic="topic",
            route="my-handler",
            handler=my_handler,
        )
        app = create_app_handler_service(
            handler, app_name="test-app", subscriptions=[sub]
        )
        client = TestClient(app)
        response = client.post("/subscriptions/v1/my-handler")
        assert response.status_code == 200
        assert response.json() == {"status": "SUCCESS"}


class TestManifestEndpoint:
    """Tests for GET /workflows/v1/manifest."""

    def _make_manifest(self):
        from application_sdk.handler.manifest import (
            AppManifest,
            DagNode,
            DagNodeDependency,
            ExecuteWorkflowInputs,
        )

        return AppManifest(
            execution_mode="dag",
            dag={
                "extract": DagNode(
                    activity_name="execute_workflow",
                    activity_display_name="Extract",
                    app_name="my-extractor",
                    inputs=ExecuteWorkflowInputs(
                        workflow_type="extraction",
                        task_queue="my-extractor-queue",
                        args={"connection": "{{connection}}"},
                    ),
                ),
                "load": DagNode(
                    activity_name="execute_workflow",
                    activity_display_name="Load",
                    app_name="my-loader",
                    inputs=ExecuteWorkflowInputs(
                        workflow_type="loading",
                        task_queue="my-loader-queue",
                    ),
                    depends_on=DagNodeDependency(node_id="extract"),
                ),
            },
            init_endpoint="/workflows/v1/init",
        )

    def test_manifest_returns_programmatic_manifest(self) -> None:
        manifest = self._make_manifest()
        app = create_app_handler_service(
            _TestHandler(), app_name="test-app", manifest=manifest
        )
        client = TestClient(app)
        response = client.get("/workflows/v1/manifest")
        assert response.status_code == 200
        body = response.json()
        assert body["execution_mode"] == "dag"
        assert "extract" in body["dag"]
        assert body["dag"]["extract"]["app_name"] == "my-extractor"
        assert body["dag"]["load"]["depends_on"]["node_id"] == "extract"
        assert body["init_endpoint"] == "/workflows/v1/init"

    def test_manifest_falls_back_to_disk(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Extract",
                    "app_name": "disk-app",
                    "inputs": {
                        "workflow_type": "extraction",
                        "task_queue": "disk-queue",
                    },
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest_data))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["execution_mode"] == "dag"
            assert body["dag"]["extract"]["app_name"] == "disk-app"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_disk_substitutes_deployment_name(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Extract",
                    "app_name": "my-app",
                    "inputs": {
                        "workflow_type": "extraction",
                        "task_queue": "{deployment_name}-queue",
                    },
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        original_dep = svc_module.DEPLOYMENT_NAME
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        svc_module.DEPLOYMENT_NAME = "prod-deploy"
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["dag"]["extract"]["inputs"]["task_queue"] == "prod-deploy-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_manifest_disk_substitutes_app_name(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Extract",
                    "app_name": "{app_name}",
                    "inputs": {
                        "workflow_type": "extraction",
                        "task_queue": "{deployment_name}-queue",
                    },
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        original_dep = svc_module.DEPLOYMENT_NAME
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        svc_module.DEPLOYMENT_NAME = "prod-deploy"
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["dag"]["extract"]["app_name"] == "test-app"
            assert body["dag"]["extract"]["inputs"]["task_queue"] == "prod-deploy-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_manifest_programmatic_takes_priority(self, tmp_path: Path) -> None:
        """When both programmatic and disk manifest exist, programmatic wins."""
        from application_sdk.handler import service as svc_module

        disk_data = {
            "execution_mode": "disk-mode",
            "dag": {
                "node": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Node",
                    "app_name": "disk-app",
                    "inputs": {"workflow_type": "t", "task_queue": "q"},
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(disk_data))

        manifest = self._make_manifest()
        app = create_app_handler_service(
            _TestHandler(), app_name="test-app", manifest=manifest
        )
        client = TestClient(app)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            # Programmatic manifest wins — has "dag" mode, not "disk-mode"
            assert body["execution_mode"] == "dag"
            assert "extract" in body["dag"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_404_when_none(self, tmp_path: Path) -> None:
        """Returns 404 when no manifest param and no manifest.json on disk."""
        from application_sdk.handler import service as svc_module

        missing = tmp_path / "nonexistent"
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = missing
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 404
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_simple_app_manifest_lives_at_root_not_in_subdir(
        self, tmp_path: Path
    ) -> None:
        """Simple apps (only run(), no @entrypoint) place manifest.json at
        CONTRACT_GENERATED_DIR/manifest.json — no subdirectory.

        Heracles does not pass ?entrypoint= for these apps.  The endpoint must
        serve the root file when no param is present, and must NOT search inside
        any named subdirectory.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)
        # Root manifest — correct location for a simple app.
        (contract_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "linear", "app_name": "simple-app"})
        )
        # A subdir manifest should NOT be found when no ?entrypoint= is provided.
        subdir = contract_dir / "run"
        subdir.mkdir()
        (subdir / "manifest.json").write_text(json.dumps({"app_name": "wrong"}))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            # No ?entrypoint= → root manifest, not the subdir one.
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["app_name"] == "simple-app"
            assert body["execution_mode"] == "linear"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_simple_app_manifest_convention_vs_multi_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """Documents the path convention side-by-side:

        - Simple app (no @entrypoint):  CONTRACT_GENERATED_DIR/manifest.json
          Served via GET /manifest (no ?entrypoint= param).
        - Multi-entrypoint app:         CONTRACT_GENERATED_DIR/{name}/manifest.json
          Served via GET /manifest?entrypoint={name}.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        # Simple-app root manifest.
        (contract_dir / "manifest.json").write_text(json.dumps({"app_name": "simple"}))
        # Multi-entrypoint subdir manifests.
        for ep_name in ("crawler", "miner"):
            ep_dir = contract_dir / ep_name
            ep_dir.mkdir()
            (ep_dir / "manifest.json").write_text(json.dumps({"app_name": ep_name}))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()

            # Simple app path: no param → root.
            resp = client.get("/workflows/v1/manifest")
            assert resp.status_code == 200
            assert resp.json()["app_name"] == "simple"

            # Multi-entrypoint paths: ?entrypoint= → subdir.
            for ep_name in ("crawler", "miner"):
                resp = client.get(f"/workflows/v1/manifest?entrypoint={ep_name}")
                assert resp.status_code == 200
                assert resp.json()["app_name"] == ep_name
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


class TestNormalizeCredentials:
    """Tests for _normalize_credentials v2→v3 compat shim."""

    def test_v3_list_passthrough(self) -> None:
        body = {"credentials": [{"key": "host", "value": "localhost"}]}
        result = _normalize_credentials(body)
        assert result["credentials"] == [{"key": "host", "value": "localhost"}]

    def test_v3_empty_list_passthrough(self) -> None:
        body = {"credentials": []}
        result = _normalize_credentials(body)
        assert result["credentials"] == []

    def test_missing_credentials_passthrough(self) -> None:
        body = {"other_key": "value"}
        result = _normalize_credentials(body)
        assert result == {"other_key": "value"}

    def test_null_credentials_passthrough(self) -> None:
        body = {"credentials": None}
        result = _normalize_credentials(body)
        assert result["credentials"] is None

    def test_v2_dict_conversion(self) -> None:
        body = {
            "credentials": {
                "host": "app.mode.com",
                "username": "user1",
                "password": "secret",
            }
        }
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "app.mode.com"
        assert keys["username"] == "user1"
        assert keys["password"] == "secret"

    def test_v2_extra_flattening(self) -> None:
        body = {
            "credentials": {
                "host": "app.mode.com",
                "extra": {"workspace": "atlan", "region": "us"},
            }
        }
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "app.mode.com"
        assert keys["extra.workspace"] == "atlan"
        assert keys["extra.region"] == "us"
        assert "extra" not in keys

    def test_none_values_excluded(self) -> None:
        body = {"credentials": {"host": "localhost", "port": None}}
        result = _normalize_credentials(body)
        keys = [c["key"] for c in result["credentials"]]
        assert "host" in keys
        assert "port" not in keys

    def test_non_string_values_serialized_as_json(self) -> None:
        body = {
            "credentials": {
                "host": "localhost",
                "port": 5432,
                "ssl": True,
                "options": {"timeout": 30},
            }
        }
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "localhost"
        assert keys["port"] == "5432"
        assert keys["ssl"] == "true"
        assert keys["options"] == '{"timeout": 30}'

    def test_non_dict_extra_ignored(self) -> None:
        body = {"credentials": {"host": "localhost", "extra": "not-a-dict"}}
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "localhost"
        assert "extra" not in keys

    def test_does_not_mutate_original_body(self) -> None:
        body = {
            "credentials": {"host": "localhost"},
            "metadata": {"key": "value"},
        }
        original_meta = body["metadata"]
        result = _normalize_credentials(body)
        assert result is not body
        assert result["metadata"] is original_meta
        assert isinstance(body["credentials"], dict)

    def test_preserves_other_body_fields(self) -> None:
        body = {
            "credentials": {"host": "localhost"},
            "connection_id": "conn-123",
            "timeout_seconds": 60,
        }
        result = _normalize_credentials(body)
        assert result["connection_id"] == "conn-123"
        assert result["timeout_seconds"] == 60

    def test_v2_auth_integration(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "credentials": {
                    "host": "app.mode.com",
                    "username": "user",
                    "password": "pass",
                    "extra": {"workspace": "ws"},
                }
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "success"

    # ── Flat top-level credential format (Heracles credential test) ──────

    def test_flat_toplevel_conversion(self) -> None:
        """Heracles sends flat top-level keys: {"host": ..., "authType": ...}."""
        body = {
            "host": "myns.servicebus.windows.net:9093",
            "port": 9093,
            "authType": "basic",
            "username": "$ConnectionString",
            "password": "Endpoint=sb://myns/;SharedAccessKeyName=key;SharedAccessKey=secret",
            "extra": {"security_protocol": "SASL_SSL"},
            "connectorConfigName": "atlan-connectors-azure-event-hub",
        }
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "myns.servicebus.windows.net:9093"
        assert keys["authType"] == "basic"
        assert keys["username"] == "$ConnectionString"
        assert "Endpoint=sb://" in keys["password"]
        assert keys["extra.security_protocol"] == "SASL_SSL"
        assert keys["port"] == "9093"

    def test_flat_toplevel_no_extra(self) -> None:
        body = {"host": "localhost", "authType": "basic", "password": "secret"}
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "localhost"
        assert keys["authType"] == "basic"

    def test_flat_toplevel_preserves_other_fields(self) -> None:
        body = {
            "host": "localhost",
            "authType": "basic",
            "connection_id": "conn-123",
            "timeout_seconds": 30,
        }
        result = _normalize_credentials(body)
        assert result["connection_id"] == "conn-123"
        assert result["timeout_seconds"] == 30
        assert isinstance(result["credentials"], list)

    def test_no_credential_keys_passthrough(self) -> None:
        """Body with no known credential keys should pass through unchanged."""
        body = {"connection_id": "conn-123", "metadata": {"key": "value"}}
        result = _normalize_credentials(body)
        assert "credentials" not in result
        assert result == body

    def test_flat_toplevel_auth_integration(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "host": "app.mode.com",
                "authType": "basic",
                "username": "user",
                "password": "pass",
                "extra": {"workspace": "ws"},
                "connectorConfigName": "test-connector",
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "success"


class TestFlattenToPairs:
    """Tests for _flatten_to_pairs (flat dict → v3 list)."""

    def test_simple_keys(self) -> None:
        result = _flatten_to_pairs({"host": "db.example.com", "username": "admin"})
        keys = {p["key"]: p["value"] for p in result}
        assert keys == {"host": "db.example.com", "username": "admin"}

    def test_extra_nested(self) -> None:
        creds = dict(host="db.example.com", extra={"role": "ADMIN", "warehouse": "WH"})
        result = _flatten_to_pairs(creds)
        keys = {p["key"]: p["value"] for p in result}
        assert keys["extra.role"] == "ADMIN"
        assert keys["extra.warehouse"] == "WH"
        assert "extra" not in keys

    def test_none_values_skipped(self) -> None:
        result = _flatten_to_pairs({"host": "db.example.com", "port": None})
        keys = [p["key"] for p in result]
        assert "port" not in keys

    def test_empty_dict(self) -> None:
        assert _flatten_to_pairs({}) == []

    def test_non_dict_extra_dropped(self) -> None:
        """Non-dict extra values are silently ignored."""
        result = _flatten_to_pairs({"host": "db.example.com", "extra": "string"})
        keys = {p["key"] for p in result}
        assert keys == {"host"}

    def test_mutates_input_extra_key(self) -> None:
        """_flatten_to_pairs pops 'extra' from the input dict."""
        creds = {"host": "db.example.com", "extra": {"role": "ADMIN"}}
        _flatten_to_pairs(creds)
        assert "extra" not in creds


class TestStartCredentialStripping:
    """Tests for credential stripping in /start handler.

    The /start endpoint strips credentials from the body before dispatching
    to Temporal. No state store save or credential_guid injection occurs.
    """

    def test_normalize_v2_dict_credentials(self) -> None:
        """V2 dict credentials are normalized to v3 list format."""
        body = {
            "credentials": {
                "host": "db.example.com",
                "port": 5432,
                "username": "admin",
                "password": "secret",
            },
            "other_field": "kept",
        }

        body = _normalize_credentials(body)

        assert isinstance(body["credentials"], list)
        keys = {item["key"] for item in body["credentials"]}
        assert "host" in keys
        assert "port" in keys
        # Other fields preserved
        assert body["other_field"] == "kept"

    def test_normalize_v3_list_credentials(self) -> None:
        """V3 list credentials pass through normalization unchanged."""
        body = {
            "credentials": [
                {"key": "host", "value": "db.example.com"},
                {"key": "username", "value": "admin"},
            ],
        }

        body = _normalize_credentials(body)

        assert isinstance(body["credentials"], list)
        assert len(body["credentials"]) == 2

    def test_no_credentials_passthrough(self) -> None:
        """Body without credentials passes through unchanged."""
        body = {"name": "test-workflow"}
        body = _normalize_credentials(body)
        assert "credentials" not in body or not body.get("credentials")


# ---------------------------------------------------------------------------
# Entrypoint manifest resolution tests (Option B: name == folder name)
# ---------------------------------------------------------------------------


class TestEntrypointManifestResolution:
    """Tests for GET /workflows/v1/manifest?entrypoint={name}.

    Option B: entrypoint name IS the folder name — no atlan.yaml parsing.
    GET /manifest?entrypoint=X serves CONTRACT_GENERATED_DIR/X/manifest.json.
    """

    def test_manifest_with_entrypoint_returns_correct_manifest(
        self, tmp_path: Path
    ) -> None:
        """GET /manifest?entrypoint=snowflake resolves to snowflake/manifest.json."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "snowflake"
        ep_dir.mkdir(parents=True)
        manifest_data = {"execution_mode": "dag", "app_name": "snowflake-extractor"}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=snowflake")
            assert response.status_code == 200
            body = response.json()
            assert body["execution_mode"] == "dag"
            assert body["app_name"] == "snowflake-extractor"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_manifest_substitutes_deployment_name_for_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """Entrypoint manifest replaces {deployment_name} placeholder."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "ep1"
        ep_dir.mkdir(parents=True)
        manifest_data = {"task_queue": "{deployment_name}-queue"}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        original_dep = svc_module.DEPLOYMENT_NAME
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        svc_module.DEPLOYMENT_NAME = "prod-deploy"
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=ep1")
            assert response.status_code == 200
            body = response.json()
            assert body["task_queue"] == "prod-deploy-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_valid_but_unknown_entrypoint_returns_404(self, tmp_path: Path) -> None:
        """Well-formed name with no matching subdir on disk returns 404."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=unknown")
            assert response.status_code == 404
            assert "No manifest found" in response.json()["detail"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_entrypoint_with_missing_manifest_file_returns_404(
        self, tmp_path: Path
    ) -> None:
        """Subdir exists but manifest.json is absent — returns 404."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        (contract_dir / "snowflake").mkdir(parents=True)  # dir, but no manifest.json

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=snowflake")
            assert response.status_code == 404
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_invalid_entrypoint_name_returns_400(self, tmp_path: Path) -> None:
        """Entrypoint names with path-traversal or illegal chars return 400."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            for bad_name in ["../etc", "a/b", "name with spaces", "name@bad"]:
                resp = client.get(f"/workflows/v1/manifest?entrypoint={bad_name}")
                assert resp.status_code == 400, f"Expected 400 for {bad_name!r}"
                assert resp.json()["detail"] == "Invalid entrypoint name"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_valid_name_not_in_registry_returns_404(self, tmp_path: Path) -> None:
        """A well-formed name with no matching manifest on disk returns 404.

        The glob-based registry only contains entrypoints whose manifest.json
        exists; a valid-format name with no file returns 404, not 400. Path escape
        is structurally impossible because glob() only yields paths under
        CONTRACT_GENERATED_DIR — no is_relative_to guard is needed.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            resp = client.get("/workflows/v1/manifest?entrypoint=valid")
            assert resp.status_code == 404
            assert "valid" in resp.json()["detail"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_legacy_manifest_alias_forwards_entrypoint(self, tmp_path: Path) -> None:
        """GET /manifest?entrypoint=name uses the legacy alias correctly."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "snow"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"app_name": "snow-app"}))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/manifest?entrypoint=snow")
            assert response.status_code == 200
            assert response.json()["app_name"] == "snow-app"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_no_entrypoint_param_serves_root_manifest(self, tmp_path: Path) -> None:
        """Without entrypoint param, serves root manifest unchanged."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)
        (contract_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "linear"})
        )

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            assert response.json()["execution_mode"] == "linear"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir


# ---------------------------------------------------------------------------
# Configmaps deduplication tests
# ---------------------------------------------------------------------------


class TestConfigMapsDeduplication:
    """Tests for list_configmaps deduplication across subdirectories."""

    def test_configmaps_dedupes_across_subdirs(self, tmp_path: Path) -> None:
        """Configmaps with same stem in different subdirs appear only once."""
        from application_sdk.handler import service as svc_module

        # Root-level configs
        (tmp_path / "config-a.json").write_text("{}")
        (tmp_path / "config-b.json").write_text("{}")

        # Sub-directory with duplicate stem + new config
        subdir = tmp_path / "snow-gen"
        subdir.mkdir()
        (subdir / "config-a.json").write_text("{}")  # duplicate of root
        (subdir / "config-c.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            # config-a should appear only once despite being in root and subdir
            assert configmaps.count("config-a") == 1
            assert "config-b" in configmaps
            assert "config-c" in configmaps
            assert "manifest" not in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_rglob_finds_nested_configs(self, tmp_path: Path) -> None:
        """rglob finds JSON files in nested sub-directories."""
        from application_sdk.handler import service as svc_module

        # Only sub-directory configs, no root configs
        subdir = tmp_path / "entrypoint-a"
        subdir.mkdir()
        (subdir / "nested-config.json").write_text("{}")

        deep_subdir = tmp_path / "entrypoint-b" / "deep"
        deep_subdir.mkdir(parents=True)
        (deep_subdir / "deep-config.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            assert "nested-config" in configmaps
            assert "deep-config" in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_excludes_manifest_in_subdirs(self, tmp_path: Path) -> None:
        """manifest.json in subdirectories is also excluded."""
        from application_sdk.handler import service as svc_module

        subdir = tmp_path / "ep-gen"
        subdir.mkdir()
        (subdir / "manifest.json").write_text("{}")
        (subdir / "real-config.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            assert "manifest" not in configmaps
            assert "real-config" in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


# ---------------------------------------------------------------------------
# Local vault credential provisioning tests
# ---------------------------------------------------------------------------


class TestProvisionLocalVault:
    """Tests for POST /workflows/v1/dev/local-vault credential provisioning endpoint."""

    def _make_local_vault_client(
        self,
        *,
        deployment_name: str = "local",
        local_environment: str = "local",
        storage: object | None = None,
    ) -> tuple[TestClient, dict[str, object]]:
        """Create a TestClient with DEPLOYMENT_NAME and storage patched.

        Returns (client, restore_dict) so the caller can restore module state.
        """
        from application_sdk.handler import service as svc_module

        handler = _TestHandler()
        app = create_app_handler_service(
            handler, app_name="vault-test", storage=storage
        )

        original_dep = svc_module.DEPLOYMENT_NAME
        original_local = svc_module.LOCAL_ENVIRONMENT
        svc_module.DEPLOYMENT_NAME = deployment_name
        svc_module.LOCAL_ENVIRONMENT = local_environment
        client = TestClient(app, raise_server_exceptions=False)
        restore = {
            "dep": original_dep,
            "local": original_local,
            "module": svc_module,
        }
        return client, restore

    def _restore(self, restore: dict[str, object]) -> None:
        svc_module = restore["module"]
        svc_module.DEPLOYMENT_NAME = restore["dep"]  # type: ignore[attr-defined]
        svc_module.LOCAL_ENVIRONMENT = restore["local"]  # type: ignore[attr-defined]

    def test_happy_path_returns_200_with_credential_guid(self, tmp_path: Path) -> None:
        """POST full credential body returns 200 with credential_guid, writes secrets.json."""
        import os

        # storage=None means objectstore save is a no-op (returns False)
        client, restore = self._make_local_vault_client(storage=None)

        body = {
            "username": "admin",
            "password": "secret123",
            "host": "db.example.com",
            "port": 5432,
            "extra": {"ssl": True},
            "url": "jdbc:postgresql://db.example.com:5432",
        }

        try:
            original_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                response = client.post("/workflows/v1/dev/local-vault", json=body)
            finally:
                os.chdir(original_cwd)

            assert response.status_code == 200
            resp_body = response.json()
            assert resp_body["success"] is True
            guid = resp_body["data"]["credential_guid"]
            # Verify it's a valid hex string (uuid4().hex)
            assert len(guid) == 32
            int(guid, 16)  # raises ValueError if not valid hex

            # Verify secrets.json was written with sensitive fields
            import orjson

            secrets_file = tmp_path / "local" / "dapr" / "secrets" / "secrets.json"
            assert secrets_file.exists()
            all_secrets = orjson.loads(secrets_file.read_bytes())
            assert guid in all_secrets
            stored_sensitive = all_secrets[guid]
            assert stored_sensitive["username"] == "admin"
            assert stored_sensitive["password"] == "secret123"
            assert stored_sensitive["extra"] == {"ssl": True}
            assert stored_sensitive["url"] == "jdbc:postgresql://db.example.com:5432"
            # Non-sensitive fields should NOT be in secrets
            assert "host" not in stored_sensitive
            assert "port" not in stored_sensitive
        finally:
            self._restore(restore)

    def test_dev_only_gate_rejects_non_local(self) -> None:
        """When DEPLOYMENT_NAME != LOCAL_ENVIRONMENT, returns 403."""
        client, restore = self._make_local_vault_client(
            deployment_name="production", local_environment="local"
        )
        try:
            response = client.post(
                "/workflows/v1/dev/local-vault", json={"host": "db.example.com"}
            )
            assert response.status_code == 403
            assert "Dev-only" in response.json()["detail"]
        finally:
            self._restore(restore)

    def test_sensitive_nonsensitive_split(self, tmp_path: Path) -> None:
        """Verify SENSITIVE_FIELDS go to secrets.json, everything else to object storage."""
        import os
        from unittest.mock import MagicMock, patch

        mock_storage = MagicMock()  # non-None so objectstore save runs
        client, restore = self._make_local_vault_client(storage=mock_storage)

        body = {
            "username": "user1",
            "password": "pass1",
            "extra": {"key": "val"},
            "url": "jdbc://host",
            "driverProperties": {"prop": "val"},
            "sodaConnection": "soda://conn",
            "host": "db.example.com",
            "port": 5432,
            "authType": "basic",
        }

        captured_config: dict = {}

        async def capture_put_json(key, body_arg, store):
            captured_config.update(body_arg)

        try:
            with patch(
                "application_sdk.storage.ops.put_json",
                side_effect=capture_put_json,
            ):
                original_cwd = os.getcwd()
                os.chdir(tmp_path)
                try:
                    response = client.post("/workflows/v1/dev/local-vault", json=body)
                finally:
                    os.chdir(original_cwd)

            assert response.status_code == 200
            guid = response.json()["data"]["credential_guid"]

            # Check secrets.json for sensitive fields
            import orjson

            secrets_file = tmp_path / "local" / "dapr" / "secrets" / "secrets.json"
            all_secrets = orjson.loads(secrets_file.read_bytes())
            sensitive = all_secrets[guid]
            assert set(sensitive.keys()) == {
                "username",
                "password",
                "extra",
                "url",
                "driverProperties",
                "sodaConnection",
            }

            # Non-sensitive fields should be in the captured config
            assert "host" in captured_config
            assert "port" in captured_config
            assert "authType" in captured_config
            assert captured_config["credentialSource"] == "direct"
            # Sensitive fields should NOT be in config
            assert "username" not in captured_config
            assert "password" not in captured_config
        finally:
            self._restore(restore)

    def test_auto_generates_uuid_hex(self, tmp_path: Path) -> None:
        """Verify the returned guid is a valid 32-char hex string."""
        import os

        client, restore = self._make_local_vault_client(storage=None)

        try:
            original_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                response = client.post(
                    "/workflows/v1/dev/local-vault", json={"host": "example.com"}
                )
            finally:
                os.chdir(original_cwd)

            assert response.status_code == 200
            guid = response.json()["data"]["credential_guid"]
            assert isinstance(guid, str)
            assert len(guid) == 32
            # Must be valid hex
            int(guid, 16)
        finally:
            self._restore(restore)

    def test_empty_body_succeeds(self, tmp_path: Path) -> None:
        """Empty body should still work — empty secrets + empty config."""
        import os

        client, restore = self._make_local_vault_client(storage=None)

        try:
            original_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                response = client.post("/workflows/v1/dev/local-vault", json={})
            finally:
                os.chdir(original_cwd)

            assert response.status_code == 200
            guid = response.json()["data"]["credential_guid"]

            # secrets.json should exist with empty sensitive dict
            import orjson

            secrets_file = tmp_path / "local" / "dapr" / "secrets" / "secrets.json"
            all_secrets = orjson.loads(secrets_file.read_bytes())
            assert all_secrets[guid] == {}
        finally:
            self._restore(restore)

    def test_no_storage_still_writes_secrets(self, tmp_path: Path) -> None:
        """When storage is None, secrets are written but objectstore save is a no-op."""
        import os

        client, restore = self._make_local_vault_client(storage=None)

        try:
            original_cwd = os.getcwd()
            os.chdir(tmp_path)
            try:
                response = client.post(
                    "/workflows/v1/dev/local-vault",
                    json={"username": "u", "host": "h"},
                )
            finally:
                os.chdir(original_cwd)

            # The endpoint still succeeds because _config_save_to_objectstore
            # returns False (no-op) when storage is None, but doesn't raise.
            assert response.status_code == 200
            guid = response.json()["data"]["credential_guid"]

            import orjson

            secrets_file = tmp_path / "local" / "dapr" / "secrets" / "secrets.json"
            all_secrets = orjson.loads(secrets_file.read_bytes())
            assert all_secrets[guid] == {"username": "u"}
        finally:
            self._restore(restore)


# ---------------------------------------------------------------------------
# BLDX-1129: tests targeting the bug class — runtime-only inline imports,
# swallowed exceptions, contract drift between handler service and its
# injected dependencies (Temporal, object store, etc.).
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_temporal_client_singleton():
    """Reset the module-level _temporal_client cache before and after a test.

    _get_temporal_client() caches a Temporal client globally; tests that
    exercise its real body (rather than patching it out) must reset this
    or they will leak state into other tests.
    """
    from application_sdk.handler import service as svc_module

    svc_module._temporal_client = None
    svc_module._handler_auth_manager = None
    yield
    svc_module._temporal_client = None
    svc_module._handler_auth_manager = None


class TestGetTemporalClientInlineImports:
    """BLDX-1129 regression: exercise the real _get_temporal_client body so a
    rename of create_temporal_client / TemporalAuthConfig / TemporalAuthManager
    in application_sdk.execution surfaces here, not in production.
    """

    @pytest.mark.asyncio
    async def test_no_auth_uses_create_temporal_client(
        self, reset_temporal_client_singleton
    ) -> None:
        from unittest.mock import AsyncMock, patch

        from application_sdk.handler import service as svc_module

        svc_module._workflow_config = svc_module.WorkflowClientConfig(
            host="temporal:7233",
            namespace="default",
            auth_enabled=False,
        )

        fake_client = object()
        with patch(
            "application_sdk.execution.create_temporal_client",
            new=AsyncMock(return_value=fake_client),
        ) as mock_create:
            result = await svc_module._get_temporal_client()

        assert result is fake_client
        # Must call with the configured host, namespace, and api_key=None when no auth
        kwargs = mock_create.call_args.kwargs
        assert mock_create.call_args.args[0] == "temporal:7233"
        assert mock_create.call_args.args[1] == "default"
        assert kwargs["api_key"] is None
        assert kwargs["tls_enabled"] is False
        assert kwargs["enable_prometheus"] is True
        assert kwargs["prometheus_bind_address"] == ""

    @pytest.mark.asyncio
    async def test_temporal_client_uses_metrics_runtime_config(
        self, reset_temporal_client_singleton
    ) -> None:
        from unittest.mock import AsyncMock, patch

        from application_sdk.handler import service as svc_module

        svc_module._workflow_config = svc_module.WorkflowClientConfig(
            host="temporal:7233",
            namespace="default",
            enable_temporal_core_metrics=False,
            prometheus_bind_address="127.0.0.1:9999",
        )

        fake_client = object()
        with patch(
            "application_sdk.execution.create_temporal_client",
            new=AsyncMock(return_value=fake_client),
        ) as mock_create:
            result = await svc_module._get_temporal_client()

        assert result is fake_client
        kwargs = mock_create.call_args.kwargs
        assert kwargs["enable_prometheus"] is False
        assert kwargs["prometheus_bind_address"] == "127.0.0.1:9999"

    @pytest.mark.asyncio
    async def test_returns_cached_client_on_second_call(
        self, reset_temporal_client_singleton
    ) -> None:
        from unittest.mock import AsyncMock, patch

        from application_sdk.handler import service as svc_module

        svc_module._workflow_config = svc_module.WorkflowClientConfig(
            host="temporal:7233", namespace="default"
        )
        sentinel = object()
        with patch(
            "application_sdk.execution.create_temporal_client",
            new=AsyncMock(return_value=sentinel),
        ) as mock_create:
            first = await svc_module._get_temporal_client()
            second = await svc_module._get_temporal_client()

        assert first is second is sentinel
        # Cached: only one underlying create
        assert mock_create.call_count == 1

    @pytest.mark.asyncio
    async def test_auth_enabled_uses_temporal_auth_manager(
        self, reset_temporal_client_singleton
    ) -> None:
        """When auth_enabled=True, the inline-imported TemporalAuthConfig and
        TemporalAuthManager must be wired up and the api_key threaded through.
        Catches BLDX-1129-class breakage if either symbol is renamed in
        application_sdk.execution.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.handler import service as svc_module

        svc_module._workflow_config = svc_module.WorkflowClientConfig(
            host="t:7233",
            namespace="default",
            auth_enabled=True,
            auth_client_id="cid",
            auth_client_secret="csec",
            auth_token_url="https://idp/token",
            auth_base_url="https://idp",
            auth_scopes="a,b",
        )

        fake_auth_manager = MagicMock()
        fake_auth_manager.acquire_initial_token = AsyncMock(return_value="api-key-xyz")
        fake_auth_manager.start_background_refresh = MagicMock()

        fake_client = object()
        with (
            patch(
                "application_sdk.execution.TemporalAuthManager",
                return_value=fake_auth_manager,
            ) as mock_mgr_cls,
            patch("application_sdk.execution.TemporalAuthConfig") as mock_cfg_cls,
            patch(
                "application_sdk.execution.create_temporal_client",
                new=AsyncMock(return_value=fake_client),
            ) as mock_create,
        ):
            result = await svc_module._get_temporal_client()

        assert result is fake_client
        mock_cfg_cls.assert_called_once()
        mock_mgr_cls.assert_called_once()
        # api_key from auth manager must be passed through to create_temporal_client
        assert mock_create.call_args.kwargs["api_key"] == "api-key-xyz"
        # Background token refresh must be started after client connect
        fake_auth_manager.start_background_refresh.assert_called_once_with(fake_client)


class TestGetWorkflowResultHelper:
    """Tests for _get_workflow_result coverage (lines 198-215)."""

    @pytest.mark.asyncio
    async def test_typed_dict_result_returned_as_dict(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.service import _get_workflow_result

        mock_handle = MagicMock()
        mock_handle.result = AsyncMock(return_value={"foo": "bar"})
        mock_client = MagicMock()
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        result = await _get_workflow_result(
            mock_client, workflow_id="wf-1", output_type=None
        )
        assert result == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_pydantic_result_is_model_dumped(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from pydantic import BaseModel

        from application_sdk.handler.service import _get_workflow_result

        class _Out(BaseModel):
            x: int = 1
            y: str = "ok"

        mock_handle = MagicMock()
        mock_handle.result = AsyncMock(return_value=_Out(x=42, y="hello"))
        mock_client = MagicMock()
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        result = await _get_workflow_result(
            mock_client, workflow_id="wf-2", output_type=_Out
        )
        assert result == {"x": 42, "y": "hello"}

    @pytest.mark.asyncio
    async def test_unknown_result_type_returns_empty_dict(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.service import _get_workflow_result

        mock_handle = MagicMock()
        mock_handle.result = AsyncMock(return_value="not-a-dict-or-model")
        mock_client = MagicMock()
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        result = await _get_workflow_result(
            mock_client, workflow_id="wf-3", output_type=None
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_typed_failure_falls_back_to_untyped(self) -> None:
        """If typed deserialization fails, fall back to untyped result.
        Verifies the swallowed-but-logged exception path still returns data.
        """
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.service import _get_workflow_result

        # First call (typed) raises; second (untyped) succeeds
        first_handle = MagicMock()
        first_handle.result = AsyncMock(side_effect=RuntimeError("type mismatch"))
        second_handle = MagicMock()
        second_handle.result = AsyncMock(return_value={"recovered": True})

        mock_client = MagicMock()
        mock_client.get_workflow_handle = MagicMock(
            side_effect=[first_handle, second_handle]
        )

        result = await _get_workflow_result(
            mock_client, workflow_id="wf-4", output_type=int
        )
        assert result == {"recovered": True}
        # Two handles fetched: one typed, one untyped fallback
        assert mock_client.get_workflow_handle.call_count == 2


class TestStopWorkflowEndpoint:
    """Tests for POST /workflows/v1/stop/{workflow_id}/{run_id:path}."""

    def _make_routed_client(self):
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _StopApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="stop-test",
            app_class=_StopApp,
            temporal_host="temporal:7233",
        )
        return svc

    def test_stop_success_returns_200(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._make_routed_client()
            mock_handle = MagicMock()
            mock_handle.terminate = AsyncMock(return_value=None)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.post("/workflows/v1/stop/wf-1/run-1")
            assert response.status_code == 200
            body = response.json()
            assert body["success"] is True
            mock_handle.terminate.assert_awaited_once()
        finally:
            from application_sdk.app.registry import AppRegistry, TaskRegistry

            AppRegistry.reset()
            TaskRegistry.reset()

    def test_stop_not_found_returns_404(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._make_routed_client()
            mock_handle = MagicMock()
            import temporalio.service

            mock_handle.terminate = AsyncMock(
                side_effect=temporalio.service.RPCError(
                    "wording can change",
                    status=temporalio.service.RPCStatusCode.NOT_FOUND,
                    raw_grpc_status=None,
                )
            )
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.post("/workflows/v1/stop/wf-x/run-x")
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()
        finally:
            from application_sdk.app.registry import AppRegistry, TaskRegistry

            AppRegistry.reset()
            TaskRegistry.reset()

    def test_stop_generic_error_returns_500_without_leaking_details(self) -> None:
        """Generic Temporal errors → 500 with a generic message, no stack/internals."""
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._make_routed_client()
            mock_handle = MagicMock()
            mock_handle.terminate = AsyncMock(
                side_effect=RuntimeError("connection refused: 10.0.0.42:7233")
            )
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.post("/workflows/v1/stop/wf-1/run-1")
            assert response.status_code == 500
            body_str = str(response.json())
            assert "10.0.0.42" not in body_str
            assert "RuntimeError" not in body_str
        finally:
            from application_sdk.app.registry import AppRegistry, TaskRegistry

            AppRegistry.reset()
            TaskRegistry.reset()


class TestGetWorkflowStatusEndpoint:
    """Tests for GET /workflows/v1/status/{workflow_id}/{run_id:path}."""

    def _setup(self):
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _StatusApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        return create_app_handler_service(
            _TestHandler(),
            app_name="status-test",
            app_class=_StatusApp,
            temporal_host="temporal:7233",
        )

    def _teardown(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def test_status_running_returns_status_payload(self) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            now = datetime.now(UTC)
            desc = MagicMock()
            desc.status.name = "RUNNING"
            desc.execution_time = now - timedelta(seconds=10)
            desc.close_time = None
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/status/wf-1/run-1")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "RUNNING"
            assert data["execution_duration_seconds"] >= 10
        finally:
            self._teardown()

    def test_status_completed_uses_close_time_for_duration(self) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            start = datetime(2024, 1, 1, tzinfo=UTC)
            desc = MagicMock()
            desc.status.name = "COMPLETED"
            desc.execution_time = start
            desc.close_time = start + timedelta(seconds=42)
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/status/wf-1/run-1")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["execution_duration_seconds"] == 42
        finally:
            self._teardown()

    def test_status_unknown_when_status_falsy(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status = None
            desc.execution_time = None
            desc.close_time = None
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/status/wf-1/run-1")
            assert response.status_code == 200
            assert response.json()["data"]["status"] == "UNKNOWN"
            assert response.json()["data"]["execution_duration_seconds"] == 0
        finally:
            self._teardown()

    def test_status_not_found_returns_404(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        import temporalio.service

        try:
            svc = self._setup()
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(
                side_effect=temporalio.service.RPCError(
                    "wording can change",
                    status=temporalio.service.RPCStatusCode.NOT_FOUND,
                    raw_grpc_status=None,
                )
            )
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.get("/workflows/v1/status/wf-x/run-x")
            assert response.status_code == 404
        finally:
            self._teardown()

    def test_status_generic_error_returns_500(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(side_effect=RuntimeError("boom"))
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.get("/workflows/v1/status/wf-1/run-1")
            assert response.status_code == 500
            assert "boom" not in str(response.json())
        finally:
            self._teardown()


class TestGetResultEndpoint:
    """Tests for GET /workflows/v1/result/{workflow_id}."""

    def _setup(self):
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _ResApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        return create_app_handler_service(
            _TestHandler(),
            app_name="res-test",
            app_class=_ResApp,
            temporal_host="temporal:7233",
        )

    def _teardown(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def test_result_running_returns_running_status(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status.name = "RUNNING"
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-r")
            assert response.status_code == 200
            assert response.json()["data"]["status"] == "running"
        finally:
            self._teardown()

    def test_result_completed_returns_result_data(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status.name = "COMPLETED"
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    new=AsyncMock(return_value={"a": 1}),
                ),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-c")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "completed"
            assert data["result"] == {"a": 1}
        finally:
            self._teardown()

    @pytest.mark.parametrize(
        "temporal_status", ["FAILED", "TERMINATED", "CANCELED", "TIMED_OUT"]
    )
    def test_result_failed_states_return_failed_status(
        self, temporal_status: str
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status.name = temporal_status
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-f")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "failed"
            assert temporal_status.lower() in data["error"].lower()
        finally:
            self._teardown()

    def test_result_unknown_status_returns_lowercased(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status.name = "EXOTIC_STATE"
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-other")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "exotic_state"
        finally:
            self._teardown()

    def test_result_wait_true_returns_completed(self) -> None:
        """With ?wait=true, retrieve the workflow result directly."""
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.workflow_type = "res-test"  # single implicit entrypoint
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    new=AsyncMock(return_value={"final": "value"}),
                ),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-wait?wait=true")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "completed"
            assert data["result"] == {"final": "value"}
        finally:
            self._teardown()

    def test_result_wait_failure_returns_failed_envelope(self) -> None:
        """A non-WorkflowFailureError raised during result decoding produces
        the new ``result_decode_failed`` status (PR #1603 / BLDX-1173) —
        distinct from a real workflow failure. Both still set
        ``success=false`` and must not leak the underlying exception text.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.workflow_type = "res-test"
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    new=AsyncMock(side_effect=RuntimeError("internal-error-xxx")),
                ),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-wait-fail?wait=true")
            assert response.status_code == 200
            body = response.json()
            data = body["data"]
            # Generic exception during decode → new distinct status.
            assert data["status"] == "result_decode_failed"
            assert body["success"] is False
            # Generic message — must not leak the raw exception text.
            assert "internal-error-xxx" not in str(body)
        finally:
            self._teardown()

    def test_result_not_found_returns_404(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        import temporalio.service

        try:
            svc = self._setup()
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(
                side_effect=temporalio.service.RPCError(
                    "wording can change",
                    status=temporalio.service.RPCStatusCode.NOT_FOUND,
                    raw_grpc_status=None,
                )
            )
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.get("/workflows/v1/result/wf-nope")
            assert response.status_code == 404
        finally:
            self._teardown()

    def test_result_503_when_unconfigured(self) -> None:
        client = _make_client()
        response = client.get("/workflows/v1/result/wf-1")
        assert response.status_code == 503

    def test_status_503_when_unconfigured(self) -> None:
        client = _make_client()
        response = client.get("/workflows/v1/status/wf-1/run-1")
        assert response.status_code == 503


class TestGetResultMultiEntrypointOutputType:
    """Regression: /result must resolve per-entrypoint output_type from
    desc.workflow_type, not use the app's first-registered type or None.

    Original bug: _output_type on the App class was set to the first entry
    point's Output type (alphabetically via dir()), so any workflow that ran
    a different entry point would be deserialised with the wrong schema.

    The fix uses desc.workflow_type (e.g. "app-name:beta") to look up the
    correct EntryPointMetadata and pass its output_type to _get_workflow_result.
    """

    def _setup(self):
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _MultiEpApp(App):
            name = "multi-ep-test"

            @entrypoint
            async def alpha(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

            @entrypoint
            async def beta(self, input: _BetaInput) -> _BetaOutput:
                return _BetaOutput()

        return create_app_handler_service(
            _TestHandler(),
            app_name="multi-ep-test",
            app_class=_MultiEpApp,
            temporal_host="temporal:7233",
        )

    def _teardown(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    @pytest.mark.parametrize(
        "ep_name,expected_output_type",
        [
            ("alpha", _AlphaOutput),
            ("beta", _BetaOutput),
        ],
    )
    def test_result_completed_resolves_per_entrypoint_output_type(
        self, ep_name: str, expected_output_type: type
    ) -> None:
        """Polling path: output_type passed to _get_workflow_result must match
        the entrypoint encoded in desc.workflow_type, not the app's
        first-registered type (_AlphaOutput) and not None."""
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()

            desc = MagicMock()
            desc.status.name = "COMPLETED"
            desc.workflow_type = f"multi-ep-test:{ep_name}"

            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

            captured: list[type | None] = []

            async def _spy(client, *, workflow_id, output_type):
                captured.append(output_type)
                return {"result_field": "ok"}

            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    side_effect=_spy,
                ),
            ):
                client = TestClient(svc)
                response = client.get(f"/workflows/v1/result/wf-{ep_name}")

            assert response.status_code == 200
            assert len(captured) == 1
            assert captured[0] is expected_output_type, (
                f"For entrypoint {ep_name!r} expected output_type="
                f"{expected_output_type.__name__!r}, got {captured[0]!r}. "
                "The /result endpoint must resolve per-entrypoint output_type "
                "from desc.workflow_type, not use the app-level first-registered "
                "type or None."
            )
        finally:
            self._teardown()

    def test_result_wait_resolves_per_entrypoint_output_type(self) -> None:
        """wait=True path: describe() must be called to obtain workflow_type
        so the correct output_type can be resolved before waiting."""
        from unittest.mock import AsyncMock, MagicMock, patch

        try:
            svc = self._setup()

            desc = MagicMock()
            desc.workflow_type = "multi-ep-test:beta"

            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

            captured: list[type | None] = []

            async def _spy(client, *, workflow_id, output_type):
                captured.append(output_type)
                return {"result_field": "ok"}

            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    side_effect=_spy,
                ),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-beta-wait?wait=true")

            assert response.status_code == 200
            assert len(captured) == 1
            assert captured[0] is _BetaOutput, (
                f"For wait=True + 'beta' entrypoint expected output_type=_BetaOutput, "
                f"got {captured[0]!r}. "
                "The wait=True path must call describe() and resolve output_type "
                "from desc.workflow_type."
            )
        finally:
            self._teardown()


class TestStartWorkflowExtras:
    """Tests for /start beyond simple routing — explicit workflow_id, caller
    correlation_id, credential stripping, and error branches."""

    def setup_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def _wire(self, app_cls):
        """Build service + patch _get_temporal_client. Caller must reset
        registries beforehand and define app_cls AFTER reset."""
        from unittest.mock import AsyncMock, MagicMock, patch

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="start-extras",
            app_class=app_cls,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-id-actual"
        mock_handle.result_run_id = "run-id-actual"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        return TestClient(svc, raise_server_exceptions=False), mock_client, patcher

    def test_explicit_workflow_id_is_used(self) -> None:
        from application_sdk.app.base import App

        class _IdApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_IdApp)
        try:
            response = client.post(
                "/workflows/v1/start",
                json={"name": "x", "workflow_id": "caller-given-id"},
            )
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["id"] == "caller-given-id"
        finally:
            patcher.stop()

    def test_caller_correlation_id_round_trips(self) -> None:
        """If caller provides correlation_id, response echoes the same value."""
        from application_sdk.app.base import App

        class _CorrApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_CorrApp)
        try:
            response = client.post(
                "/workflows/v1/start",
                json={"name": "x", "correlation_id": "trace-9999"},
            )
            assert response.status_code == 200
            assert response.json()["correlation_id"] == "trace-9999"
        finally:
            patcher.stop()

    def test_inline_credentials_are_stripped_before_dispatch(self) -> None:
        """Credentials must NEVER reach Temporal history — start_workflow's
        first arg (input_data) must not contain raw credential pairs."""
        from application_sdk.app.base import App

        class _StripApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_StripApp)
        try:
            response = client.post(
                "/workflows/v1/start",
                json={
                    "name": "x",
                    "credentials": [{"key": "password", "value": "supersecret"}],
                },
            )
            assert response.status_code == 200
            call_args = mock_client.start_workflow.call_args
            input_data = call_args.kwargs["args"][0]
            dumped = (
                input_data.model_dump()
                if hasattr(input_data, "model_dump")
                else vars(input_data)
            )
            assert "supersecret" not in str(dumped)
        finally:
            patcher.stop()

    def test_start_response_envelope_shape(self) -> None:
        from application_sdk.app.base import App

        class _EnvApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, _, patcher = self._wire(_EnvApp)
        try:
            response = client.post("/workflows/v1/start", json={"name": "x"})
            body = response.json()
            assert set(body.keys()) >= {"success", "message", "data", "correlation_id"}
            assert body["success"] is True
            assert "workflow_id" in body["data"]
            assert "run_id" in body["data"]
        finally:
            patcher.stop()


class TestStartWorkflowExecutionTimeout:
    """Tests for execution_timeout wiring in /workflows/v1/start."""

    def setup_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def _wire(self, app_cls, workflow_max_timeout_hours=None):
        from unittest.mock import AsyncMock, MagicMock, patch

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="timeout-test",
            app_class=app_cls,
            temporal_host="temporal:7233",
            workflow_max_timeout_hours=workflow_max_timeout_hours,
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-id"
        mock_handle.result_run_id = "run-id"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        return TestClient(svc, raise_server_exceptions=False), mock_client, patcher

    def test_execution_timeout_set_when_configured(self) -> None:
        """execution_timeout is passed to start_workflow when workflow_max_timeout_hours is set."""
        from datetime import timedelta

        from application_sdk.app.base import App

        class _TApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_TApp, workflow_max_timeout_hours=48)
        try:
            response = client.post("/workflows/v1/start", json={"name": "x"})
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["execution_timeout"] == timedelta(hours=48)
        finally:
            patcher.stop()

    def test_execution_timeout_none_when_not_configured(self) -> None:
        """execution_timeout is None when workflow_max_timeout_hours is not set."""
        from application_sdk.app.base import App

        class _TApp2(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, mock_client, patcher = self._wire(_TApp2)
        try:
            response = client.post("/workflows/v1/start", json={"name": "x"})
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["execution_timeout"] is None
        finally:
            patcher.stop()


class TestUploadFileEndpoint:
    """Tests for POST /workflows/v1/file."""

    def test_upload_no_storage_returns_503(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/file",
            files={"file": ("hello.txt", b"hello")},
        )
        assert response.status_code == 503

    def test_upload_happy_path_returns_metadata(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="upload-test", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.upload_file", new=AsyncMock(return_value=None)
        ) as mock_upload:
            response = client.post(
                "/workflows/v1/file",
                files={"file": ("report.pdf", b"data", "application/pdf")},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        # alias_generator=to_camel — fields exposed as camelCase
        assert data["rawName"] == "report.pdf"
        assert data["extension"] == "pdf"
        assert data["contentType"] == "application/pdf"
        assert data["fileSize"] == 4
        assert data["isUploaded"] is True
        # Storage upload was actually invoked
        mock_upload.assert_awaited_once()

    def test_upload_with_explicit_filename_and_prefix_overrides(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="upload2", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.upload_file", new=AsyncMock(return_value=None)
        ) as mock_upload:
            response = client.post(
                "/workflows/v1/file",
                files={"file": ("ignored.txt", b"hi")},
                data={"filename": "real.csv", "prefix": "custom/path"},
            )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["rawName"] == "real.csv"
        assert data["extension"] == "csv"
        # Key must use the supplied prefix, not the default
        assert data["key"].startswith("custom/path/")
        # And the storage layer was called with that same key
        called_key = mock_upload.await_args.args[0]
        assert called_key.startswith("custom/path/")

    def test_upload_strips_unsafe_extension_chars(self) -> None:
        """Extensions with non-alphanumerics must be sanitised — no path traversal."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="upload3", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.upload_file", new=AsyncMock(return_value=None)
        ):
            response = client.post(
                "/workflows/v1/file",
                files={"file": ("evil.tar/../foo", b"x")},
            )
        assert response.status_code == 200
        data = response.json()["data"]
        # Extension must not contain '/' or '.'
        assert "/" not in data["extension"]
        assert ".." not in data["extension"]

    def test_upload_falls_back_to_octet_stream_when_no_content_type(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="upload4", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.upload_file", new=AsyncMock(return_value=None)
        ):
            response = client.post(
                "/workflows/v1/file",
                files={"file": ("blob", b"raw")},
            )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["contentType"] == "application/octet-stream"


class TestEventTriggerEndpoint:
    """Tests for POST /events/v1/event/{event_id}."""

    def _make_event_app(self):
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _EvApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # Tag the app class so the event handler can find _input_type
        _EvApp._input_type = _RoutingInput
        return _EvApp

    def _teardown(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def test_event_503_when_temporal_unconfigured(self) -> None:
        from application_sdk.handler.contracts import EventTriggerConfig

        trigger = EventTriggerConfig(event_id="t1", event_type="topic", event_name="ev")
        app = create_app_handler_service(
            _TestHandler(), app_name="ev-test", event_triggers=[trigger]
        )
        client = TestClient(app)
        response = client.post("/events/v1/event/t1", json={})
        assert response.status_code == 503

    def test_event_unknown_id_returns_404(self) -> None:
        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="known", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            client = TestClient(app)
            response = client.post("/events/v1/event/unknown", json={})
            assert response.status_code == 404
            assert "unknown" in response.json()["detail"]
        finally:
            self._teardown()

    def test_event_invalid_json_returns_400(self) -> None:
        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            client = TestClient(app)
            response = client.post(
                "/events/v1/event/t1",
                content=b"not-valid-json",
                headers={"content-type": "application/json"},
            )
            assert response.status_code == 400
        finally:
            self._teardown()

    def test_event_happy_path_starts_workflow(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            mock_handle = MagicMock()
            mock_handle.id = "wf-1"
            mock_handle.result_run_id = "run-1"
            mock_client = MagicMock()
            mock_client.start_workflow = AsyncMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post(
                    "/events/v1/event/t1",
                    json={"data": {"name": "from-event"}},
                )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "SUCCESS"
            assert body["workflow_id"] == "wf-1"
        finally:
            self._teardown()

    def test_event_temporal_failure_returns_retry_status(self) -> None:
        """When Temporal start fails, the endpoint returns 500 + RETRY so Dapr
        will retry the message — per the bus contract."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            mock_client = MagicMock()
            mock_client.start_workflow = AsyncMock(
                side_effect=RuntimeError("temporal down")
            )
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post("/events/v1/event/t1", json={"data": {}})
            assert response.status_code == 500
            assert response.json()["status"] == "RETRY"
        finally:
            self._teardown()

    def test_event_execution_timeout_set_when_configured(self) -> None:
        """execution_timeout is passed to start_workflow on the event route when configured."""
        from datetime import timedelta
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
                workflow_max_timeout_hours=48,
            )
            mock_client = MagicMock()
            mock_handle = MagicMock()
            mock_handle.id = "wf-id"
            mock_handle.result_run_id = "run-id"
            mock_client.start_workflow = AsyncMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post("/events/v1/event/t1", json={})
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["execution_timeout"] == timedelta(hours=48)
        finally:
            self._teardown()

    def test_event_execution_timeout_none_when_not_configured(self) -> None:
        """execution_timeout is None on the event route when workflow_max_timeout_hours is unset."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.handler.contracts import EventTriggerConfig

        try:
            app_cls = self._make_event_app()
            trigger = EventTriggerConfig(
                event_id="t1", event_type="topic", event_name="ev"
            )
            app = create_app_handler_service(
                _TestHandler(),
                app_name="ev-test",
                app_class=app_cls,
                temporal_host="t:7233",
                event_triggers=[trigger],
            )
            mock_client = MagicMock()
            mock_handle = MagicMock()
            mock_handle.id = "wf-id"
            mock_handle.result_run_id = "run-id"
            mock_client.start_workflow = AsyncMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(app)
                response = client.post("/events/v1/event/t1", json={})
            assert response.status_code == 200
            kwargs = mock_client.start_workflow.call_args.kwargs
            assert kwargs["execution_timeout"] is None
        finally:
            self._teardown()


class TestFrontendHomeEndpoint:
    """Tests for GET / (frontend index.html)."""

    def test_frontend_home_missing_returns_placeholder_html(
        self, tmp_path: Path
    ) -> None:
        # frontend_assets_path doesn't contain index.html
        app = create_app_handler_service(
            _TestHandler(),
            app_name="ui-test",
            frontend_assets_path=str(tmp_path / "missing"),
        )
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 404
        assert "UI not available" in response.text

    def test_frontend_home_serves_index_html_when_present(self, tmp_path: Path) -> None:
        assets = tmp_path / "static"
        assets.mkdir()
        (assets / "index.html").write_text("<html><body>HELLO</body></html>")
        app = create_app_handler_service(
            _TestHandler(),
            app_name="ui-test-2",
            frontend_assets_path=str(assets),
        )
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "HELLO" in response.text


class TestMetricsEndpoint:
    """Tests for GET /metrics Temporal-core proxy behavior."""

    def test_metrics_skips_temporal_core_proxy_when_disabled(
        self, tmp_path: Path
    ) -> None:
        app = create_app_handler_service(
            _TestHandler(),
            app_name="metrics-test",
            enable_temporal_core_metrics=False,
            frontend_assets_path=str(tmp_path),
        )
        client = TestClient(app)

        with patch("httpx.AsyncClient") as mock_async_client:
            response = client.get("/metrics")

        assert response.status_code == 200
        assert "python_info" in response.text
        mock_async_client.assert_not_called()

    def test_metrics_connect_error_logs_warning_with_traceback(
        self, tmp_path: Path
    ) -> None:
        import httpx

        class _UnavailableTemporalMetricsClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def get(self, url: str) -> object:
                raise httpx.ConnectError("All connection attempts failed")

        app = create_app_handler_service(
            _TestHandler(),
            app_name="metrics-test",
            enable_temporal_core_metrics=True,
            prometheus_bind_address="127.0.0.1:9464",
            frontend_assets_path=str(tmp_path),
        )
        client = TestClient(app)

        with (
            patch("httpx.AsyncClient", new=_UnavailableTemporalMetricsClient),
            patch("application_sdk.handler.service.logger.debug") as mock_debug,
            patch("application_sdk.handler.service.logger.warning") as mock_warning,
        ):
            response = client.get("/metrics")

        assert response.status_code == 200
        assert "python_info" in response.text
        mock_debug.assert_not_called()
        mock_warning.assert_called_once()
        assert "proxy request failed" in mock_warning.call_args.args[0]
        assert mock_warning.call_args.kwargs.get("exc_info") is True

    def test_metrics_request_error_logs_warning_with_traceback(
        self, tmp_path: Path
    ) -> None:
        import httpx

        class _FailingTemporalMetricsClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def get(self, url: str) -> object:
                raise httpx.ReadTimeout("read timed out")

        app = create_app_handler_service(
            _TestHandler(),
            app_name="metrics-test",
            enable_temporal_core_metrics=True,
            prometheus_bind_address="127.0.0.1:9464",
            frontend_assets_path=str(tmp_path),
        )
        client = TestClient(app)

        with (
            patch("httpx.AsyncClient", new=_FailingTemporalMetricsClient),
            patch("application_sdk.handler.service.logger.debug") as mock_debug,
            patch("application_sdk.handler.service.logger.warning") as mock_warning,
        ):
            response = client.get("/metrics")

        assert response.status_code == 200
        assert "python_info" in response.text
        mock_debug.assert_not_called()
        mock_warning.assert_called_once()
        assert "proxy request failed" in mock_warning.call_args.args[0]
        assert mock_warning.call_args.kwargs.get("exc_info") is True


class TestRunAppHandlerService:
    """Smoke test for run_app_handler_service — ensures uvicorn wiring is OK."""

    def test_run_invokes_uvicorn_with_built_app(self) -> None:
        from unittest.mock import patch

        from application_sdk.handler.service import run_app_handler_service

        with patch("uvicorn.run") as mock_run:
            run_app_handler_service(
                _TestHandler(), host="127.0.0.1", port=9000, log_level="warning"
            )
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        # First positional arg is the FastAPI app instance
        assert kwargs.get("host") == "127.0.0.1"
        assert kwargs.get("port") == 9000
        assert kwargs.get("log_level") == "warning"


class TestWorkflowClientConfig:
    """Tests for WorkflowClientConfig.is_configured()."""

    def test_unconfigured_when_host_missing(self) -> None:
        from application_sdk.handler.service import WorkflowClientConfig

        cfg = WorkflowClientConfig(host="", task_queue="q")
        assert cfg.is_configured() is False

    def test_unconfigured_when_app_class_missing(self) -> None:
        from application_sdk.handler.service import WorkflowClientConfig

        cfg = WorkflowClientConfig(host="t:7233", app_class=None)
        assert cfg.is_configured() is False

    def test_configured_when_host_and_app_class_present(self) -> None:
        from application_sdk.handler.service import WorkflowClientConfig

        class _Cls:
            pass

        cfg = WorkflowClientConfig(host="t:7233", app_class=_Cls, app_name="test-app")  # type: ignore[arg-type]
        assert cfg.is_configured() is True


class TestStateStoreDeprecation:
    """state_store= parameter is deprecated and ignored — verify warning."""

    def test_state_store_emits_deprecation_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            create_app_handler_service(
                _TestHandler(),
                app_name="dep-test",
                state_store="some-state-store",
            )
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "state_store" in str(w.message)
            for w in caught
        )

    def test_no_state_store_no_deprecation_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            create_app_handler_service(_TestHandler(), app_name="no-dep")
        # No state_store warning should be emitted
        assert not any(
            issubclass(w.category, DeprecationWarning)
            and "state_store" in str(w.message)
            for w in caught
        )


class TestConfigEndpointPersistence:
    """Tests for /workflows/v1/config/{id} round-trip with a real obstore mock."""

    def test_get_config_503_when_no_storage(self) -> None:
        client = _make_client()
        response = client.get("/workflows/v1/config/some-id")
        assert response.status_code == 503

    def test_post_config_503_when_no_storage(self) -> None:
        client = _make_client()
        response = client.post("/workflows/v1/config/some-id", json={"k": "v"})
        assert response.status_code == 503

    def test_get_config_404_when_storage_present_but_key_missing(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="cfg-test", storage=mock_storage
        )
        client = TestClient(app)
        # download_file raises → _config_load_from_objectstore returns None
        with patch(
            "application_sdk.storage.ops.download_file",
            new=AsyncMock(side_effect=FileNotFoundError("missing")),
        ):
            response = client.get("/workflows/v1/config/abc-123")
        assert response.status_code == 404
        assert "abc-123" in response.json()["detail"]

    def test_post_config_200_when_storage_present(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="cfg-post", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.put_json", new=AsyncMock(return_value=None)
        ) as mock_put:
            response = client.post(
                "/workflows/v1/config/abc-123",
                params={"type": "credentials"},
                json={"foo": "bar"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == {"foo": "bar"}
        mock_put.assert_awaited_once()
        # Key must conform to the v2 path convention
        called_key = mock_put.await_args.args[0]
        assert called_key.endswith("/credentials/abc-123/config.json")

    def test_get_config_200_round_trip(self) -> None:
        """Full round-trip: object store returns bytes, endpoint deserializes them."""
        from unittest.mock import AsyncMock, MagicMock, patch

        import orjson

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="cfg-rt", storage=mock_storage
        )
        client = TestClient(app)

        async def fake_download(key, dest, store):
            Path(dest).write_bytes(orjson.dumps({"hello": "world"}))

        with patch(
            "application_sdk.storage.ops.download_file",
            new=AsyncMock(side_effect=fake_download),
        ):
            response = client.get("/workflows/v1/config/round-trip")
        assert response.status_code == 200
        assert response.json()["data"] == {"hello": "world"}


class TestComputeManifestHook:
    """Per-entrypoint dynamic manifest hook.

    Apps that need to compute the manifest per submission (placeholder
    fill-in, SQL gen, full DAG rewrite) drop a `core.py` at
    ``app.<entrypoint_snake>.core`` exposing a
    ``compute_manifest(manifest, fe_inputs) -> dict`` callable. The SDK's
    /manifest handler discovers it via importlib and substitutes the
    return value as the response body.

    Tests inject a fake module into ``sys.modules`` to simulate the
    convention without touching the filesystem.
    """

    @staticmethod
    def _install_fake_core(
        monkeypatch: pytest.MonkeyPatch, entrypoint: str, fn: object
    ) -> None:
        """Register ``app.<snake>.core`` with ``compute_manifest = fn``."""
        import sys
        import types

        from application_sdk.app.entrypoint import entrypoint_module_segment

        snake = entrypoint_module_segment(entrypoint)
        # Ensure the parent packages exist so ``import app.<snake>.core`` resolves.
        for parent in ("app", f"app.{snake}"):
            if parent not in sys.modules:
                pkg = types.ModuleType(parent)
                pkg.__path__ = []  # type: ignore[attr-defined]
                monkeypatch.setitem(sys.modules, parent, pkg)
        core = types.ModuleType(f"app.{snake}.core")
        core.compute_manifest = fn  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, f"app.{snake}.core", core)

    def test_optional_import_returns_none_when_target_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely-missing module (target or a parent) → None (fall-through)."""
        import importlib

        from application_sdk.handler import service as svc

        def _missing(name):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)

        monkeypatch.setattr(importlib, "import_module", _missing)
        assert svc._import_optional_app_module("app.foo.core") is None

    def test_optional_import_reraises_transitive_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A module that exists but whose own import fails (missing dependency)
        must surface, not be swallowed into a silent fall-through."""
        import importlib

        from application_sdk.handler import service as svc

        def _broken(name):
            # The target's code imports 'somedep', which isn't installed.
            raise ModuleNotFoundError("No module named 'somedep'", name="somedep")

        monkeypatch.setattr(importlib, "import_module", _broken)
        with pytest.raises(ModuleNotFoundError):
            svc._import_optional_app_module("app.foo.core")

    def test_hook_invoked_when_module_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """compute_manifest is called and its return becomes the body."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "hook-ep"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(
            json.dumps({"dag": {"extract": {"static": True}}})
        )

        captured: dict[str, object] = {}

        def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            captured["manifest"] = manifest
            captured["fe_inputs"] = fe_inputs
            return {"dag": {"extract": {"computed": True, "echo": fe_inputs}}}

        self._install_fake_core(monkeypatch, "hook-ep", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            payload = {"foo": "bar"}
            response = client.get(
                "/workflows/v1/manifest",
                params={"entrypoint": "hook-ep", "fe_inputs": json.dumps(payload)},
            )
            assert response.status_code == 200
            body = response.json()
            assert body == {"dag": {"extract": {"computed": True, "echo": payload}}}
            # The hook receives the static manifest unmodified and the decoded form.
            assert captured["manifest"] == {"dag": {"extract": {"static": True}}}
            assert captured["fe_inputs"] == payload
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_static_manifest_when_no_hook(self, tmp_path: Path) -> None:
        """No app.<snake>.core module → static manifest returned unchanged."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "no-hook"
        ep_dir.mkdir(parents=True)
        manifest_data = {"dag": {"extract": {"static": True}}}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=no-hook")
            assert response.status_code == 200
            assert response.json() == manifest_data
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_fe_inputs_defaults_to_empty_dict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the FE doesn't send `fe_inputs`, the hook gets an empty dict."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "default-form"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"k": "v"}))

        captured: dict[str, object] = {}

        def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            captured["fe_inputs"] = fe_inputs
            return manifest

        self._install_fake_core(monkeypatch, "default-form", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=default-form")
            assert response.status_code == 200
            assert captured["fe_inputs"] == {}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_invalid_fe_inputs_returns_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed fe_inputs JSON → 400 (not 500)."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "bad-form"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text("{}")

        self._install_fake_core(monkeypatch, "bad-form", lambda m, f: m)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get(
                "/workflows/v1/manifest",
                params={"entrypoint": "bad-form", "fe_inputs": "not-json{"},
            )
            assert response.status_code == 400
            assert "fe_inputs is not valid JSON" in response.json()["detail"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_legacy_alias_forwards_fe_inputs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Heracles' /manifest (no version) also routes through the hook."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "legacy-ep"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"orig": True}))

        def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {**manifest, "fe": fe_inputs}

        self._install_fake_core(monkeypatch, "legacy-ep", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get(
                "/manifest",
                params={"entrypoint": "legacy-ep", "fe_inputs": json.dumps({"x": 1})},
            )
            assert response.status_code == 200
            assert response.json() == {"orig": True, "fe": {"x": 1}}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_kebab_entrypoint_resolves_to_snake_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kebab `csa-hello-a` → import `app.csa_hello_a.core`."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "csa-hello-a"  # kebab on disk
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"static": True}))

        def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
            return {"computed": True}

        # Module is registered under app.csa_hello_a (snake) — the hook does the translation.
        self._install_fake_core(monkeypatch, "csa-hello-a", compute_manifest)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=csa-hello-a")
            assert response.status_code == 200
            assert response.json() == {"computed": True}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


class TestPerEntrypointHandlerHook:
    """Per-entrypoint handler discovery for /workflows/v1/{auth,check,metadata}.

    Multi-entrypoint apps drop ``app.<entrypoint_snake>.handler`` modules
    that expose plain async ``test_auth``, ``preflight_check``,
    ``fetch_metadata`` functions. Heracles sends the entrypoint identifier
    as the ``connector`` field (``{bundle_name}-{entrypoint.name}``); the
    SDK matches the suffix against known entrypoints (subdirs of
    ``CONTRACT_GENERATED_DIR``) and dispatches to the per-entrypoint module
    when present, falling through to the app-level ``Handler`` instance
    otherwise.

    Tests inject fake modules into ``sys.modules`` to simulate the
    discovery convention, and point ``CONTRACT_GENERATED_DIR`` at a
    ``tmp_path`` populated with empty manifest files so the connector
    suffix-match has known entrypoints to find.
    """

    @staticmethod
    def _install_fake_handler(
        monkeypatch: pytest.MonkeyPatch, entrypoint: str, **fns: object
    ) -> None:
        """Register ``app.<snake>.handler`` exposing the given async fns."""
        import sys
        import types

        from application_sdk.app.entrypoint import entrypoint_module_segment

        snake = entrypoint_module_segment(entrypoint)
        for parent in ("app", f"app.{snake}"):
            if parent not in sys.modules:
                pkg = types.ModuleType(parent)
                pkg.__path__ = []  # type: ignore[attr-defined]
                monkeypatch.setitem(sys.modules, parent, pkg)
        handler_mod = types.ModuleType(f"app.{snake}.handler")
        for name, fn in fns.items():
            setattr(handler_mod, name, fn)
        monkeypatch.setitem(sys.modules, f"app.{snake}.handler", handler_mod)

    # ------------------------------------------------------------------
    # /workflows/v1/auth
    # ------------------------------------------------------------------

    def test_auth_dispatches_to_entrypoint_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit ``entrypoint`` routes to the per-entrypoint test_auth by exact
        name — a direct lookup, no contract-dir glob or connector parsing."""
        captured: dict[str, object] = {}

        async def test_auth(input: AuthInput, ctx) -> AuthOutput:
            captured["entrypoint"] = input.entrypoint
            captured["app_name"] = ctx.app_name
            return AuthOutput(status=AuthStatus.FAILED, message="entrypoint says no")

        self._install_fake_handler(monkeypatch, "csa-hello-a", test_auth=test_auth)

        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "csa-hello-a", "credentials": []},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["data"]["status"] == "failed"
        assert body["data"]["message"] == "entrypoint says no"
        assert captured["entrypoint"] == "csa-hello-a"
        assert captured["app_name"] == "test-app"

    def test_auth_falls_back_when_no_entrypoint_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid ``entrypoint`` with no ``app.<segment>.handler`` module → the
        app-level Handler.test_auth runs (deterministic is/isn't fallback)."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "sdk-fallback-noep-auth", "credentials": []},
        )
        assert response.status_code == 200
        # _TestHandler returns SUCCESS with "auth ok"
        assert response.json()["data"]["message"] == "auth ok"

    def test_auth_sync_handler_fn_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A *sync* ``def test_auth`` in the entrypoint module is rejected by
        discovery (it would TypeError on ``await``) → app-level Handler runs."""

        def test_auth(input: AuthInput, ctx) -> AuthOutput:  # not async — invalid
            raise AssertionError("sync handler must not be dispatched")

        self._install_fake_handler(monkeypatch, "csa-hello-a", test_auth=test_auth)

        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "csa-hello-a", "credentials": []},
        )
        assert response.status_code == 200
        assert response.json()["data"]["message"] == "auth ok"

    def test_auth_falls_back_when_entrypoint_empty(self) -> None:
        """No ``entrypoint`` → app-level Handler.test_auth runs (single-entrypoint compat)."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 200
        assert response.json()["data"]["message"] == "auth ok"

    def test_auth_invalid_entrypoint_name_falls_back(self) -> None:
        """A malformed ``entrypoint`` (e.g. path-traversal attempt) fails the
        format check → no dispatch, app-level Handler runs."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "../evil", "credentials": []},
        )
        assert response.status_code == 200
        assert response.json()["data"]["message"] == "auth ok"

    def test_auth_connector_is_not_used_for_routing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The legacy ``connector`` field no longer drives dispatch. A connector
        naming a real entrypoint module is ignored when no ``entrypoint`` is sent
        — routing is explicit-only (the per-entrypoint fn must not run)."""

        async def test_auth(input: AuthInput, ctx) -> AuthOutput:
            raise AssertionError("connector must not trigger per-entrypoint dispatch")

        self._install_fake_handler(monkeypatch, "csa-hello-a", test_auth=test_auth)

        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"connector": "test-bundle-csa-hello-a", "credentials": []},
        )
        assert response.status_code == 200
        assert response.json()["data"]["message"] == "auth ok"

    # ------------------------------------------------------------------
    # /workflows/v1/check
    # ------------------------------------------------------------------

    def test_check_dispatches_to_entrypoint_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-entrypoint preflight_check is awaited; its checks render in response."""
        from application_sdk.handler.contracts import PreflightCheck

        async def preflight_check(input: PreflightInput, ctx) -> PreflightOutput:
            assert input.entrypoint == "csa-hello-b"
            assert ctx.app_name == "test-app"
            return PreflightOutput(
                status=PreflightStatus.NOT_READY,
                checks=[
                    PreflightCheck(
                        name="recipientCheck",
                        passed=False,
                        message="recipient missing",
                    )
                ],
            )

        self._install_fake_handler(
            monkeypatch, "csa-hello-b", preflight_check=preflight_check
        )

        client = _make_client()
        response = client.post(
            "/workflows/v1/check",
            json={"entrypoint": "csa-hello-b", "credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        # Envelope success reports that preflight ran (a check was produced), not
        # that every check passed — per DBBI-665. The failing check surfaces in
        # data.recipientCheck.
        assert body["success"] is True
        assert body["data"]["recipientCheck"] == {
            "success": False,
            "message": "recipient missing",
            "successMessage": "",
            "failureMessage": "recipient missing",
        }

    def test_check_falls_back_when_no_entrypoint_module(self) -> None:
        """A valid ``entrypoint`` with no handler module → app-level
        Handler.preflight_check runs."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/check",
            json={"entrypoint": "sdk-fallback-noep-check", "credentials": []},
        )
        assert response.status_code == 200
        # Fell back to the app-level Handler; the default test handler's preflight
        # returns status=ready, message "ready".
        assert response.json()["message"] == "ready"

    # ------------------------------------------------------------------
    # /workflows/v1/metadata
    # ------------------------------------------------------------------

    def test_metadata_dispatches_to_entrypoint_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-entrypoint fetch_metadata is awaited; its objects render in response."""

        async def fetch_metadata(input: MetadataInput, ctx) -> SqlMetadataOutput:
            assert input.entrypoint == "csa-hello-d"
            return SqlMetadataOutput(
                objects=[
                    SqlMetadataObject(TABLE_CATALOG="cat", TABLE_SCHEMA="sch1"),
                    SqlMetadataObject(TABLE_CATALOG="cat", TABLE_SCHEMA="sch2"),
                ]
            )

        self._install_fake_handler(
            monkeypatch, "csa-hello-d", fetch_metadata=fetch_metadata
        )

        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={"entrypoint": "csa-hello-d", "credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["data"]) == 2
        assert body["data"][0] == {"TABLE_CATALOG": "cat", "TABLE_SCHEMA": "sch1"}

    def test_metadata_falls_back_when_no_entrypoint_module(self) -> None:
        """A valid ``entrypoint`` with no handler module → app-level
        Handler.fetch_metadata runs."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={"entrypoint": "sdk-fallback-noep-meta", "credentials": []},
        )
        assert response.status_code == 200
        # _TestHandler returns empty SqlMetadataOutput
        assert response.json()["data"] == []

    def test_metadata_input_object_filter_from_metadata_template_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Heracles forwards the widget filter key as ``metadataTemplateKey`` (and
        a ``type`` mirror) but the v3 SDK contract uses ``object_filter``. Verify
        the bridge: per-entrypoint hooks read the widget key from
        ``input.object_filter``."""
        captured: dict[str, str] = {}

        async def fetch_metadata(input: MetadataInput, ctx) -> ApiMetadataOutput:
            captured["object_filter"] = input.object_filter
            return ApiMetadataOutput(objects=[])

        self._install_fake_handler(
            monkeypatch, "csa-meta-bridge", fetch_metadata=fetch_metadata
        )

        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={
                "entrypoint": "csa-meta-bridge",
                "credentials": [],
                "metadataTemplateKey": "tags",
            },
        )
        assert response.status_code == 200
        assert captured["object_filter"] == "tags"

    def test_metadata_input_object_filter_explicit_wins_over_template_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the body sets ``object_filter`` explicitly, the bridge does not
        overwrite it with ``metadataTemplateKey``."""
        captured: dict[str, str] = {}

        async def fetch_metadata(input: MetadataInput, ctx) -> ApiMetadataOutput:
            captured["object_filter"] = input.object_filter
            return ApiMetadataOutput(objects=[])

        self._install_fake_handler(
            monkeypatch, "csa-meta-bridge-2", fetch_metadata=fetch_metadata
        )

        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={
                "entrypoint": "csa-meta-bridge-2",
                "credentials": [],
                "object_filter": "public.*",
                "metadataTemplateKey": "tags",
            },
        )
        assert response.status_code == 200
        assert captured["object_filter"] == "public.*"

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def test_entrypoint_fn_raising_surfaces_as_500(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-entrypoint fn raising an unexpected exception → 500 (same as Handler.<fn>)."""

        async def test_auth(input: AuthInput, ctx) -> AuthOutput:
            raise RuntimeError("entrypoint blew up")

        self._install_fake_handler(monkeypatch, "csa-hello-a", test_auth=test_auth)

        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"entrypoint": "csa-hello-a", "credentials": []},
        )
        assert response.status_code == 500


class TestInputContractEndpoint:
    """Tests for GET /workflows/v1/input-contract."""

    def setup_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def _client(self, app_cls: type) -> TestClient:
        svc = create_app_handler_service(
            _TestHandler(), app_name="ic-test", app_class=app_cls
        )
        return TestClient(svc, raise_server_exceptions=False)

    def test_single_entrypoint_returns_model_json_schema(self) -> None:
        """Single-entry-point app: no ?entrypoint= resolves the only one and
        returns exactly AppInputContract.model_json_schema()."""
        from application_sdk.app.base import App

        class _SingleEpApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        resp = self._client(_SingleEpApp).get("/workflows/v1/input-contract")
        assert resp.status_code == 200
        assert resp.json() == _RoutingInput.model_json_schema()

    def test_multi_entrypoint_selected_by_query_param(self) -> None:
        """Multi-entry-point app: ?entrypoint=<name> returns that entry point's schema."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        resp = self._client(_MultiEpApp).get(
            "/workflows/v1/input-contract?entrypoint=extract"
        )
        assert resp.status_code == 200
        assert resp.json() == _RoutingInput.model_json_schema()

    def test_multi_entrypoint_no_param_uses_auto_default(self) -> None:
        """Multi-entry-point app without ?entrypoint= → 200 using auto-default (first alphabetically)."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpApp2(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        # 'extract' precedes 'load' alphabetically → auto-marked default
        resp = self._client(_MultiEpApp2).get("/workflows/v1/input-contract")
        assert resp.status_code == 200
        assert resp.json() == _RoutingInput.model_json_schema()

    def test_unknown_entrypoint_returns_404(self) -> None:
        from application_sdk.app.base import App

        class _SingleEpApp2(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        resp = self._client(_SingleEpApp2).get(
            "/workflows/v1/input-contract?entrypoint=nope"
        )
        assert resp.status_code == 404

    def test_invalid_entrypoint_name_returns_400(self) -> None:
        from application_sdk.app.base import App

        class _SingleEpApp3(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        resp = self._client(_SingleEpApp3).get(
            "/workflows/v1/input-contract?entrypoint=../etc"
        )
        assert resp.status_code == 400

    @staticmethod
    def _inject_generated_contract(ep_module: str, contract_cls: type):
        """Register a fake app/generated/{ep}/_input.py:AppInputContract in
        sys.modules so _published_input_contract can import it. Returns the list
        of injected module paths for cleanup."""
        import sys
        import types

        paths = ["app", "app.generated", f"app.generated.{ep_module}"]
        for p in paths:
            sys.modules.setdefault(p, types.ModuleType(p))
        leaf = f"app.generated.{ep_module}._input"
        mod = types.ModuleType(leaf)
        mod.AppInputContract = contract_cls
        sys.modules[leaf] = mod
        return paths + [leaf]

    def test_prefers_generated_app_input_contract(self) -> None:
        """When app/generated/{ep}/_input.py:AppInputContract is importable, the
        endpoint returns its (rich) schema, not the thin runtime input_type."""
        import sys

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _GenContract(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
            region: str = "region-us"

        class _MultiEpGen(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        injected = self._inject_generated_contract("extract", _GenContract)
        try:
            resp = self._client(_MultiEpGen).get(
                "/workflows/v1/input-contract?entrypoint=extract"
            )
            assert resp.status_code == 200
            # rich generated contract, not the thin _RoutingInput
            assert resp.json() == _GenContract.model_json_schema()
            assert "region" in resp.json()["properties"]
            # the entrypoint with no generated module still falls back
            resp_load = self._client(_MultiEpGen).get(
                "/workflows/v1/input-contract?entrypoint=load"
            )
            assert resp_load.json() == _RoutingInput.model_json_schema()
        finally:
            for p in injected:
                sys.modules.pop(p, None)

    def test_published_contract_falls_back_to_input_type(self) -> None:
        """No generated module → _published_input_contract returns input_type."""
        from application_sdk.app.entrypoint import EntryPointMetadata
        from application_sdk.handler.service import _published_input_contract

        ep = EntryPointMetadata(
            name="no-generated-here",
            input_type=_RoutingInput,
            output_type=_RoutingOutput,
            method_name="no_generated_here",
        )
        assert _published_input_contract(ep) is _RoutingInput


class TestDefaultEntrypoint:
    """Tests for default-entrypoint resolution (@entrypoint(default=True))."""

    def setup_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def test_resolver_rules(self) -> None:
        from application_sdk.app.entrypoint import (
            EntryPointMetadata,
            _resolve_default_entrypoint,
        )

        def _ep(name: str, *, default: bool = False) -> EntryPointMetadata:
            return EntryPointMetadata(
                name=name,
                input_type=_RoutingInput,
                output_type=_RoutingOutput,
                method_name=name,
                default=default,
            )

        a, b = _ep("a"), _ep("b")
        # single → that one
        assert _resolve_default_entrypoint({"a": a}) is a
        # multi, one default → the default
        bd = _ep("b", default=True)
        assert _resolve_default_entrypoint({"a": a, "b": bd}) is bd
        # multi, no default → None
        assert _resolve_default_entrypoint({"a": a, "b": b}) is None
        # multi, ambiguous (>1 default) → None
        ad = _ep("a", default=True)
        assert _resolve_default_entrypoint({"a": ad, "b": bd}) is None
        # empty → None
        assert _resolve_default_entrypoint({}) is None

    def test_multiple_defaults_raise_at_registration(self) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import EntryPointContractError, entrypoint

        with pytest.raises(EntryPointContractError, match="default entry point"):

            class _BadApp(App):
                @entrypoint(default=True)
                async def extract(self, input: _AlphaInput) -> _AlphaOutput:
                    return _AlphaOutput()

                @entrypoint(default=True)
                async def load(self, input: _BetaInput) -> _BetaOutput:
                    return _BetaOutput()

    def test_input_contract_resolves_marked_default(self) -> None:
        """Multi-entry-point app: no ?entrypoint= resolves the default-marked one."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _DefaultEpApp(App):
            @entrypoint
            async def extract(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

            @entrypoint(default=True)
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(), app_name="default-ic", app_class=_DefaultEpApp
        )
        resp = TestClient(svc, raise_server_exceptions=False).get(
            "/workflows/v1/input-contract"
        )
        assert resp.status_code == 200
        # The default ('load') uses _RoutingInput — confirm that schema came back.
        assert resp.json() == _RoutingInput.model_json_schema()

    def test_start_resolves_marked_default_without_param(self) -> None:
        """Multi-entry-point app: /start with no ?entrypoint= resolves the default."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _DefaultStartApp(App):
            @entrypoint
            async def extract(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

            @entrypoint(default=True)
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="default-start",
            app_class=_DefaultStartApp,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-1"
        mock_handle.result_run_id = "run-1"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        try:
            resp = TestClient(svc, raise_server_exceptions=False).post(
                "/workflows/v1/start", json={"name": "x"}
            )
            assert resp.status_code == 200
        finally:
            patcher.stop()
