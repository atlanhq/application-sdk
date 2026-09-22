"""Nothing bound for the HTTP wire may carry a credential.

The hosted path and the worker path used to disagree about this: application_sdk
redacted, server_sdk did not, and server_sdk is the side facing Atlan.
``_summarize_check`` serialises the whole ``PreflightCheck`` -- evidence dict
included -- into the ``/workflows/v1/check`` response body, so a driver
exception carrying a DSN shipped the source password to the caller.

Also pins the preflight message-resolution precedence, which had no executed
test at all despite being the rule the typed-error envelope exists to serve.
"""

from __future__ import annotations

import json

import pytest
from server_sdk.errors.leaves import AuthError, SourceUnavailableError
from server_sdk.errors.redaction import (
    mask_secret_named_keys,
    redact_secrets,
    redact_wire_value,
    sanitize_cause_repr,
    secret_named_evidence_keys,
)
from server_sdk.errors.wire import FailureDetails
from server_sdk.handler.contracts import PreflightCheck
from server_sdk.server import _summarize_check

DSN = "postgresql://atlanadmin:sup3rs3cr3t@warehouse.internal:5439/db"
SECRETS = ("sup3rs3cr3t", "hunter2", "AKIA-LIVE-KEY")


def _wire_blob(obj) -> str:
    # ensure_ascii=False so the truncation marker stays comparable as itself.
    return json.dumps(obj, default=str, ensure_ascii=False)


# ── string redaction ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (DSN, "postgresql://***@warehouse.internal:5439/db"),
        # A raw @ inside the password must not leave the tail exposed.
        ("postgresql://u:p@ss@host:5432/db", "postgresql://***@host:5432/db"),
        # ODBC quotes values containing the ';' separator.
        ("UID=sa;PWD={s3cr;et};Server=x", "UID=sa;PWD=***;Server=x"),
        # Presigned object-store URL: the signature authorises the request.
        (
            "https://x.blob.core.windows.net/c?sig=AB%2Fd&se=2026",
            "https://x.blob.core.windows.net/c?sig=***&se=2026",
        ),
    ],
)
def test_redact_secrets(raw: str, expected: str) -> None:
    assert redact_secrets(raw) == expected


def test_operator_identifiers_survive() -> None:
    """Over-redaction costs on-call the correlation IDs they triage with."""
    keep = "run_guid=7f3a correlation_uuid=99b1 next_token=page-42 uid=svc_reader"
    assert redact_secrets(keep) == keep


# ── evidence keys ───────────────────────────────────────────────────────────


def test_secret_named_keys_are_masked_not_dropped() -> None:
    ev = {"db_password": "hunter2", "api_key": "AKIA-LIVE-KEY", "host": "wh.internal"}
    masked = mask_secret_named_keys(ev)
    assert masked["db_password"] == "***"
    assert masked["api_key"] == "***"
    # The key survives so the envelope still says a credential was involved.
    assert set(masked) == set(ev)
    assert masked["host"] == "wh.internal"


def test_generic_key_names_are_not_swept_up() -> None:
    assert (
        secret_named_evidence_keys({"object_key": "k", "cache_key": "c"}) == frozenset()
    )


def test_masking_never_raises() -> None:
    """server_sdk masks where application_sdk rejects.

    ``handler/sql.py`` builds failure details inside a "report NOT_READY, never
    500" boundary, so a raising validator would turn a redaction problem into
    exactly the 500 that boundary exists to prevent.
    """
    fd = FailureDetails(
        category=AuthError.category,
        code="AUTH",
        retryable=False,
        message="denied",
        evidence={"password": "hunter2"},
    )
    assert fd.evidence["password"] == "***"


# ── hostile structures degrade, never hang ──────────────────────────────────


def test_self_referential_evidence_is_pruned_not_recursed() -> None:
    ev: dict = {"password": "hunter2"}
    ev["self"] = ev
    assert redact_wire_value(ev)["self"] is None


def test_depth_is_bounded() -> None:
    deep: dict = {}
    node = deep
    for _ in range(80):
        node["n"] = {}
        node = node["n"]
    node["dsn"] = DSN
    blob = _wire_blob(redact_wire_value(deep))
    assert "…" in blob
    # Truncation must drop the secret, not merely stop walking above it.
    assert "sup3rs3cr3t" not in blob


# ── the full envelope ───────────────────────────────────────────────────────


def test_no_secret_reaches_the_failure_envelope() -> None:
    err = SourceUnavailableError(
        f"could not connect: {DSN}",
        dsn=DSN,
        db_password="hunter2",
        api_key="AKIA-LIVE-KEY",
        host="warehouse.internal",
    )
    blob = _wire_blob(err.to_failure_details().model_dump(mode="json"))
    assert not [s for s in SECRETS if s in blob], blob
    # Redaction must not cost the diagnostic.
    assert "warehouse.internal" in blob


def test_preflight_check_message_is_scrubbed() -> None:
    """`message=str(exc)` is the documented SQL-connector fallback, and it does
    not go through FailureDetails."""
    check = PreflightCheck(name="connectivity", passed=False, message=f"boom {DSN}")
    assert "sup3rs3cr3t" not in check.message


def test_cause_repr_never_reaches_the_http_caller() -> None:
    err = SourceUnavailableError("warehouse is resuming")
    fd = err.to_failure_details().model_copy(
        update={"cause_repr": "OperationalError: " + DSN}
    )
    summary = _summarize_check(
        PreflightCheck(name="connectivity", passed=False, error=fd)
    )
    assert "cause_repr" not in summary.get("error", {})
    assert "sup3rs3cr3t" not in _wire_blob(summary)


def test_sanitize_cause_repr_redacts_before_truncating() -> None:
    exc = RuntimeError(DSN + " x" * 4000)
    out = sanitize_cause_repr(exc)
    assert "sup3rs3cr3t" not in out
    assert out.startswith("RuntimeError: ")
    assert "elided" in out


# ── message-resolution precedence (previously unexecuted) ───────────────────


def test_failed_check_error_message_beats_its_plain_message() -> None:
    check = PreflightCheck(
        name="connectivity",
        passed=False,
        message="generic failure",
        error=SourceUnavailableError(
            "warehouse is resuming", suggested_action="retry in 60s"
        ),
    )
    assert check.resolved_message == "warehouse is resuming"
    assert check.resolved_suggested_action == "retry in 60s"
    summary = _summarize_check(check)
    assert summary["message"] == "warehouse is resuming"
    assert summary["suggested_action"] == "retry in 60s"


def test_a_passing_check_keeps_its_own_message() -> None:
    check = PreflightCheck(name="connectivity", passed=True, message="all good")
    assert check.resolved_message == "all good"
    assert check.resolved_suggested_action == ""


def test_an_app_error_is_coerced_into_failure_details() -> None:
    check = PreflightCheck(name="auth", passed=False, error=AuthError("denied"))
    assert isinstance(check.error, FailureDetails)
    assert check.error.category is AuthError.category


# ── every route, not just the one that has an envelope ──────────────────────


def _sql_app_that_fails_with(driver_error: str):
    """An app whose SQL client dies the way a real driver does."""
    from server_sdk.clients.models import DatabaseConfig
    from server_sdk.clients.sql import BaseSQLClient
    from server_sdk.handler.sql import SQLHandler
    from server_sdk.server import build_asgi_app

    class _Client(BaseSQLClient):
        DB_CONFIG = DatabaseConfig(
            template="postgresql://{username}:{password}@{host}/{database}"
        )

        async def load(self, credentials):
            raise RuntimeError(driver_error)

    class _Handler(SQLHandler):
        # `client_class`, lowercase, and `_build_client` is `async def`. Getting
        # either wrong makes the handler fail with a TypeError BEFORE load() is
        # reached, so the driver error never fires and an
        # "assert secret not in body" passes without testing anything. That is
        # exactly what the first version of this file did.
        client_class = _Client

        async def _build_client(self, credentials):
            client = _Client()
            await client.load(credentials)
            return client

    return build_asgi_app(_Handler(), app_name="acme")


@pytest.mark.parametrize(
    "path", ["/workflows/v1/auth", "/workflows/v1/check", "/workflows/v1/metadata"]
)
def test_no_route_ships_the_dsn_password(path: str) -> None:
    """Round 1 scrubbed PreflightCheck.message and missed the siblings.

    SQLHandler.test_auth reports a failure as ``message=str(e)`` into
    AuthOutput, which had no validator -- and heracles calls /auth on every
    "Test authentication" press, so the password reached the browser.
    """
    from fastapi.testclient import TestClient

    driver_error = f'FATAL: password authentication failed; dsn="{DSN}"'
    client = TestClient(
        _sql_app_that_fails_with(driver_error), raise_server_exceptions=False
    )
    resp = client.post(
        path,
        json={
            "credentials": [
                {"key": "username", "value": "u"},
                {"key": "password", "value": "p"},
                {"key": "host", "value": "h"},
                {"key": "database", "value": "d"},
            ]
        },
    )
    assert "sup3rs3cr3t" not in resp.text, resp.text


def test_an_app_error_message_is_redacted_at_construction() -> None:
    """str(exc) feeds every route's HTTPException detail and log line, so the
    redaction has to happen once, in the constructor."""
    err = SourceUnavailableError(f"could not connect: {DSN}")
    assert "sup3rs3cr3t" not in str(err)
    assert "sup3rs3cr3t" not in err.message
    assert "warehouse.internal" in err.message  # diagnostic survives


# ── the redactor itself must not become the outage ──────────────────────────


def test_redaction_is_linear_not_quadratic() -> None:
    """It runs on the hosted request path, so a pathological string would stall
    the event loop for every co-hosted app.

    The scheme prefix used to be scanned from every start position, which is
    O(n^2) and needs no URL to trigger: 200k characters took ~125 seconds.
    """
    import time

    def elapsed(n: int) -> float:
        text = "postgresql://" + "a" * n
        start = time.perf_counter()
        redact_secrets(text)
        return time.perf_counter() - start

    elapsed(20_000)  # warm
    small, large = elapsed(50_000), elapsed(400_000)
    # 8x the input must not cost anything like 64x the time.
    assert large < small * 20, f"{small:.4f}s -> {large:.4f}s looks superlinear"
    assert large < 2.0, f"400k chars took {large:.2f}s"


# ── the log is a SECOND sink, and it is not the response body ───────────────
# Pod stderr ships to ClickHouse for tenant vclusters. An earlier fix redacted
# the %s operand and left exc_info=True in place, so the password landed one
# line below on the traceback.


def _capture_logs(app, body: dict) -> tuple[str, str]:
    """Drive a route and return (response text, everything logged)."""
    import io
    import logging

    from fastapi.testclient import TestClient

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        resp = TestClient(app, raise_server_exceptions=False).post(
            body["path"], json=body["json"]
        )
        return resp.text, buf.getvalue()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


SQL_BODY = [
    {"key": "username", "value": "u"},
    {"key": "password", "value": "p"},
    {"key": "host", "value": "h"},
    {"key": "database", "value": "d"},
]


@pytest.mark.parametrize(
    "path", ["/workflows/v1/auth", "/workflows/v1/check", "/workflows/v1/metadata"]
)
def test_no_route_writes_the_dsn_to_the_log(path: str) -> None:
    app = _sql_app_that_fails_with(
        f'FATAL: password authentication failed; dsn="{DSN}"'
    )
    text, logs = _capture_logs(app, {"path": path, "json": {"credentials": SQL_BODY}})
    assert "sup3rs3cr3t" not in text, "wire"
    assert "sup3rs3cr3t" not in logs, "log sink"


def test_the_traceback_survives_redaction() -> None:
    """Redaction must not cost the diagnostic — an on-call still needs the frames."""
    app = _sql_app_that_fails_with(f'FATAL: boom; dsn="{DSN}"')
    _, logs = _capture_logs(
        app, {"path": "/workflows/v1/auth", "json": {"credentials": SQL_BODY}}
    )
    assert "Traceback" in logs
    assert "password authentication failed" in logs or "boom" in logs
    assert "postgresql://***@warehouse.internal:5439/db" in logs


def test_a_raw_exception_operand_is_redacted() -> None:
    """Some sites pass the exception object itself as the %s operand."""
    import io
    import logging

    from server_sdk.observability.logger_adaptor import get_logger

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = get_logger("server_sdk.test.operand")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        logger.warning("failed: %s", RuntimeError(f'dsn="{DSN}"'))
    finally:
        logger.removeHandler(handler)
    assert "sup3rs3cr3t" not in buf.getvalue()
    assert "***" in buf.getvalue()


def test_exc_info_is_cleared_so_a_structured_handler_cannot_re_derive_it() -> None:
    """A JSON/OTel handler formats from record.exc_info, bypassing exc_text."""
    import logging

    from server_sdk.observability.logger_adaptor import get_logger

    seen: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    logger = get_logger("server_sdk.test.structured")
    logger.addHandler(_Capture())
    logger.setLevel(logging.WARNING)
    try:
        raise RuntimeError(f'dsn="{DSN}"')
    except RuntimeError:
        logger.warning("boom", exc_info=True)

    assert seen, "handler saw no record"
    record = seen[-1]
    assert record.exc_info is None, "exc_info must be cleared once folded"
    assert "sup3rs3cr3t" not in (record.exc_text or "")


# ── coverage parity with the worker-side redactor ───────────────────────────
# Expected values are application_sdk.errors.base.redact_secrets' actual output,
# inlined rather than imported: application_sdk is not installed in this
# package's venv, so importing it would make the whole comparison skip — and a
# suite that skips reports agreement it never measured.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A scheme need not start the run. An earlier lookbehind-anchored
        # pattern dropped every one of these while claiming parity.
        ("2postgresql://u:p@h", "2postgresql://***@h"),
        ("-postgresql://u:p@h", "-postgresql://***@h"),
        (".postgresql://u:p@h", ".postgresql://***@h"),
        ("+postgresql://u:p@h", "+postgresql://***@h"),
        ("x=1&y=2postgresql://u:p@h", "x=1&y=2postgresql://***@h"),
        ("10.0.0.1postgres://u:p@h", "10.0.0.1postgres://***@h"),
        # Ordinary shapes.
        ("see postgresql://u:p@h", "see postgresql://***@h"),
        ("postgresql://u:p@ss@h:5432/db", "postgresql://***@h:5432/db"),
        ("a https://x:y@h and b http://p:q@i", "a https://***@h and b http://***@i"),
        ("ftp://u:p@h  postgres://a:b@c", "ftp://***@h  postgres://***@c"),
        # Not credentials: no userinfo at all, or no scheme.
        ("postgresql://h/db", "postgresql://h/db"),
        ("x://@h", "x://@h"),
        ("://nope", "://nope"),
        ("no url here", "no url here"),
        ("123://u:p@h", "123://u:p@h"),
    ],
)
def test_url_userinfo_coverage_matches_the_worker(raw: str, expected: str) -> None:
    assert redact_secrets(raw) == expected


def test_redaction_stays_linear_on_a_long_scheme_run() -> None:
    """The completeness fix must not reintroduce the quadratic scan.

    A long run of scheme-legal characters — any hash or base64 blob in an error
    message — was O(n^2): 200k characters took ~125 seconds on the shared
    request path.
    """
    import time

    def elapsed(n: int) -> float:
        text = "postgresql://" + "a" * n
        start = time.perf_counter()
        redact_secrets(text)
        return time.perf_counter() - start

    elapsed(20_000)  # warm
    small, large = elapsed(50_000), elapsed(400_000)
    assert large < small * 20, f"{small:.4f}s -> {large:.4f}s looks superlinear"
    assert large < 2.0, f"400k chars took {large:.2f}s"


# ── the filter must redact everything AND destroy nothing ───────────────────
# Redacting the record's PARTS was wrong twice: it missed every secret that was
# not a top-level str or exception, and rewriting the format string / re-tupling
# args made %-formatting raise, which the stdlib swallows into a
# "--- Logging error ---" and emits nothing. A filter that deletes the line it
# was protecting is worse than the leak.


def _emit(call) -> tuple[str, str]:
    """Run one logging call; return (what was emitted, what stderr got)."""
    import contextlib
    import io
    import logging

    from server_sdk.observability.logger_adaptor import get_logger

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = get_logger("server_sdk.test.filter")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        call(logger)
    return buf.getvalue().strip(), err.getvalue()


@pytest.mark.parametrize(
    ("name", "call"),
    [
        ("exception as msg", lambda lg: lg.error(RuntimeError(f"connect to {DSN}"))),
        ("dsn inside a list", lambda lg: lg.info("urls %s", [DSN])),
        ("dsn inside a dict", lambda lg: lg.info("urls %s", {"dsn": DSN})),
        ("dsn as a str arg", lambda lg: lg.info("ok %s", DSN)),
        ("dsn in the format string", lambda lg: lg.info(f"ok {DSN}")),
    ],
)
def test_the_filter_redacts_every_shape(name: str, call) -> None:
    emitted, _ = _emit(call)
    assert "sup3rs3cr3t" not in emitted.lower(), f"{name}: {emitted}"
    assert emitted, f"{name}: nothing was emitted"


def test_a_secret_named_placeholder_does_not_destroy_the_record() -> None:
    """Redacting the FORMAT STRING ate the placeholder after a secret-named
    token, so %-formatting raised and the record vanished."""
    emitted, err = _emit(
        lambda lg: lg.info("failed, password=%s rejected for user %s", "hunter2", "bob")
    )
    assert "--- Logging error" not in err
    assert "bob" in emitted
    assert "hunter2" not in emitted  # the VALUE is what gets redacted
    assert "password=***" in emitted


def test_mapping_args_still_format() -> None:
    """The stdlib stores a lone Mapping as-is so %(name)s works; re-tupling it
    made formatting raise."""
    emitted, err = _emit(
        lambda lg: lg.info(
            "connecting as %(user)s to %(host)s", {"user": "u", "host": "h"}
        )
    )
    assert "--- Logging error" not in err
    assert emitted == "connecting as u to h"


def test_redaction_is_linear_on_a_body_full_of_schemes() -> None:
    """A minified JSON error body — what obstore and the drivers embed — is many
    "://" with no "@" in one whitespace-free run. Rescanning the tail for each
    one was quadratic: 43KB took ~1s, slower than the regex it replaced."""
    import json
    import time

    def elapsed(n: int) -> float:
        body = json.dumps(
            {"tried": [f"https://shard{i}.warehouse.internal/v1/t" for i in range(n)]},
            separators=(",", ":"),
        )
        start = time.perf_counter()
        redact_secrets(body)
        return time.perf_counter() - start

    elapsed(200)  # warm
    small, large = elapsed(1000), elapsed(8000)
    assert large < small * 20, f"{small:.4f}s -> {large:.4f}s looks superlinear"
    assert large < 1.0, f"8000 schemes took {large:.2f}s"
