"""Nothing bound for the HTTP wire may carry a credential.

Ported from the api-package fork's suite, where the hosted path and the worker
path used to disagree about this. They now share one implementation;
``_summarize_check`` serialises the whole ``PreflightCheck`` -- evidence dict
included -- into the ``/workflows/v1/check`` response body, so a driver
exception carrying a DSN shipped the source password to the caller.

Also pins the preflight message-resolution precedence, which had no executed
test at all despite being the rule the typed-error envelope exists to serve.
"""

from __future__ import annotations

import json

import pytest

from application_sdk.errors.base import (
    redact_secrets,
    redact_wire_value,
    sanitize_cause_repr,
)
from application_sdk.errors.leaves import AuthError, SourceUnavailableError
from application_sdk.errors.wire import FailureDetails, secret_named_evidence_keys
from application_sdk.handler.contracts import PreflightCheck
from application_sdk.handler.routes import _summarize_check

DSN = "warehouse://atlanadmin:sup3rs3cr3t@warehouse.internal:5439/db"
SECRETS = ("sup3rs3cr3t", "hunter2", "AKIA-LIVE-KEY")


def _wire_blob(obj) -> str:
    # ensure_ascii=False so the truncation marker stays comparable as itself.
    return json.dumps(obj, default=str, ensure_ascii=False)


# ── string redaction ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (DSN, "warehouse://***@warehouse.internal:5439/db"),
        # A raw @ inside the password must not leave the tail exposed.
        ("warehouse://u:p@ss@host:5432/db", "warehouse://***@host:5432/db"),
        # ODBC quotes values containing the ';' separator.
        ("UID=sa;PWD={s3cr;et};Host=x", "UID=sa;PWD=***;Host=x"),
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


def test_generic_key_names_are_not_swept_up() -> None:
    assert (
        secret_named_evidence_keys({"object_key": "k", "cache_key": "c"}) == frozenset()
    )


def test_a_top_level_secret_named_key_is_rejected() -> None:
    """The producer bug stays loud at the top level (``sql_app`` degrades it)."""
    with pytest.raises(ValueError, match="secret-named"):
        FailureDetails(
            category=AuthError.category,
            code="AUTH",
            retryable=False,
            message="x",
            evidence={"password": "hunter2"},
        )


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


def test_cause_repr_never_reaches_the_http_caller() -> None:
    err = SourceUnavailableError(message="warehouse is resuming")
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
            message="warehouse is resuming", suggested_action="retry in 60s"
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
    check = PreflightCheck(name="auth", passed=False, error=AuthError(message="denied"))
    assert isinstance(check.error, FailureDetails)
    assert check.error.category is AuthError.category


# ── every route, not just the one that has an envelope ──────────────────────


def _sql_app_that_fails_with(driver_error: str):
    """An app whose source dies the way a real driver does, on every route."""
    from application_sdk.handler import DefaultHandler
    from application_sdk.handler.asgi import build_asgi_app

    class _Handler(DefaultHandler):
        async def test_auth(self, input):
            raise RuntimeError(driver_error)

        async def preflight_check(self, input):
            raise RuntimeError(driver_error)

        async def fetch_metadata(self, input):
            raise RuntimeError(driver_error)

    return build_asgi_app(_Handler(), app_name="acme")


@pytest.mark.parametrize(
    "path", ["/workflows/v1/auth", "/workflows/v1/check", "/workflows/v1/metadata"]
)
def test_no_route_ships_the_dsn_password(path: str) -> None:
    """Round 1 scrubbed PreflightCheck.message and missed the siblings.

    Every route's failure path (the HTTP detail, the envelope, the log line)
    carries ``str(exc)``; the UI calls /auth on every "Test authentication"
    press, so an unredacted driver error reaches the browser.
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


# ── the redactor itself must not become the outage ──────────────────────────


def test_redaction_is_linear_not_quadratic() -> None:
    """It runs on the hosted request path, so a pathological string would stall
    the event loop for every co-hosted app.

    The scheme prefix used to be scanned from every start position, which is
    O(n^2) and needs no URL to trigger: 200k characters took ~125 seconds, so
    the 400k here would take minutes. Linear, it takes milliseconds. The bound
    sits far from both, so it only decides how long that regression takes to
    fail. An earlier version compared the timings of two input sizes; at
    milliseconds a preemption swung the ratio past its bar on a loaded runner
    (FND-4233).

    It runs in a child process, which the timeout kills: a regression must not
    leave a thread scanning for minutes in the worker that runs later tests.
    The bound includes interpreter start-up, so it is generous.
    """
    import subprocess
    import sys

    code = (
        "from application_sdk.errors.base import redact_secrets\n"
        "redact_secrets('warehouse://' + 'a' * 400_000)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        pytest.fail("redacting 400k characters looks superlinear")
    assert proc.returncode == 0, proc.stderr


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


# ── coverage parity with the worker-side redactor ───────────────────────────
# Expected values are application_sdk.errors.base.redact_secrets' actual output,
# inlined rather than imported: application_sdk is not installed in this
# package's venv, so importing it would make the whole comparison skip — and a
# suite that skips reports agreement it never measured.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Ordinary shapes.
        ("see warehouse://u:p@h", "see warehouse://***@h"),
        ("warehouse://u:p@ss@h:5432/db", "warehouse://***@h:5432/db"),
        ("a https://x:y@h and b http://p:q@i", "a https://***@h and b http://***@i"),
        ("ftp://u:p@h  warehouse://a:b@c", "ftp://***@h  warehouse://***@c"),
        # Not credentials: no userinfo at all, or no scheme.
        ("warehouse://h/db", "warehouse://h/db"),
        ("://nope", "://nope"),
        ("no url here", "no url here"),
        ("123://u:p@h", "123://u:p@h"),
    ],
)
def test_url_userinfo_coverage_matches_the_worker(raw: str, expected: str) -> None:
    assert redact_secrets(raw) == expected
