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
from application_sdk.errors.wire import (
    FailureDetails,
    mask_secret_named_keys,
    secret_named_evidence_keys,
)
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


def test_no_secret_reaches_the_failure_envelope() -> None:
    # Evidence rides on the leaf's own dataclass fields. ``endpoint`` embeds the
    # DSN; the message does too. Neither may reach the wire unredacted.
    err = SourceUnavailableError(
        message=f"could not connect: {DSN}",
        endpoint=DSN,
        source_type="warehouse.internal",
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


def test_a_failed_auth_message_is_scrubbed() -> None:
    """A handler that reports a driver error as ``AuthOutput.message`` (the SQL
    handlers do) must not ship the DSN password to the browser."""
    from application_sdk.handler.contracts import AuthOutput, AuthStatus

    out = AuthOutput(status=AuthStatus.FAILED, message=f"auth failed: {DSN}")
    assert "sup3rs3cr3t" not in out.model_dump_json()


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


def test_an_app_error_message_is_redacted_at_construction() -> None:
    """str(exc) feeds every route's HTTPException detail and log line, so the
    redaction has to happen once, in the constructor."""
    err = SourceUnavailableError(message=f"could not connect: {DSN}")
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
        text = "warehouse://" + "a" * n
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
        ("2warehouse://u:p@h", "2warehouse://***@h"),
        ("-warehouse://u:p@h", "-warehouse://***@h"),
        (".warehouse://u:p@h", ".warehouse://***@h"),
        ("+warehouse://u:p@h", "+warehouse://***@h"),
        ("x=1&y=2warehouse://u:p@h", "x=1&y=2warehouse://***@h"),
        ("10.0.0.1warehouse://u:p@h", "10.0.0.1warehouse://***@h"),
        # Ordinary shapes.
        ("see warehouse://u:p@h", "see warehouse://***@h"),
        ("warehouse://u:p@ss@h:5432/db", "warehouse://***@h:5432/db"),
        ("a https://x:y@h and b http://p:q@i", "a https://***@h and b http://***@i"),
        ("ftp://u:p@h  warehouse://a:b@c", "ftp://***@h  warehouse://***@c"),
        # Not credentials: no userinfo at all, or no scheme.
        ("warehouse://h/db", "warehouse://h/db"),
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
        text = "warehouse://" + "a" * n
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


# ── masking has to follow the structure, like the value redaction does ──────


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ({"config": {"password": "h"}}, {"config": {"password": "***"}}),
        (
            {"creds": [{"password": "h"}, {"host": "x"}]},
            {"creds": [{"password": "***"}, {"host": "x"}]},
        ),
        ({"a": {"b": {"db_password": "h"}}}, {"a": {"b": {"db_password": "***"}}}),
        # generic names that merely resemble one must survive
        (
            {"object_key": "k", "next_token_id": "t"},
            {"object_key": "k", "next_token_id": "t"},
        ),
    ],
)
def test_secret_named_keys_are_masked_at_every_depth(evidence, expected) -> None:
    """The value redaction always walked the whole structure; the key masking
    only looked at the top level, so a secret-NAMED key one level down went
    through untouched — and evidence is routinely nested."""
    from application_sdk.errors.categories import FailureCategory
    from application_sdk.errors.wire import FailureDetails

    got = FailureDetails(
        category=FailureCategory.AUTH,
        code="AUTH",
        retryable=False,
        message="x",
        evidence=evidence,
    ).evidence
    assert got == expected


def test_a_pathologically_deep_evidence_truncates_rather_than_hangs() -> None:
    from application_sdk.errors.categories import FailureCategory
    from application_sdk.errors.wire import FailureDetails

    deep: dict = {}
    node = deep
    for _ in range(80):
        node["n"] = {}
        node = node["n"]
    node["password"] = "hunter2"

    got = FailureDetails(
        category=FailureCategory.AUTH,
        code="AUTH",
        retryable=False,
        message="x",
        evidence={"deep": deep},
    ).evidence
    assert "hunter2" not in json.dumps(got, default=str)


def test_a_nested_non_str_key_does_not_raise_out_of_the_validator() -> None:
    """Pydantic validates only the TOP-level key type, so once masking started
    recursing, a nested mapping could arrive with an int key and ``k.lower()``
    raised AttributeError straight out of a frozen-model validator -- the 500
    that this module's mask-instead-of-reject divergence exists to prevent.
    ``{57014: n}`` is the realistic shape: a connector counting rows by SQLSTATE.
    """
    masked = mask_secret_named_keys({"rows_by_sqlstate": {57014: 3}, "password": "p"})
    assert masked["password"] == "***"
    assert masked["rows_by_sqlstate"] == {57014: 3}


def test_a_non_dict_mapping_has_its_strings_redacted() -> None:
    """The two halves of the composed validator have to agree on what a mapping
    is. ``redact_wire_value`` tested ``dict`` while the masker descended into any
    ``Mapping``, so a ChainMap shipped a live DSN to the /check body -- next to a
    masked 'password', which made the response look redacted while it was not.
    """
    import collections

    wire = redact_wire_value({"cfg": collections.ChainMap({"dsn": DSN})})
    assert "warehouse://***@warehouse.internal:5439/db" in repr(wire)
    assert not any(secret in repr(wire) for secret in SECRETS)


def test_masking_keeps_a_namedtuple_a_namedtuple() -> None:
    """``redact_wire_value`` deliberately rebuilds a NamedTuple so field names
    survive; the masker flattened it back to a plain tuple one step later. No
    wire consequence -- JSON is identical -- but in-process readers of
    ``.evidence`` lost attribute access, and the sibling's comment promised
    otherwise.
    """
    import typing

    class Row(typing.NamedTuple):
        host: str
        password: str

    out = mask_secret_named_keys({"row": Row(host="db", password="p")})["row"]
    assert hasattr(type(out), "_fields")
    assert out.host == "db"
