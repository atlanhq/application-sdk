"""``FailureDetails`` → ``AppError``: the reverse lookup in ``errors.wire``.

On the far side of an activity boundary only the envelope survives. These pin
the rebuild that turns it back into a typed error, the leaf registry it keys
on, and the evidence degrade a producer falls back to when the envelope refuses
a verdict — all reachable without the SQL template (FND-4687).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from application_sdk.errors import wire
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AppTimeoutError,
    AuthError,
    InternalError,
    NotFoundError,
    SourceUnavailableError,
)
from application_sdk.errors.wire import (
    FailureDetails,
    degraded_failure_details,
    error_from_failure_details,
    redact_evidence_value,
    redacted_validation_reason,
    register_wire_leaf,
)


@dataclass(kw_only=True)
class _WidgetMissingError(NotFoundError):
    code: ClassVar[str] = "NOT_FOUND_TEST_WIDGET"
    widget: str | None = None


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An isolated copy of the leaf registry, restored after the test."""
    copy = dict(wire._WIRE_LEAVES)
    monkeypatch.setattr(wire, "_WIRE_LEAVES", copy)
    return copy


class TestRegistry:
    @pytest.mark.parametrize(
        "leaf", [AuthError, AppTimeoutError, SourceUnavailableError, InternalError]
    )
    def test_generic_leaves_rebuild_as_themselves(self, leaf: type) -> None:
        err = error_from_failure_details(leaf(message="m").to_failure_details())

        assert type(err) is leaf

    def test_sql_probe_timeout_registers_with_the_template(self) -> None:
        import application_sdk.templates.sql_app  # noqa: F401  # registers
        from application_sdk.templates.sql_app_errors import SqlProbeTimeoutError

        err = error_from_failure_details(
            SqlProbeTimeoutError(message="m").to_failure_details()
        )

        assert type(err) is SqlProbeTimeoutError

    @pytest.mark.usefixtures("registry")
    def test_registered_leaf_gets_its_type_and_declared_evidence_back(self) -> None:
        register_wire_leaf(_WidgetMissingError)
        details = _WidgetMissingError(message="gone", widget="w-1").to_failure_details()

        err = error_from_failure_details(details)

        assert type(err) is _WidgetMissingError
        assert err.widget == "w-1"
        assert err.message == "gone"

    def test_register_is_usable_as_a_decorator(self, registry: dict) -> None:
        @register_wire_leaf
        @dataclass(kw_only=True)
        class _Decorated(NotFoundError):
            code: ClassVar[str] = "NOT_FOUND_TEST_DECORATED"

        assert registry["NOT_FOUND_TEST_DECORATED"] is _Decorated

    @pytest.mark.usefixtures("registry")
    def test_re_registering_the_same_class_is_a_no_op(self) -> None:
        register_wire_leaf(AuthError)

    @pytest.mark.usefixtures("registry")
    def test_a_second_class_cannot_claim_a_registered_code(self) -> None:
        @dataclass(kw_only=True)
        class _OtherAuthError(AuthError):
            pass  # inherits code "AUTH"

        with pytest.raises(TypeError, match="AUTH"):
            register_wire_leaf(_OtherAuthError)

    def test_concurrent_claims_on_one_code_leave_exactly_one_winner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The conflict check and the write are one step: of many threads
        racing distinct classes onto one code, one wins and the rest raise.
        The registry's read is slowed so an unguarded check-then-write would
        let every thread see the code free."""

        class _SlowReads(dict):
            def get(self, key: Any, default: Any = None) -> Any:
                found = super().get(key, default)
                time.sleep(0.01)  # between the read and the caller's write
                return found

        monkeypatch.setattr(wire, "_WIRE_LEAVES", _SlowReads(wire._WIRE_LEAVES))
        leaves = [
            dataclass(kw_only=True)(
                type(
                    f"_Racer{i}",
                    (NotFoundError,),
                    {
                        "__annotations__": {"code": "ClassVar[str]"},
                        "code": "NOT_FOUND_RACE",
                    },
                )
            )
            for i in range(16)
        ]
        barrier = threading.Barrier(len(leaves))
        errors: list[BaseException] = []

        def _claim(leaf: type) -> None:
            barrier.wait()
            try:
                register_wire_leaf(leaf)
            except TypeError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_claim, args=(leaf,)) for leaf in leaves]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == len(leaves) - 1

    @pytest.mark.usefixtures("registry")
    def test_rejects_a_leaf_whose_constructor_cannot_take_the_base_fields(
        self,
    ) -> None:
        """An ``init=False`` leaf with its own ``__init__`` would otherwise
        register fine and raise ``TypeError`` on the far side of the wire."""
        from application_sdk.storage.errors import SdrStorageDiskFullError

        with pytest.raises(TypeError, match="retryable"):
            register_wire_leaf(SdrStorageDiskFullError)

    @pytest.mark.usefixtures("registry")
    def test_init_false_fields_are_not_passed_as_evidence(self) -> None:
        import dataclasses

        @dataclass(kw_only=True)
        class _Derived(NotFoundError):
            code: ClassVar[str] = "NOT_FOUND_TEST_DERIVED"
            derived: str = dataclasses.field(init=False, default="x")

        register_wire_leaf(_Derived)
        details = _Derived(message="m").to_failure_details()

        assert type(error_from_failure_details(details)) is _Derived

    def test_rebuild_does_not_need_the_sql_template(self) -> None:
        """Non-SQL apps reach the rebuild through ``errors`` alone."""
        code = (
            "import sys\n"
            "from application_sdk.errors import AuthError, error_from_failure_details\n"
            "err = error_from_failure_details(AuthError(message='m').to_failure_details())\n"
            "assert type(err) is AuthError\n"
            "assert 'application_sdk.templates.sql_app' not in sys.modules\n"
        )
        subprocess.run([sys.executable, "-c", code], check=True)


class TestUnknownCode:
    def _envelope(self, **overrides: Any) -> FailureDetails:
        fields: dict[str, Any] = {
            "category": FailureCategory.SOURCE_UNAVAILABLE,
            "code": "SOME_FUTURE_CODE",
            "retryable": True,
            "audience": Audience.USER,
            "message": "from a newer SDK",
            "evidence": {"field_no_leaf_has": 1},
            "cause_repr": "OSError: boom",
        }
        fields.update(overrides)
        return FailureDetails(**fields)

    def test_keeps_the_envelope_routing(self) -> None:
        err = error_from_failure_details(self._envelope())

        assert type(err) is wire._EnvelopeError
        assert err.category is FailureCategory.SOURCE_UNAVAILABLE
        assert err.audience is Audience.USER
        assert err.code == "SOME_FUTURE_CODE"
        assert err.effective_retryable is True

    def test_round_trips(self) -> None:
        details = self._envelope()

        assert error_from_failure_details(details).to_failure_details() == details

    def test_empty_code_round_trips_as_itself(self) -> None:
        details = self._envelope(code="")

        err = error_from_failure_details(details)

        assert err.code == ""
        assert err.to_failure_details().code == ""

    def test_masks_values_under_nested_secret_named_keys(self) -> None:
        """The denylist judges top-level keys only and string redaction cannot
        recognise an opaque credential, so a nested secret-named key is masked
        by name on the way through."""
        details = self._envelope(
            evidence={
                "connection": {"api_key": "opaque-value", "host": "db"},
                "attempts": [{"client_secret": "opaque-2"}],
            }
        )

        wire_json = (
            error_from_failure_details(details).to_failure_details().model_dump_json()
        )

        assert "opaque-value" not in wire_json
        assert "opaque-2" not in wire_json
        assert '"host":"db"' in wire_json

    def test_redacts_nested_evidence_values(self) -> None:
        details = self._envelope(
            evidence={"conn": {"dsn": "postgresql://u:hunter2@host/db"}}
        )

        err = error_from_failure_details(details)

        assert "hunter2" not in err.to_failure_details().model_dump_json()


class TestRebuildEvidence:
    def test_drops_keys_the_leaf_does_not_declare(self) -> None:
        details = (
            AuthError(message="m")
            .to_failure_details()
            .model_copy(update={"evidence": {"not_a_field": 1}})
        )

        assert type(error_from_failure_details(details)) is AuthError

    def test_drops_evidence_shadowing_a_base_field(self) -> None:
        """A ``message`` evidence key would otherwise splat beside the explicit
        argument and raise ``TypeError: got multiple values``."""
        details = (
            AuthError(message="real")
            .to_failure_details()
            .model_copy(update={"evidence": {"message": "shadow", "retryable": True}})
        )

        err = error_from_failure_details(details)

        assert err.message == "real"


class TestDegradedFailureDetails:
    def test_drops_secret_named_keys_and_keeps_routing(self) -> None:
        @dataclass(kw_only=True)
        class _AppAuthError(AuthError):
            api_key: str | None = None
            auth_method: str | None = "odbc"

        err = _AppAuthError(message="rejected", api_key="synthetic")
        with pytest.raises(ValidationError):
            err.to_failure_details()

        details = degraded_failure_details(err)

        assert details.category is FailureCategory.AUTH
        assert details.audience is Audience.USER
        assert details.code == AuthError.code
        assert "api_key" not in details.evidence
        assert details.evidence["auth_method"] == "odbc"
        assert "synthetic" not in details.model_dump_json()

    def test_reads_routing_off_a_reconstruction_leaf(self) -> None:
        rebuilt = error_from_failure_details(
            FailureDetails(
                category=FailureCategory.SOURCE_UNAVAILABLE,
                code="SOME_FUTURE_CODE",
                retryable=False,
                audience=Audience.USER,
                message="m",
                evidence={"kept": 1},
            )
        )

        details = degraded_failure_details(rebuilt)

        assert details.code == "SOME_FUTURE_CODE"
        assert details.audience is Audience.USER
        assert details.evidence == {"kept": 1}

    def test_raises_when_even_the_evidence_free_envelope_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = FailureDetails

        def _refuse(**kwargs: Any) -> FailureDetails:
            return real.model_validate({**kwargs, "unexpected": 1})

        monkeypatch.setattr(wire, "FailureDetails", _refuse)

        with pytest.raises(ValidationError):
            degraded_failure_details(AuthError(message="m"))


def test_redact_evidence_value_keeps_shape_and_masks_by_name() -> None:
    value = {"conn": {"db_password": "opaque", "port": 5432}, "tags": ("a",)}

    assert redact_evidence_value(value) == {
        "conn": {"db_password": "***", "port": 5432},
        "tags": ("a",),
    }


def test_redacted_validation_reason_omits_the_rejected_input() -> None:
    with pytest.raises(ValidationError) as caught:
        FailureDetails(
            category=FailureCategory.AUTH,
            code="AUTH",
            retryable=False,
            message="m",
            evidence={"api_key": "synthetic-secret-value"},
        )

    reason = redacted_validation_reason(caught.value)

    assert "api_key" in reason
    assert "synthetic-secret-value" not in reason
