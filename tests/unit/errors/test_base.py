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
    cause = ValueError("x" * 2600)
    e = AuthError(message="x", cause=cause)
    fd = e.to_failure_details()
    assert fd.cause_repr is not None
    # 1200 head + 700 tail + the elision marker, under the type-name prefix.
    assert len(fd.cause_repr) < len("ValueError: ") + 2000 + len(
        "…[9999 chars elided]…"
    )


def test_redacts_presigned_url_signature_params() -> None:
    """Every cloud store's presigned-URL signature is bearer-equivalent material.

    None of these matched the keyword list before, so a signed URL embedded in
    a driver error survived redaction intact -- while the ``X-Goog-Credential``
    sitting next to it *was* redacted, purely because it happens to end in
    ``credential``. That inconsistency is what made the logged copy of a driver
    error unsafe. (FND-957 review)
    """
    from application_sdk.errors import redact_secrets

    gcs = redact_secrets(
        "https://storage.googleapis.com/b/k"
        "?X-Goog-Credential=svc%40p.iam&X-Goog-Signature=deadbeefcafe0123"
    )
    assert "deadbeefcafe0123" not in gcs
    assert "X-Goog-Signature=***" in gcs

    s3 = redact_secrets(
        "https://b.s3.amazonaws.com/k"
        "?X-Amz-Credential=AKIA%2F20260831&X-Amz-Signature=abc123def456"
    )
    assert "abc123def456" not in s3
    assert "X-Amz-Signature=***" in s3

    # Azure SAS names the signature ``sig``, which no longer-form keyword covers.
    azure = redact_secrets(
        "https://acct.blob.core.windows.net/c/k?sv=2021&sig=Zm9vYmFyc2ln%3D&se=2026"
    )
    assert "Zm9vYmFyc2ln" not in azure
    assert "sig=***" in azure
    # Non-secret SAS params stay readable -- they say which container and window.
    assert "sv=2021" in azure
    assert "se=2026" in azure


def test_signature_redaction_does_not_eat_pagination_cursors() -> None:
    """The keyword list is an enumeration, not a wildcard, and must stay one.

    A generic ``token`` would redact the pagination cursors an on-call needs,
    which is the same reason ``uid`` is deliberately absent. This pins the line.
    """
    from application_sdk.errors import redact_secrets

    for diagnostic in (
        "next_token=abc123&page_token=xyz",
        "continuation_token=deadbeef",
        "run_guid=01a0-sig-99",
        "config=prod&design=v2&assign=me",
        "uid=svc_account",
    ):
        assert redact_secrets(diagnostic) == diagnostic


def test_logged_cause_of_a_signed_url_error_carries_no_signature() -> None:
    """The log copy of a driver error is uncapped-ish (8000) and indexed.

    ``storage.ops`` passes ``redact_secrets(str(exc))`` as the ``error_message``
    log field, so whatever survives redaction is what gets indexed. A signed
    URL in the driver string must not.
    """
    from application_sdk.errors import redact_secrets

    driver_error = (
        "Generic GCS error: Error performing PUT "
        "https://storage.googleapis.com/bkt/artifacts%2Ft.json"
        "?X-Goog-Signature=1a2b3c4d5e6f7890 in 53ms - "
        "Server returned non-2xx status code: 500 Internal Server Error"
    )
    out = redact_secrets(driver_error)
    assert "1a2b3c4d5e6f7890" not in out
    # The rest of the diagnostic survives -- redaction must not cost the reason.
    assert "500 Internal Server Error" in out
    assert "artifacts%2Ft.json" in out


def test_cause_repr_keeps_both_ends_of_a_long_message() -> None:
    """A backend error puts the request URL at the head and the reason at the tail.

    Head-only truncation spent the whole budget on the URL and deleted the
    provider's explanation — the failure mode FND-957 was filed for. Both ends
    must survive, with an explicit marker for what was dropped in between.
    """
    cause = ValueError("HEAD_MARKER" + "x" * 2600 + "TAIL_MARKER")
    fd = AuthError(message="x", cause=cause).to_failure_details()
    assert fd.cause_repr is not None
    assert "HEAD_MARKER" in fd.cause_repr
    assert "TAIL_MARKER" in fd.cause_repr
    assert "chars elided" in fd.cause_repr


def test_cause_repr_keeps_a_full_provider_body_uncut() -> None:
    """The reported GCS envelope must survive the cap intact.

    Shape and sizes are taken from the real failure: a percent-encoded request
    URL, the status line, the provider's XML body, then obstore's Rust debug
    dump. At the old 500-char cap the URL alone consumed 374 and the cut landed
    inside ``<Message>``. (FND-957)
    """
    key = (
        "artifacts/apps/example-app/workflows/09f02b09-898f-482f-a018-c8a0bb6c6e96/"
        "runs/01a04040-1c14-7591-b735-b9bde93b9560/transformed/table/chunk-part0000.json"
    )
    url = f"https://storage.googleapis.com/example-bucket/{key.replace('/', '%2F')}"
    body = (
        "<?xml version='1.0' encoding='UTF-8'?><Error><Code>PreconditionFailed</Code>"
        "<Message>Invalid precondition during the request: at least one of the "
        "pre-conditions you specified did not hold.</Message></Error>"
    )
    cause = ValueError(
        f"Generic GCS error: Error performing POST {url}?uploads= in 53.300492ms - "
        f"Server returned non-2xx status code: 400 Bad Request: {body}"
        '\n\nDebug source:\nGeneric {\n    store: "GCS",\n}'
    )
    fd = AuthError(message="x", cause=cause).to_failure_details()
    assert fd.cause_repr is not None
    assert body in fd.cause_repr
    assert "chars elided" not in fd.cause_repr


def test_cause_repr_strips_the_obstore_debug_dump() -> None:
    """obstore appends a Rust ``Debug source:`` dump after the provider body.

    It is low-density text that competes with the diagnostic for the cap, and
    keeping it would make a tail-preserving truncation retain the dump instead
    of the provider's reason.
    """
    cause = ValueError(
        "Generic GCS error: the part that matters"
        '\n\nDebug source:\nGeneric {\n    store: "GCS",\n    source: Status,\n}'
    )
    fd = AuthError(message="x", cause=cause).to_failure_details()
    assert fd.cause_repr is not None
    assert "the part that matters" in fd.cause_repr
    assert "Debug source" not in fd.cause_repr


def test_cause_repr_redacts_before_truncating_so_the_tail_is_safe() -> None:
    """Retaining a tail must not be able to expose an unredacted secret."""
    cause = ValueError(
        "connect https://user:hunter2@host/x "
        + "PAD" * 900
        + " retry_with api_key=sk-secret123"
    )
    fd = AuthError(message="x", cause=cause).to_failure_details()
    assert fd.cause_repr is not None
    assert "hunter2" not in fd.cause_repr
    assert "sk-secret123" not in fd.cause_repr
    assert "api_key=***" in fd.cause_repr


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
