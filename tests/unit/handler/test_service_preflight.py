"""Handler service tests: the preflight endpoint."""

from __future__ import annotations

from unittest.mock import patch

from application_sdk.errors.leaves import AuthError
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.handler.service import (
    _CATEGORY_TO_HTTP,
)
from tests.unit.handler._service_support import (
    _ConfigCapture,
    _FailingHandler,
    _make_client,
    _TestHandler,
)


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
        assert body["preflight"]["status"] == "ready"
        assert "should_block" not in body["preflight"]
        assert body["preflight"]["checks"] == []

    def test_preflight_emits_structured_outcome_row(self) -> None:
        # FND-901: the HTTP surface emits the shared "Preflight check outcome"
        # row (INFO — the caller is watching the screen) instead of prose.
        client = _make_client()
        with patch("application_sdk.handler.service.logger") as ml:
            response = client.post("/workflows/v1/check", json={"credentials": []})
        assert response.status_code == 200
        event = next(
            c.kwargs
            for c in ml.info.call_args_list
            if c.args and c.args[0] == "Preflight check outcome"
        )
        assert event["preflight_surface"] == "http"
        assert event["outcome"] == "ready"
        assert event["request_id"]
        assert event["check_matrix"] == "[]"
        ml.error.assert_not_called()

    def test_default_handler_reports_success_with_no_checks(self) -> None:
        client = _make_client(handler=DefaultHandler())
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )

        assert response.status_code == 200
        body = response.json()
        # No checks emitted, so the SageV2 envelope is success=False (same as
        # any zero-check handler).
        assert body["success"] is False
        assert body["data"] == {}
        assert body["message"] == "No preflight handler registered"
        assert body["preflight"]["status"] == "ready"
        assert "should_block" not in body["preflight"]
        assert body["preflight"]["checks"] == []

    def test_preflight_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )
        assert response.status_code == 500

    def test_unmeasured_duration_is_omitted_from_the_response(self) -> None:
        # The -1.0 sentinel belongs to the telemetry row; the display payload
        # shows no duration rather than a negative one.
        from application_sdk.handler.contracts import PreflightCheck
        from application_sdk.handler.service import _summarize_check

        unmeasured = _summarize_check(PreflightCheck(name="auth", passed=True))
        assert "duration_ms" not in unmeasured
        measured = _summarize_check(
            PreflightCheck(name="auth", passed=True, duration_ms=50.0)
        )
        assert measured["duration_ms"] == 50.0

    def test_wrong_password_is_not_a_crash(self) -> None:
        # AuthError -> 401: the response working as designed, the single most
        # common preflight failure. It must never enter the crash series.
        from application_sdk.errors.leaves import AuthError

        class _WrongPasswordHandler(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise AuthError(message="wrong password")

        client = _make_client(handler=_WrongPasswordHandler())
        with patch("application_sdk.handler.service.logger") as ml:
            response = client.post("/workflows/v1/check", json={"credentials": []})
        assert response.status_code == 401
        crashed = [
            c
            for c in ml.error.call_args_list
            if c.args and c.args[0] == "Preflight check outcome"
        ]
        assert crashed == []
        counted = [
            c.kwargs
            for c in ml.info.call_args_list
            if c.args and c.args[0] == "Preflight check outcome"
        ]
        assert len(counted) == 1
        assert counted[0]["outcome"] == "client_fault"

    def test_client_fault_set_matches_the_http_mapping(self) -> None:
        # The crash-row guard and the HTTP status mapping encode the same
        # judgement ("is this the client's input, or our failure?"). Pin them
        # together so neither can drift without this failing.
        from application_sdk.errors.categories import FailureCategory
        from application_sdk.execution._temporal.preflight_gate import (
            _CLIENT_FAULT_CATEGORIES,
        )

        for category in FailureCategory:
            maps_to_4xx = _CATEGORY_TO_HTTP.get(category, 500) < 500
            assert (category in _CLIENT_FAULT_CATEGORIES) == maps_to_4xx, category

    def test_preflight_handler_crash_emits_outcome_row(self) -> None:
        client = _make_client(handler=_FailingHandler())
        with patch("application_sdk.handler.service.logger") as ml:
            response = client.post("/workflows/v1/check", json={"credentials": []})
        assert response.status_code == 500
        rows = [
            c
            for c in ml.error.call_args_list
            if c.args and c.args[0] == "Preflight check outcome"
        ]
        assert len(rows) == 1
        kwargs = rows[0].kwargs
        assert kwargs["outcome"] == "crashed"
        assert kwargs["preflight_surface"] == "http"

    def test_untyped_crash_is_not_ready_and_classified_internal(self) -> None:
        # The two facts live on different fields: the verdict on the output,
        # the classification on the failed check. A caller reads both.
        class _Crashing(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise RuntimeError("password=hunter2 leaked in a driver message")

        client = _make_client(handler=_Crashing())
        response = client.post("/workflows/v1/check", json={"credentials": []})
        body = response.json()
        assert response.status_code == 500
        assert body["detail"] == "Internal server error"
        assert body["success"] is False
        assert body["preflight"]["status"] == "not_ready"
        (check,) = body["preflight"]["checks"]
        assert check["name"] == "preflightVerdict"
        assert check["passed"] is False
        assert check["error"]["category"] == "INTERNAL"
        assert check["error"]["audience"] == "APP_OWNER"
        assert check["error"]["evidence"]["classification_pending"] is True
        assert body["error"]["category"] == "INTERNAL"
        assert body["data"]["preflightVerdict"]["success"] is False
        assert body["data"]["preflightVerdict"]["failureMessage"]
        assert "hunter2" not in response.text

    def test_crash_body_never_carries_the_exception_text(self) -> None:
        # Secret redaction is not enough: a driver message names the caller's
        # own hosts and accounts, and those must not reach the HTTP caller
        # either. The cause stays in the server-side log only.
        class _Crashing(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise ConnectionError(
                    "could not connect to db-prod-7.internal.example.net:5432 "
                    "database=finance_warehouse user=svc_ro"
                )

        client = _make_client(handler=_Crashing())
        response = client.post("/workflows/v1/check", json={"credentials": []})
        body = response.json()
        assert response.status_code == 500
        assert "cause_repr" not in response.text
        assert "db-prod-7" not in response.text
        assert "finance_warehouse" not in response.text
        assert body["error"]["code"]
        (check,) = body["preflight"]["checks"]
        assert "cause_repr" not in check["error"]

    def test_typed_raise_keeps_its_leaf_and_http_status(self) -> None:
        from application_sdk.errors.leaves import AuthError

        class _Rejected(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise AuthError(
                    message="wrong password", suggested_action="Rotate the credential."
                )

        client = _make_client(handler=_Rejected())
        response = client.post("/workflows/v1/check", json={"credentials": []})
        body = response.json()
        assert response.status_code == 401
        assert body["detail"] == "wrong password"
        assert body["preflight"]["status"] == "not_ready"
        (check,) = body["preflight"]["checks"]
        assert check["error"]["category"] == "AUTH"
        assert check["error"]["audience"] == "USER"
        assert check["suggested_action"] == "Rotate the credential."
        assert body["data"]["preflightVerdict"]["failureMessage"] == "wrong password"

    def test_source_unavailable_raise_maps_to_503_with_the_verdict(self) -> None:
        from application_sdk.errors.leaves import SourceUnavailableError

        class _Unreachable(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise SourceUnavailableError(message="The source did not answer")

        client = _make_client(handler=_Unreachable())
        response = client.post("/workflows/v1/check", json={"credentials": []})
        body = response.json()
        assert response.status_code == 503
        assert body["preflight"]["status"] == "not_ready"
        assert (
            body["preflight"]["checks"][0]["error"]["category"] == "SOURCE_UNAVAILABLE"
        )

    def test_returned_verdicts_check_error_drops_the_exception_text(self) -> None:
        # A handler that returns NOT_READY with a typed error built from a driver
        # exception carries that exception's text as cause_repr. The Temporal
        # payload keeps it; the HTTP caller must not see it.
        from application_sdk.errors.leaves import AuthError

        class _Rejected(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                cause = RuntimeError(
                    "login failed for user svc_ro at db-prod-7.internal"
                )
                return PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    checks=[
                        PreflightCheck(
                            name="connectivity",
                            passed=False,
                            error=AuthError(
                                message="Rejected", cause=cause
                            ).to_failure_details(),
                        )
                    ],
                )

        client = _make_client(handler=_Rejected())
        response = client.post("/workflows/v1/check", json={"credentials": []})
        assert response.status_code == 200
        assert "cause_repr" not in response.text
        assert "db-prod-7" not in response.text
        (check,) = response.json()["preflight"]["checks"]
        assert check["error"]["category"] == "AUTH"

    def test_deprecated_handler_error_carries_the_verdict_too(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post("/workflows/v1/check", json={"credentials": []})
        body = response.json()
        assert response.status_code == 500
        assert body["detail"] == "preflight failed"
        assert body["preflight"]["status"] == "not_ready"
        assert body["preflight"]["checks"][0]["error"]["audience"] == "APP_OWNER"

    def test_request_contract_error_stays_a_plain_4xx(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/check?entrypoint=no-such-entrypoint",
            json={"credentials": [], "entrypoint": "bad name!"},
        )
        assert response.status_code == 400
        assert "preflight" not in response.json()

    def test_a_5xx_httpexception_still_emits_a_crash_row(self) -> None:
        # An HTTPException is not proof of a deliberate client-facing response.
        # A 5xx raised inside the body reaches none of the other boundary
        # handlers, so without a row it leaves the funnel's denominator — the
        # defect gap 3 closed for handler raises, on the same surface.
        from fastapi import HTTPException

        class _ServiceUnavailableHandler(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise HTTPException(status_code=503, detail="upstream is down")

        client = _make_client(handler=_ServiceUnavailableHandler())
        with patch("application_sdk.handler.service.logger") as ml:
            response = client.post("/workflows/v1/check", json={"credentials": []})
        assert response.status_code == 503
        rows = [
            c.kwargs
            for c in ml.error.call_args_list
            if c.args and c.args[0] == "Preflight check outcome"
        ]
        assert len(rows) == 1
        assert rows[0]["outcome"] == "crashed"
        assert rows[0]["preflight_surface"] == "http"

    def test_a_4xx_httpexception_emits_no_row(self) -> None:
        # The other half: a deliberate client-facing status stays unrecorded,
        # because the response itself is the channel.
        from fastapi import HTTPException

        class _ConflictHandler(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                raise HTTPException(status_code=409, detail="already connected")

        client = _make_client(handler=_ConflictHandler())
        with patch("application_sdk.handler.service.logger") as ml:
            response = client.post("/workflows/v1/check", json={"credentials": []})
        assert response.status_code == 409
        for level in ("info", "warning", "error"):
            for c in getattr(ml, level).call_args_list:
                if c.args:
                    assert c.args[0] != "Preflight check outcome"

    def test_crash_row_names_the_requested_entrypoint(self) -> None:
        # The raise must land BEFORE _validated_entrypoint reassigns
        # `entrypoint`, because that window is the only one the seed governs.
        client = _make_client(handler=_FailingHandler())
        with (
            patch(
                "application_sdk.handler.service._validated_entrypoint",
                side_effect=RuntimeError("boom"),
            ),
            patch("application_sdk.handler.service.logger") as ml,
        ):
            response = client.post(
                "/workflows/v1/check",
                json={"credentials": [], "entrypoint": "alpha"},
            )
        assert response.status_code == 500
        rows = [
            c.kwargs
            for c in ml.error.call_args_list
            if c.args and c.args[0] == "Preflight check outcome"
        ]
        assert len(rows) == 1
        assert rows[0]["entrypoint"] == "alpha"

    def test_malformed_entrypoint_400_emits_no_crash_row(self) -> None:
        client = _make_client()
        with patch("application_sdk.handler.service.logger") as ml:
            response = client.post(
                "/workflows/v1/check",
                json={"credentials": [], "entrypoint": "Bad Name!"},
            )
        assert response.status_code == 400
        for level in ("info", "warning", "error"):
            for c in getattr(ml, level).call_args_list:
                if c.args:
                    assert c.args[0] != "Preflight check outcome"

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

    def test_preflight_metadata_only_mirrors_to_connection_config(self) -> None:
        handler = _ConfigCapture()
        client = _make_client(handler=handler)
        response = client.post(
            "/workflows/v1/check",
            json={
                "credentials": [],
                "metadata": {"warehouse": "COMPUTE_WH"},
            },
        )

        assert response.status_code == 200
        assert handler.received == [
            {
                "metadata": {"warehouse": "COMPUTE_WH"},
                "connection_config": {"warehouse": "COMPUTE_WH"},
            }
        ]

    def test_preflight_connection_config_only_mirrors_to_metadata(self) -> None:
        handler = _ConfigCapture()
        client = _make_client(handler=handler)
        response = client.post(
            "/workflows/v1/check",
            json={
                "credentials": [],
                "connection_config": {"warehouse": "COMPUTE_WH"},
            },
        )

        assert response.status_code == 200
        assert handler.received == [
            {
                "metadata": {"warehouse": "COMPUTE_WH"},
                "connection_config": {"warehouse": "COMPUTE_WH"},
            }
        ]

    def test_preflight_connection_config_and_metadata_are_preserved(self) -> None:
        handler = _ConfigCapture()
        client = _make_client(handler=handler)
        response = client.post(
            "/workflows/v1/check",
            json={
                "credentials": [],
                "metadata": {"legacy": "kept"},
                "connection_config": {"canonical": "kept"},
            },
        )

        assert response.status_code == 200
        assert handler.received == [
            {
                "metadata": {"legacy": "kept"},
                "connection_config": {"canonical": "kept"},
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
        assert body["preflight"]["status"] == "not_ready"
        assert "should_block" not in body["preflight"]
        assert "status" not in body["data"]

    def test_a_failed_checks_typed_error_outranks_its_message(self) -> None:
        """On a returned verdict the typed error is the check's message in both
        the legacy ``data`` map and the typed body; the legacy text never leaks."""

        class _OneCheck(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    checks=[
                        PreflightCheck(
                            name="auth",
                            passed=False,
                            message="legacy text",
                            error=AuthError(
                                message="typed text", suggested_action="Rotate it."
                            ),
                        )
                    ],
                )

        client = _make_client(handler=_OneCheck())
        response = client.post("/workflows/v1/check", json={"credentials": []})
        body = response.json()
        assert body["data"]["auth"]["message"] == "typed text"
        assert body["data"]["auth"]["failureMessage"] == "typed text"
        (check,) = body["preflight"]["checks"]
        assert check["message"] == "typed text"
        assert check["suggested_action"] == "Rotate it."
        assert check["error"]["category"] == "AUTH"
        assert "legacy text" not in response.text

    def test_preflight_not_ready_status_surfaced(self) -> None:
        # Block-ness is derivable from status == not_ready — there is no
        # per-check blocking flag or should_block signal anymore.
        class _OneCheck(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    message="Credentials are invalid",
                    checks=[
                        PreflightCheck(
                            name="loginCheck",
                            passed=False,
                            message="Credentials are invalid",
                        )
                    ],
                )

        client = _make_client(handler=_OneCheck())
        body = client.post("/workflows/v1/check", json={"credentials": []}).json()

        assert body["success"] is True
        assert list(body["data"]) == ["loginCheck"]
        assert body["data"]["loginCheck"]["success"] is False
        assert "should_block" not in body["preflight"]
        assert body["preflight"]["status"] == "not_ready"
        assert body["preflight"]["checks"][0]["name"] == "loginCheck"
        assert "status" not in body["data"]
        assert "checks" not in body["data"]

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
        assert body["preflight"]["status"] == "not_ready"
        assert "should_block" not in body["preflight"]

    def test_flattening_and_summary_prefer_error_over_message(self) -> None:
        # Precedence: when a check carries a typed error, its message and
        # suggested_action win over the deprecated check.message.
        class _OneCheck(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.errors.leaves import AuthError
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    checks=[
                        PreflightCheck(
                            name="loginCheck",
                            passed=False,
                            message="stale deprecated text",
                            error=AuthError(
                                message="Credentials are invalid",
                                suggested_action="Rotate the credential",
                            ),
                        )
                    ],
                )

        body = (
            _make_client(handler=_OneCheck())
            .post("/workflows/v1/check", json={"credentials": []})
            .json()
        )
        entry = body["data"]["loginCheck"]
        assert entry["failureMessage"] == "Credentials are invalid"
        assert entry["message"] == "Credentials are invalid"
        summary_check = body["preflight"]["checks"][0]
        assert summary_check["message"] == "Credentials are invalid"
        assert summary_check["suggested_action"] == "Rotate the credential"

    def test_flattening_uses_check_message_when_no_error(self) -> None:
        class _OneCheck(_TestHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                from application_sdk.handler.contracts import PreflightCheck

                return PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    checks=[
                        PreflightCheck(
                            name="loginCheck",
                            passed=False,
                            message="Credentials are invalid",
                        )
                    ],
                )

        body = (
            _make_client(handler=_OneCheck())
            .post("/workflows/v1/check", json={"credentials": []})
            .json()
        )
        assert body["data"]["loginCheck"]["failureMessage"] == "Credentials are invalid"
        assert body["preflight"]["checks"][0]["message"] == "Credentials are invalid"
        assert "suggested_action" not in body["preflight"]["checks"][0]
