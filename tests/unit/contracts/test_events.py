"""Tests for the lifecycle event data contracts in ``contracts/events.py``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from application_sdk.contracts.events import WorkerTokenRefreshEventData

_WIRE = {
    "application_name": "example-app",
    "deployment_name": "example-deployment",
    "force_refresh": "true",
    "token_expiry_time": "1781519400.0",
    "time_until_expiry": "90.0",
    "refresh_timestamp": "1781519310.0",
    "app_version": "1.2.3",
    "commit_sha": "abc1234",
}


class TestWorkerTokenRefreshEventData:
    def test_wire_payload_round_trips_unchanged(self) -> None:
        """The string-valued token_refresh payload validates and dumps back as-is."""
        assert WorkerTokenRefreshEventData.model_validate(_WIRE).model_dump() == _WIRE

    @pytest.mark.parametrize(
        "field",
        [
            "force_refresh",
            "token_expiry_time",
            "time_until_expiry",
            "refresh_timestamp",
        ],
    )
    def test_flag_and_timing_fields_are_strings(self, field: str) -> None:
        """These four fields are declared ``str``, matching what the wire carries."""
        assert WorkerTokenRefreshEventData.model_fields[field].annotation is str

    def test_rejects_a_typed_bool_flag(self) -> None:
        """A ``bool`` is not silently stringified into the string-typed flag."""
        with pytest.raises(ValidationError):
            WorkerTokenRefreshEventData.model_validate({**_WIRE, "force_refresh": True})

    def test_image_identity_defaults_to_empty(self) -> None:
        """app_version / commit_sha default to empty on images without build info."""
        data = {
            k: v for k, v in _WIRE.items() if k not in ("app_version", "commit_sha")
        }
        model = WorkerTokenRefreshEventData.model_validate(data)
        assert model.app_version == ""
        assert model.commit_sha == ""


class TestV2EventRegistrationDeprecations:
    """The v2 event-registration models warn; nothing in the SDK reads them."""

    def test_event_filter_warns(self) -> None:
        from application_sdk.contracts.events import EventFilter

        with pytest.warns(DeprecationWarning, match="EventFilterRule"):
            EventFilter(path="event.data.type", operator="==", value="x")

    def test_consumes_warns(self) -> None:
        from application_sdk.contracts.events import Consumes

        with pytest.warns(DeprecationWarning, match="EventTriggerConfig"):
            Consumes(eventId="i", eventType="t", eventName="n", version="1")

    def test_event_registration_warns(self) -> None:
        from application_sdk.contracts.events import EventRegistration

        with pytest.warns(DeprecationWarning, match="removed in v4.0"):
            EventRegistration()

    def test_importing_the_module_does_not_warn(self, recwarn) -> None:
        import importlib

        import application_sdk.contracts.events as events

        importlib.reload(events)
        assert not [w for w in recwarn if w.category is DeprecationWarning]
