"""A malformed `extra` must not vanish silently.

`extra` carries the connector-specific connection params -- database, warehouse,
role, account, ssl_mode. The old reader returned {} for anything it could not
decode, with no log and no error, so a caller whose extra was malformed got
"Missing required credential field(s): database" (or a connection to the wrong
port) with nothing anywhere saying why.

Mirrors application_sdk's strict/lenient split: the runtime-client path raises,
because a connector cannot build a DSN without it.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from server_sdk.credentials.utils import parse_credentials_extra
from server_sdk.errors.leaves import InvalidInputError
from server_sdk.handler.base import DefaultHandler
from server_sdk.server import build_asgi_app

GOOD = {"database": "dev", "ssl_mode": "require"}


@pytest.mark.parametrize(
    "extra",
    [GOOD, '{"database": "dev", "ssl_mode": "require"}'],
    ids=["object", "json-string"],
)
def test_both_legal_shapes_decode(extra) -> None:
    assert parse_credentials_extra({"extra": extra}) == GOOD


@pytest.mark.parametrize(
    "creds", [{}, {"extra": None}, {"extra": ""}, {"extra": "   "}], ids=str
)
def test_absent_is_not_malformed(creds: dict) -> None:
    assert parse_credentials_extra(creds) == {}


@pytest.mark.parametrize(
    "extra",
    ["{not json", "[1, 2]", '"a string"', "42", 42, ["a"]],
    ids=["bad-json", "json-array", "json-string-scalar", "json-number", "int", "list"],
)
def test_strict_rejects_an_unusable_extra(extra) -> None:
    with pytest.raises(InvalidInputError) as caught:
        parse_credentials_extra({"extra": extra})
    assert caught.value.context["field"] == "extra"


def test_lenient_drops_it_but_says_so(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert parse_credentials_extra({"extra": "{not json"}, strict=False) == {}
    assert "extra" in caplog.text.lower()


def test_the_log_never_carries_the_value(caplog: pytest.LogCaptureFixture) -> None:
    """Whatever is in `extra` is credential material."""
    with caplog.at_level(logging.WARNING):
        parse_credentials_extra({"extra": '{"password": "hunter2"'}, strict=False)
    assert "hunter2" not in caplog.text


def test_a_malformed_extra_is_not_a_500_at_the_route() -> None:
    """Raising must not defeat the boundary it sits behind."""
    client = TestClient(
        build_asgi_app(DefaultHandler(), app_name="acme"),
        raise_server_exceptions=False,
    )
    resp = client.post(
        "/workflows/v1/check",
        json={"credentials": [{"key": "extra", "value": "{not json"}]},
    )
    assert resp.status_code != 500, resp.text


# ── the two views of `extra` must agree ─────────────────────────────────────


def test_flatten_sees_both_shapes_identically() -> None:
    """A second, narrower reader is how the two views drift apart.

    flatten_credentials_to_pairs used to hoist `extra` only `if
    isinstance(extra, dict)`, so the JSON-string form vanished — and the gate
    then blocked on params the extraction path would have found.
    """
    from server_sdk.handler.contracts import flatten_credentials_to_pairs

    nested = {"host": "h", "extra": {"database": "d", "ssl_mode": "require"}}
    encoded = {"host": "h", "extra": '{"database": "d", "ssl_mode": "require"}'}
    assert flatten_credentials_to_pairs(nested) == flatten_credentials_to_pairs(encoded)
    assert {"key": "extra.database", "value": "d"} in flatten_credentials_to_pairs(
        encoded
    )


def test_flatten_never_raises_on_an_unusable_extra() -> None:
    """This path runs where no one can tell malformed from absent."""
    from server_sdk.handler.contracts import flatten_credentials_to_pairs

    assert flatten_credentials_to_pairs({"host": "h", "extra": "{not json"}) == [
        {"key": "host", "value": "h"}
    ]
