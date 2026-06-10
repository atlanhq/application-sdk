"""Tests for AppError dataclass and FailureDetails wire envelope."""

import pytest
from pydantic import TypeAdapter

from application_sdk.errors.base import _BASE_FIELDS, AppError
from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import AuthError, InternalError
from application_sdk.errors.wire import FailureDetails


def test_construction_kw_only() -> None:
    e = AuthError(message="bad creds")
    assert e.message == "bad creds"
    assert str(e) == "bad creds"


def test_construction_rejects_positional() -> None:
    with pytest.raises(TypeError):
        AuthError("bad creds")  # type: ignore[call-arg]


def test_effective_retryable_uses_class_default_when_none() -> None:
    e = AuthError(message="x")
    assert e.retryable is None
    assert e.effective_retryable is False  # AuthError.default_retryable = False


def test_effective_retryable_per_instance_overrides() -> None:
    e = AuthError(message="x", retryable=True)
    assert e.effective_retryable is True


def test_cause_kwarg_sets_dunder_cause() -> None:
    root = ValueError("root")
    e = AuthError(message="wrapper", cause=root)
    assert e.__cause__ is root
    assert e.cause is root


def test_raise_from_also_sets_cause() -> None:
    root = ValueError("root")
    try:
        try:
            raise root
        except ValueError as exc:
            raise AuthError(message="wrapper") from exc
    except AuthError as caught:
        assert caught.__cause__ is root


def test_add_note_works() -> None:
    e = InternalError(message="boom")
    e.add_note("context note")
    assert "context note" in e.__notes__  # type: ignore[attr-defined]


def test_str_equals_message() -> None:
    e = AuthError(message="hello")
    assert str(e) == "hello"


def test_qualified_code() -> None:
    e = AuthError(message="x")
    assert e.qualified_code == "AUTH.AUTH"


def test_to_failure_details_returns_pydantic_model() -> None:
    from application_sdk.errors.categories import Audience

    e = AuthError(message="bad creds", auth_method="basic", principal="user")
    fd = e.to_failure_details()
    assert isinstance(fd, FailureDetails)
    assert fd.category is FailureCategory.AUTH
    assert fd.code == "AUTH"
    assert fd.retryable is False
    assert fd.audience is Audience.USER
    assert fd.message == "bad creds"
    assert fd.suggested_action is None
    assert fd.evidence["auth_method"] == "basic"
    assert fd.evidence["principal"] == "user"


def test_to_failure_details_excludes_base_fields() -> None:
    e = AuthError(message="x", app_name="myapp", run_id="run-1")
    fd = e.to_failure_details()
    assert "message" not in fd.evidence
    assert "app_name" not in fd.evidence
    assert "run_id" not in fd.evidence
    assert fd.app_name == "myapp"
    assert fd.run_id == "run-1"


def test_to_failure_details_cause_repr() -> None:
    root = ValueError("root cause")
    e = AuthError(message="x", cause=root)
    fd = e.to_failure_details()
    assert fd.cause_repr is not None
    assert "ValueError" in fd.cause_repr


def test_cause_repr_redacts_url_credentials() -> None:
    cause = ValueError("GET https://admin:s3cr3t@host.example.com/path failed")
    e = AuthError(message="x", cause=cause)
    fd = e.to_failure_details()
    assert fd.cause_repr is not None
    assert "s3cr3t" not in fd.cause_repr
    assert "admin:s3cr3t" not in fd.cause_repr
    assert "***@host.example.com" in fd.cause_repr


def test_cause_repr_redacts_query_param_secrets() -> None:
    cause = ValueError("GET https://api.example.com/data?api_key=sk-secret123&limit=10")
    e = AuthError(message="x", cause=cause)
    fd = e.to_failure_details()
    assert fd.cause_repr is not None
    assert "sk-secret123" not in fd.cause_repr
    assert "api_key=***" in fd.cause_repr
    assert "limit=10" in fd.cause_repr


def test_cause_repr_truncates_long_messages() -> None:
    cause = ValueError("x" * 600)
    e = AuthError(message="x", cause=cause)
    fd = e.to_failure_details()
    assert fd.cause_repr is not None
    assert len(fd.cause_repr) <= len("ValueError: ") + 500 + len("…")


def test_failure_details_json_round_trip() -> None:
    e = AuthError(message="bad creds", auth_method="basic")
    fd = e.to_failure_details()
    json_str = fd.model_dump_json()
    ta = TypeAdapter(FailureDetails)
    fd2 = ta.validate_json(json_str)
    assert fd2.category is FailureCategory.AUTH
    assert fd2.message == "bad creds"
    assert fd2.evidence["auth_method"] == "basic"


def test_isinstance_hierarchy() -> None:
    e = AuthError(message="x")
    assert isinstance(e, AppError)
    assert isinstance(e, Exception)


def test_failure_details_suggested_action_none_by_default() -> None:
    fd = AuthError(message="x").to_failure_details()
    assert fd.suggested_action is None


def test_failure_details_evidence_denylist_rejects_secret_keys() -> None:
    from pydantic import ValidationError

    from application_sdk.errors.categories import Audience, FailureCategory
    from application_sdk.errors.wire import FailureDetails

    with pytest.raises(ValidationError, match="secret-named"):
        FailureDetails(
            category=FailureCategory.AUTH,
            code="AUTH",
            retryable=False,
            audience=Audience.USER,
            message="x",
            evidence={"token": "bearer abc123"},
        )


def test_base_fields_sentinel() -> None:
    assert "message" in _BASE_FIELDS
    assert "retryable" in _BASE_FIELDS
    assert "cause" in _BASE_FIELDS
    assert "app_name" in _BASE_FIELDS
    assert "run_id" in _BASE_FIELDS


def test_sanitize_cause_repr_redacts_userinfo_for_any_url_scheme() -> None:
    from application_sdk.errors.base import _sanitize_cause_repr

    cases = {
        "postgresql+psycopg://user:s3cret@db.internal:5432/prod": "postgresql+psycopg://***@db.internal:5432/prod",
        "mysql://root:hunter2@10.0.0.5/app": "mysql://***@10.0.0.5/app",
        "https://alice:tok3n@api.example.com/v1": "https://***@api.example.com/v1",
        "snowflake://svc:pw@acct.snowflakecomputing.com": "snowflake://***@acct.snowflakecomputing.com",
    }
    for raw, expected in cases.items():
        out = _sanitize_cause_repr(Exception(f"connect failed for {raw}"))
        assert expected in out, out
        assert "s3cret" not in out and "hunter2" not in out
        assert "tok3n" not in out and ":pw@" not in out


def test_sanitize_cause_repr_still_redacts_secret_params() -> None:
    from application_sdk.errors.base import _sanitize_cause_repr

    out = _sanitize_cause_repr(Exception("call failed: api_key=abc123&x=1"))
    assert "api_key=***" in out
    assert "abc123" not in out
