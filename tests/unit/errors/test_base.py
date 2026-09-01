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

    cases = {
        "postgresql+psycopg://user:s3cret@db.internal:5432/prod": "postgresql+psycopg://***@db.internal:5432/prod",
        "mysql://root:hunter2@10.0.0.5/app": "mysql://***@10.0.0.5/app",
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


def test_redact_secrets_redacts_presigned_url_signatures() -> None:
    """A presigned object-store URL must not survive in a log or a cause.

    Object-store errors quote the request URL verbatim, so a failure against a
    presigned URL carries the signature that authorises it. ``credential``
    already matched ``X-Amz-Credential`` (key id plus scope) — the signature is
    the half that grants access.
    """
    from application_sdk.errors import redact_secrets

    aws = redact_secrets(
        "error sending request for url (https://b.s3.amazonaws.com/k"
        "?X-Amz-Signature=deadbeefcafe&X-Amz-Credential=AKIAEX%2Fscope)"
    )
    assert "deadbeefcafe" not in aws
    assert "X-Amz-Signature=***" in aws

    gcs = redact_secrets("https://storage.googleapis.com/k?X-Goog-Signature=abc123")
    assert "abc123" not in gcs

    azure = redact_secrets("https://a.blob.core.windows.net/k?sv=2021&sig=s3cr3t&se=x")
    assert "s3cr3t" not in azure
    assert "sig=***" in azure
    # The non-secret parts an on-call reads stay intact.
    assert "sv=2021" in azure and "se=x" in azure


def test_redact_secrets_leaves_words_ending_in_sig_alone() -> None:
    """``sig`` is short, so it carries a lookbehind: only a real param matches."""
    from application_sdk.errors import redact_secrets

    for benign in ("design=modern", "config=prod", "assign=me"):
        assert redact_secrets(benign) == benign


def test_redact_secrets_consumes_at_in_password() -> None:
    """A raw `@` inside the password must not leave the tail exposed."""
    from application_sdk.errors import redact_secrets

    out = redact_secrets("connect failed for postgresql://u:p@ssw0rd@host:5432/db")
    assert "p@ssw0rd" not in out
    assert "ssw0rd" not in out
    assert out == "connect failed for postgresql://***@host:5432/db"


# ODBC/DSN keyword syntax (CNCT-198). ODBC connection strings use `PWD=`,
# not `password=`, and they are not URLs, so neither of the two existing
# patterns covered them. Synthetic value, not a real credential.
_ODBC_DSN = (
    "DRIVER={ODBC Driver 18 for SQL Server};SERVER=host.example.com,1433;"
    "DATABASE=metadata;UID=sa;PWD=SyntheticValue123;Encrypt=yes"
)


def test_redact_secrets_redacts_odbc_pwd_keyword() -> None:
    from application_sdk.errors import redact_secrets

    assert "SyntheticValue123" not in redact_secrets(_ODBC_DSN)


def test_redact_secrets_stops_at_the_odbc_separator() -> None:
    """`[^\\s&,;#]+` already ends the match at `;`, so the pairs that follow
    the password survive. Losing them would strip the driver, host and
    encryption mode an on-call reads to diagnose the connection."""
    from application_sdk.errors import redact_secrets

    out = redact_secrets(_ODBC_DSN)
    assert "Encrypt=yes" in out
    assert "DATABASE=metadata" in out


def test_redact_secrets_keeps_odbc_uid() -> None:
    """`UID` is a user name, not a credential. Redacting it would remove
    "which account failed to log in" from every auth failure."""
    from application_sdk.errors import redact_secrets

    assert "UID=sa" in redact_secrets(_ODBC_DSN)


def test_redact_secrets_redacts_braced_odbc_pwd() -> None:
    """ODBC quotes values containing `;` as `PWD={secret;with;semicolons}`.
    The bare value class stops at the first `;` inside the braces and leaks
    the password tail, so braced values are consumed as a unit."""
    from application_sdk.errors import redact_secrets

    out = redact_secrets("PWD={secret;with;semicolons};Database=db")
    assert out == "PWD=***;Database=db"
    assert "secret" not in out and "semicolons" not in out


def test_redact_secrets_braced_pwd_inside_full_dsn() -> None:
    """End to end: a braced password inside a full DSN is masked while the
    surrounding driver/host/encryption pairs survive."""
    from application_sdk.errors import redact_secrets

    dsn = (
        "DRIVER={ODBC Driver 18 for SQL Server};SERVER=host.example.com,1433;"
        "DATABASE=metadata;UID=sa;PWD={p@ss;w0rd};Encrypt=yes"
    )
    out = redact_secrets(dsn)
    assert "p@ss" not in out and "w0rd" not in out
    assert "UID=sa" in out and "Encrypt=yes" in out
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in out


def test_redact_secrets_escaped_closing_brace_leaves_no_usable_secret() -> None:
    """An escaped closing brace (`}}` per the ODBC spec) ends the match at the
    first `}`. The residue is a brace fragment, not usable secret material."""
    from application_sdk.errors import redact_secrets

    out = redact_secrets("PWD={pa}}ss};Database=db")
    assert "PWD=***" in out
    assert "pa" not in out.split("PWD=***")[1]


def test_redact_secrets_keeps_correlation_ids() -> None:
    """A bare `uid` keyword has no word boundary in the alternation, so it
    would also match the tail of `run_guid=` and `correlation_uuid=` —
    redacting the exact IDs needed to trace a run. Guards against adding it."""
    from application_sdk.errors import redact_secrets

    out = redact_secrets("run_guid=abc123 correlation_uuid=def456")
    assert "abc123" in out
    assert "def456" in out


def test_sanitize_cause_repr_redacts_odbc_dsn() -> None:
    """End to end: `cause_repr` is serialized into Temporal activity results
    and rendered on the Sage setup-UI card."""
    from application_sdk.errors import sanitize_cause_repr

    out = sanitize_cause_repr(Exception(f"connection failed: {_ODBC_DSN}"))
    assert "SyntheticValue123" not in out


def test_safe_traceback_formats_frames() -> None:
    from application_sdk.errors import safe_traceback

    try:
        raise ValueError("boom")
    except ValueError as exc:
        out = safe_traceback(exc)
    assert "ValueError: boom" in out
    assert "test_safe_traceback_formats_frames" in out  # a frame line
    assert 'raise ValueError("boom")' in out


def test_safe_traceback_redacts_userinfo_and_secret_params() -> None:
    from application_sdk.errors import safe_traceback

    try:
        raise ValueError(
            "connect failed for postgresql://user:s3cret@db.internal/prod api_key=abc123"
        )
    except ValueError as exc:
        out = safe_traceback(exc)
    assert "s3cret" not in out
    assert "abc123" not in out
    assert "postgresql://***@db.internal/prod" in out
    assert "api_key=***" in out


def test_safe_traceback_caps_length_with_ellipsis() -> None:
    from application_sdk.errors import safe_traceback

    try:
        raise ValueError("x" * 5000)
    except ValueError as exc:
        out = safe_traceback(exc, max_len=200)
    assert len(out) == 201  # 200 chars + the "…" marker
    assert out.endswith("…")


def test_safe_traceback_handles_none() -> None:
    from application_sdk.errors import safe_traceback

    assert safe_traceback(None) == ""


def test_safe_traceback_handles_exception_without_traceback() -> None:
    from application_sdk.errors import safe_traceback

    out = safe_traceback(ValueError("never raised"))
    assert "ValueError: never raised" in out


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
