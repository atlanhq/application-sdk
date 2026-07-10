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
    from application_sdk.errors import sanitize_cause_repr

    # Hostnames are preserved after userinfo redaction; an IPv4 host is
    # additionally masked by the identity redactor (BLDX-1538 policy).
    cases = {
        "postgresql+psycopg://user:s3cret@db.internal:5432/prod": "postgresql+psycopg://***@db.internal:5432/prod",
        "mysql://root:hunter2@10.0.0.5/app": "mysql://***@***/app",
        "https://alice:tok3n@api.example.com/v1": "https://***@api.example.com/v1",
        "snowflake://svc:pw@acct.snowflakecomputing.com": "snowflake://***@acct.snowflakecomputing.com",
    }
    for raw, expected in cases.items():
        out = sanitize_cause_repr(Exception(f"connect failed for {raw}"))
        assert expected in out, out
        assert "s3cret" not in out and "hunter2" not in out
        assert "tok3n" not in out and ":pw@" not in out


def test_sanitize_cause_repr_still_redacts_secret_params() -> None:
    from application_sdk.errors import sanitize_cause_repr

    out = sanitize_cause_repr(Exception("call failed: api_key=abc123&x=1"))
    assert "api_key=***" in out
    assert "abc123" not in out


def test_redact_secrets_userinfo_and_params() -> None:
    """redact_secrets() on plain strings: URL userinfo + known secret params."""
    from application_sdk.errors import redact_secrets

    assert (
        redact_secrets("postgresql://user:s3cret@db.internal/prod")
        == "postgresql://***@db.internal/prod"
    )
    out = redact_secrets("boom api_key=abc123&password=hunter2")
    assert "api_key=***" in out and "password=***" in out
    assert "abc123" not in out and "hunter2" not in out


def test_redact_secrets_consumes_at_in_password() -> None:
    """A raw `@` inside the password must not leave the tail exposed."""
    from application_sdk.errors import redact_secrets

    out = redact_secrets("connect failed for postgresql://u:p@ssw0rd@host:5432/db")
    assert "p@ssw0rd" not in out
    assert "ssw0rd" not in out
    assert out == "connect failed for postgresql://***@host:5432/db"


def test_redact_secrets_over_redacts_trailing_at_in_no_space_run() -> None:
    """Deliberate: the greedy userinfo match consumes to the last `@` in a
    whitespace-free run, so a trailing `@` after the host over-redacts. This
    is the safe failure direction for a secret redactor — pinned so the
    behavior is understood as intentional, not a regression."""
    from application_sdk.errors import redact_secrets

    # The `@b` later in the same no-space run is swallowed up to the last `@`.
    assert redact_secrets("postgresql://u:p@host/db?to=a@b") == "postgresql://***@b"
    # A whitespace boundary protects the common "URL then prose" case.
    out = redact_secrets("postgresql://u:p@host/db connected as a@b")
    assert out == "postgresql://***@host/db connected as a@b"


def test_redact_secrets_masks_mysql_identity_keeps_username() -> None:
    """MySQL auth-failure prose: mask the client IP, keep the username."""
    from application_sdk.errors import redact_secrets

    out = redact_secrets(
        "Access denied for user 'atlan'@'49.43.224.205' (using password: YES)"
    )
    assert out == "Access denied for user 'atlan'@'***' (using password: YES)"
    assert "49.43.224.205" not in out
    assert "'atlan'" in out  # username is the actionable diagnostic — preserved


def test_redact_secrets_masks_mysql_identity_hostname() -> None:
    """The identity host may be a hostname, not an IP — mask it, keep user."""
    from application_sdk.errors import redact_secrets

    out = redact_secrets("Access denied for user 'svc'@'worker-3.internal'")
    assert out == "Access denied for user 'svc'@'***'"
    assert "worker-3.internal" not in out
    assert "'svc'" in out


def test_redact_secrets_masks_postgres_host_prose() -> None:
    """Postgres surfaces the host outside any URL — via host/server keywords."""
    from application_sdk.errors import redact_secrets

    assert (
        redact_secrets('could not connect to server at "10.0.0.5" port 5432')
        == 'could not connect to server at "***" port 5432'
    )
    assert (
        redact_secrets('could not translate host name "db.internal" to address')
        == 'could not translate host name "***" to address'
    )
    assert redact_secrets('connection to host "192.168.1.1" failed') == (
        'connection to host "***" failed'
    )


def test_redact_secrets_host_keyword_is_word_anchored() -> None:
    """The host/server keyword must not match a 'host' tail inside another word."""
    from application_sdk.errors import redact_secrets

    assert (
        redact_secrets('the ghost "spooky" appeared') == 'the ghost "spooky" appeared'
    )
    assert redact_secrets('localhost "x" note') == 'localhost "x" note'


def test_redact_secrets_keeps_postgres_username() -> None:
    """Postgres names the user in auth prose — kept, no host present to mask."""
    from application_sdk.errors import redact_secrets

    out = redact_secrets('FATAL: password authentication failed for user "admin"')
    assert out == 'FATAL: password authentication failed for user "admin"'


def test_redact_secrets_masks_bare_ipv4_anywhere() -> None:
    """A bare client/egress IP in prose is masked regardless of surrounding text."""
    from application_sdk.errors import redact_secrets

    assert redact_secrets("timeout connecting to 10.20.30.40:5432") == (
        "timeout connecting to ***:5432"
    )


def test_redact_secrets_does_not_mask_version_or_error_codes() -> None:
    """Octet validation keeps version triples and non-IP dotted numbers intact."""
    from application_sdk.errors import redact_secrets

    # Three-octet version string is not a dotted quad.
    assert redact_secrets("pymysql 8.0.32 driver error") == (
        "pymysql 8.0.32 driver error"
    )
    # Out-of-range octet (>255) is not a valid IPv4 and must survive.
    assert redact_secrets("code 999.1.1.1 seen") == "code 999.1.1.1 seen"


def test_cause_repr_masks_mysql_identity_end_to_end() -> None:
    """The wire envelope inherits the identity redaction (BLDX-1538)."""
    cause = ValueError(
        '(pymysql.err.OperationalError) (1045, "Access denied for user '
        "'atlan'@'49.43.224.205' (using password: YES)\")"
    )
    fd = AuthError(message="x", cause=cause).to_failure_details()
    assert fd.cause_repr is not None
    assert "49.43.224.205" not in fd.cause_repr
    assert "'atlan'@'***'" in fd.cause_repr
