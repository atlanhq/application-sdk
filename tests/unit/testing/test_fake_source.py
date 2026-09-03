"""Unit tests for the HttpFakeSource primitive.

All fixture data here is synthetic — this is a public repo, so no captured
customer payloads, hostnames or object ids appear.

CI runs the unit tier with ``--disable-socket --allow-unix-socket`` (see
``.github/actions/unit-tests/action.yaml``) so a unit test cannot reach the
network. HttpFakeSource's whole purpose is to bind a real loopback listener, so
``pytestmark`` grants AF_INET back via ``allow_hosts`` rather than
``enable_socket``: loopback is permitted while any connect to another host is
still refused with ``SocketConnectBlockedError``, so the guard's intent — no
outbound traffic from the unit tier — stays enforced.
"""

from __future__ import annotations

import datetime
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing import fake_source
from application_sdk.testing.fake_source import (
    CursorPage,
    CursorPageLimitError,
    FakeRequest,
    FakeResponse,
    FakeSourceNotRunningError,
    FakeSourceRouteError,
    HttpFakeSource,
    HttpFakeSourceFactory,
    cursor_page,
)

pytestmark = pytest.mark.allow_hosts(["127.0.0.1"])


def _self_signed_cert(tmp_path: "Path", common_name: str) -> tuple["Path", "Path"]:
    """Generate a throwaway self-signed cert/key pair for a TLS fake.

    Generated per-test rather than committed: a checked-in key, even a test one,
    trips secret scanners and teaches the wrong habit.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            # An IP SAN, not a DNS one: the unit tier allows connects to
            # 127.0.0.1 only, and "localhost" resolves to ::1 first on this
            # runner — which pytest-socket blocks with
            # SocketConnectBlockedError naming "::1".
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(common_name))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "fake.crt"
    key_path = tmp_path / "fake.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


HYPHENATED = "X-Fake-AuthToken"

OBJECTS = [
    {"id": f"obj-{index:02d}", "name": f"object {index:02d}"} for index in range(1, 8)
]


def _request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """Issue one HTTP call, returning (status, body, headers) even for error codes."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def _json(url: str, **kwargs: Any) -> tuple[int, Any]:
    status, body, _ = _request(url, **kwargs)
    return status, json.loads(body) if body else None


def _connect(source: HttpFakeSource) -> http.client.HTTPConnection:
    """A raw keep-alive connection, for request framing urllib cannot express."""
    return http.client.HTTPConnection("127.0.0.1", source.port, timeout=5)


def _headers_only_request(
    source: HttpFakeSource,
    headers: dict[str, str],
    *,
    path: str = "/api/echo",
    conn: http.client.HTTPConnection | None = None,
) -> tuple[int, Any]:
    """Send headers announcing a body, then send no body at all."""
    connection = conn or _connect(source)
    try:
        connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else None
    finally:
        if conn is None:
            connection.close()


def _chunked_request(
    source: HttpFakeSource,
    raw: bytes,
    *,
    half_close: bool = False,
    path: str = "/api/echo",
) -> tuple[int, Any]:
    """Send a hand-written ``Transfer-Encoding: chunked`` body, valid or not."""
    connection = _connect(source)
    try:
        connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        if raw:
            connection.send(raw)
        if half_close and connection.sock is not None:
            connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else None
    finally:
        connection.close()


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the module logger's warnings without depending on the loguru sink."""
    captured: list[str] = []

    def record(message: str, *args: Any, **_kwargs: Any) -> None:
        captured.append(message % args if args else message)

    monkeypatch.setattr(fake_source.logger, "warning", record)
    return captured


@pytest.fixture
def fake() -> Iterator[HttpFakeSource]:
    source = HttpFakeSource(name="test-source")
    source.route(r"/api/objects", lambda _r: {"items": OBJECTS})
    source.route(
        r"/api/objects/(?P<object_id>obj-\d+)",
        lambda r: next(
            (o for o in OBJECTS if o["id"] == r.path_params["object_id"]), None
        ),
    )
    with source:
        yield source


class TestLifecycle:
    def test_binds_loopback_on_ephemeral_port(self, fake: HttpFakeSource) -> None:
        assert fake.base_url.startswith("http://127.0.0.1:")
        assert fake.port > 0

    def test_defaults_to_loopback_so_the_fixture_case_is_unchanged(self) -> None:
        """The default bind is the loopback/ephemeral pair fixtures rely on."""
        source = HttpFakeSource()
        assert source.bind_host == "127.0.0.1"
        assert source.requested_port == 0
        with source:
            assert source.base_url.startswith("http://127.0.0.1:")
            assert source.port != 0

    def test_serves_a_requested_port_when_one_is_given(self) -> None:
        """A fixed port is what a peer reaching the fake by name must dial."""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        chosen = probe.getsockname()[1]
        probe.close()

        source = HttpFakeSource(port=chosen)
        with source:
            assert source.port == chosen
            assert source.base_url == f"http://127.0.0.1:{chosen}"

    def test_wildcard_bind_reports_a_dialable_base_url(self) -> None:
        """A wildcard listens everywhere, so base_url must not echo it back.

        ``0.0.0.0`` is an accept-on-every-interface instruction, not an address
        anything can connect to. Reporting it verbatim would hand callers a URL
        that fails at connect time, far from its cause, so base_url reports
        loopback — a real route to this server — and the response below proves
        the reported URL actually serves.
        """
        source = HttpFakeSource(bind_host="0.0.0.0")  # noqa: S104 — the case under test
        source.route(r"/ping", lambda _r: FakeResponse.json_({"ok": True}))
        with source:
            assert "0.0.0.0" not in source.base_url
            assert source.base_url == f"http://127.0.0.1:{source.port}"
            with urllib.request.urlopen(f"{source.base_url}/ping", timeout=5) as resp:
                assert json.loads(resp.read()) == {"ok": True}

    def test_serves_tls_when_given_an_ssl_context(self, tmp_path: Path) -> None:
        """A TLS fake is reachable over https and says so in base_url.

        Connectors whose client forces an ``https://`` scheme onto whatever host
        it is handed cannot reach a plain-HTTP fake at all, however correct its
        routes are. The self-signed cert is generated here rather than committed,
        so nothing in the repo looks like a real key.
        """
        cert, key = _self_signed_cert(tmp_path, "127.0.0.1")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))

        source = HttpFakeSource(ssl_context=ctx)
        source.route(r"/ping", lambda _r: FakeResponse.json_({"ok": True}))
        with source:
            assert source.base_url.startswith("https://")
            client_ctx = ssl.create_default_context(cafile=str(cert))
            conn = http.client.HTTPSConnection(
                "127.0.0.1", source.port, context=client_ctx, timeout=5
            )
            conn.request("GET", "/ping")
            resp = conn.getresponse()
            assert resp.status == 200
            assert json.loads(resp.read()) == {"ok": True}
            conn.close()

    def test_plain_http_stays_the_default(self) -> None:
        """No ssl_context means no behaviour change for every existing caller."""
        source = HttpFakeSource()
        assert source.ssl_context is None
        with source:
            assert source.base_url.startswith("http://")

    def test_base_url_before_start_is_an_error_not_a_hang(self) -> None:
        source = HttpFakeSource()
        with pytest.raises(FakeSourceNotRunningError, match="not running"):
            _ = source.base_url
        with pytest.raises(FakeSourceNotRunningError, match="not running"):
            _ = source.port

    def test_start_is_idempotent_and_keeps_the_same_port(self) -> None:
        source = HttpFakeSource()
        with source:
            first = source.port
            source.start()
            assert source.port == first

    def test_stop_is_idempotent(self) -> None:
        source = HttpFakeSource().start()
        source.stop()
        source.stop()
        with pytest.raises(FakeSourceNotRunningError):
            _ = source.base_url


class TestRouting:
    def test_static_route(self, fake: HttpFakeSource) -> None:
        status, payload = _json(f"{fake.base_url}/api/objects")
        assert status == 200
        assert payload == {"items": OBJECTS}

    def test_named_group_becomes_a_path_param(self, fake: HttpFakeSource) -> None:
        status, payload = _json(f"{fake.base_url}/api/objects/obj-03")
        assert status == 200
        assert payload == {"id": "obj-03", "name": "object 03"}

    def test_handler_returning_none_is_a_404(self, fake: HttpFakeSource) -> None:
        status, payload = _json(f"{fake.base_url}/api/objects/obj-99")
        assert status == 404
        assert payload == {"error": "not found"}

    def test_route_matches_full_path_not_a_prefix(self, fake: HttpFakeSource) -> None:
        status, _ = _json(f"{fake.base_url}/api/objects/obj-03/extra")
        assert status == 404

    def test_registration_order_is_match_order(self) -> None:
        source = HttpFakeSource()
        source.route(r"/api/objects/special", lambda _r: {"which": "specific"})
        source.route(r"/api/objects/(?P<name>[^/]+)", lambda _r: {"which": "broad"})
        with source:
            assert _json(f"{source.base_url}/api/objects/special")[1] == {
                "which": "specific"
            }
            assert _json(f"{source.base_url}/api/objects/other")[1] == {
                "which": "broad"
            }

    def test_query_string_is_not_part_of_the_matched_path(
        self, fake: HttpFakeSource
    ) -> None:
        status, _ = _json(f"{fake.base_url}/api/objects?limit=2&type=a")
        assert status == 200

    def test_route_returns_self_for_chaining(self) -> None:
        source = HttpFakeSource()
        assert source.route(r"/a", lambda _r: {}) is source
        assert [pattern for pattern, _ in source.routes] == ["/a"]

    def test_route_requires_at_least_one_method(self) -> None:
        with pytest.raises(FakeSourceRouteError, match="at least one HTTP method"):
            HttpFakeSource().route(r"/a", lambda _r: {}, methods=())

    def test_routes_can_be_passed_to_the_constructor(self) -> None:
        with HttpFakeSource(routes=[(r"/ping", lambda _r: {"ok": True})]) as source:
            assert _json(f"{source.base_url}/ping")[1] == {"ok": True}


class TestCatchAllFastFourOhFour:
    """The load-bearing behaviour: an unexpected call must fail, never hang."""

    def test_unmatched_path_is_404(self, fake: HttpFakeSource) -> None:
        status, payload = _json(f"{fake.base_url}/api/not/modelled")
        assert status == 404
        assert payload == {"error": "not found"}

    @pytest.mark.parametrize(
        "method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    )
    def test_every_verb_answers_rather_than_hanging(
        self, fake: HttpFakeSource, method: str
    ) -> None:
        data = b"{}" if method in {"POST", "PUT", "PATCH"} else None
        status, _, _ = _request(
            f"{fake.base_url}/api/objects", method=method, data=data
        )
        assert status == 404

    def test_wrong_method_on_a_known_path_is_404(self, fake: HttpFakeSource) -> None:
        status, _, _ = _request(
            f"{fake.base_url}/api/objects", method="POST", data=b"{}"
        )
        assert status == 404

    def test_post_body_is_drained_so_the_connection_is_reusable(
        self, fake: HttpFakeSource
    ) -> None:
        status, _, _ = _request(
            f"{fake.base_url}/api/unknown", method="POST", data=b"x" * 4096
        )
        assert status == 404
        assert _json(f"{fake.base_url}/api/objects")[0] == 200

    def test_not_found_body_is_configurable(self) -> None:
        source = HttpFakeSource(not_found_body={"message": "Not Found"})
        with source:
            assert _json(f"{source.base_url}/nope")[1] == {"message": "Not Found"}

    def test_unmatched_calls_are_recorded(self, fake: HttpFakeSource) -> None:
        _json(f"{fake.base_url}/api/absent")
        assert [(r.method, r.path) for r in fake.unmatched] == [("GET", "/api/absent")]

    def test_a_matched_route_returning_404_is_not_unmatched(
        self, fake: HttpFakeSource
    ) -> None:
        _json(f"{fake.base_url}/api/objects/obj-99")
        assert list(fake.unmatched) == []

    def test_unmatched_is_logged(
        self, fake: HttpFakeSource, warnings: list[str]
    ) -> None:
        _json(f"{fake.base_url}/api/absent")
        assert any("no route for" in message for message in warnings)
        assert any("/api/absent" in message for message in warnings)

    def test_warn_unmatched_can_be_silenced(self, warnings: list[str]) -> None:
        with HttpFakeSource(warn_unmatched=False) as source:
            _json(f"{source.base_url}/api/absent")
        assert warnings == []


class TestRequestBodyFraming:
    """A body the server cannot frame must answer and never desync the connection."""

    @pytest.fixture
    def echo(self) -> Iterator[HttpFakeSource]:
        source = HttpFakeSource(name="echo-source")
        source.route(
            r"/api/echo",
            lambda r: {"body": r.body.decode()},
            methods=("POST",),
        )
        source.route(r"/api/objects", lambda _r: {"items": OBJECTS})
        with source:
            yield source

    def test_chunked_post_body_reaches_the_handler(self, echo: HttpFakeSource) -> None:
        conn = _connect(echo)
        try:
            conn.request("POST", "/api/echo", body=iter([b"one-", b"two-", b"three"]))
            response = conn.getresponse()
            assert response.status == 200
            assert json.loads(response.read()) == {"body": "one-two-three"}
        finally:
            conn.close()

    def test_chunked_post_leaves_the_next_request_uncorrupted(
        self, echo: HttpFakeSource
    ) -> None:
        conn = _connect(echo)
        try:
            conn.request("POST", "/api/echo", body=iter([b"payload"]))
            # Decoded, not byte-compared: the server owns response framing and
            # its JSON writer's whitespace is not part of the contract.
            assert json.loads(conn.getresponse().read()) == {"body": "payload"}
            conn.request("GET", "/api/objects")
            following = conn.getresponse()
            assert following.status == 200
            assert json.loads(following.read()) == {"items": OBJECTS}
        finally:
            conn.close()

    def test_empty_chunked_post_is_an_empty_body(self, echo: HttpFakeSource) -> None:
        conn = _connect(echo)
        try:
            conn.request("POST", "/api/echo", body=iter([]))
            assert json.loads(conn.getresponse().read()) == {"body": ""}
        finally:
            conn.close()

    @pytest.mark.parametrize("value", ["abc", "1.5", "-1", "0x10", "12abc"])
    def test_unparseable_content_length_is_a_400(
        self, echo: HttpFakeSource, value: str
    ) -> None:
        status, payload = _headers_only_request(echo, {"Content-Length": value})
        assert status == 400
        assert payload == {"error": "malformed request body"}

    def test_oversize_content_length_is_a_413(self, echo: HttpFakeSource) -> None:
        status, payload = _headers_only_request(
            echo, {"Content-Length": str(64 * 1024 * 1024 + 1)}
        )
        assert status == 413
        assert payload == {"error": "request body too large"}

    @pytest.mark.parametrize(
        ("raw", "half_close"),
        [
            (b"not-a-chunk-size\r\n", False),
            (b"", True),
            (b"-1\r\n", False),
            (b"4000001\r\n", False),
            (b"5\r\nab", True),
            (b"2\r\nabXX", False),
            (b"0\r\n", True),
        ],
        ids=[
            "unparseable-size",
            "eof-before-size",
            "negative-size",
            "oversize-chunk",
            "truncated-chunk",
            "missing-chunk-terminator",
            "eof-in-trailers",
        ],
    )
    def test_malformed_chunk_framing_is_a_400(
        self, echo: HttpFakeSource, raw: bytes, half_close: bool
    ) -> None:
        status, payload = _chunked_request(echo, raw, half_close=half_close)
        assert status == 400
        assert payload == {"error": "malformed request body"}

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"Content-Length": "abc"}, 400),
            ({"Content-Length": str(64 * 1024 * 1024 + 1)}, 413),
        ],
    )
    def test_a_refused_body_closes_the_connection(
        self, echo: HttpFakeSource, headers: dict[str, str], expected: int
    ) -> None:
        conn = _connect(echo)
        try:
            status, _ = _headers_only_request(echo, headers, conn=conn)
            assert status == expected
            assert conn.sock is None
        finally:
            conn.close()

    def test_refused_body_is_logged(
        self, echo: HttpFakeSource, warnings: list[str]
    ) -> None:
        _headers_only_request(echo, {"Content-Length": "abc"})
        assert any("could not read the body" in message for message in warnings)


class TestHandlerContract:
    def test_dict_shorthand_is_a_200(self) -> None:
        with HttpFakeSource(routes=[(r"/a", lambda _r: {"x": 1})]) as source:
            assert _json(f"{source.base_url}/a") == (200, {"x": 1})

    def test_list_shorthand_is_a_200(self) -> None:
        with HttpFakeSource(routes=[(r"/a", lambda _r: [1, 2])]) as source:
            assert _json(f"{source.base_url}/a") == (200, [1, 2])

    def test_status_body_tuple_shorthand(self) -> None:
        with HttpFakeSource(routes=[(r"/a", lambda _r: (503, {"e": "busy"}))]) as src:
            assert _json(f"{src.base_url}/a") == (503, {"e": "busy"})

    def test_fake_response_with_custom_headers(self) -> None:
        source = HttpFakeSource(
            routes=[
                (
                    r"/a",
                    lambda _r: FakeResponse.json_(
                        {"vertices": []}, cursorMark="off:7", totalFound="7"
                    ),
                )
            ]
        )
        with source:
            status, _, headers = _request(f"{source.base_url}/a")
        assert status == 200
        assert headers["cursorMark"] == "off:7"
        assert headers["totalFound"] == "7"

    def test_text_response_keeps_its_content_type(self) -> None:
        source = HttpFakeSource(
            routes=[(r"/token", lambda _r: FakeResponse.text("token-value"))]
        )
        with source:
            status, body, headers = _request(f"{source.base_url}/token")
        assert (status, body) == (200, b"token-value")
        assert headers["Content-Type"].startswith("text/plain")

    def test_xml_response_for_a_non_json_source(self) -> None:
        xml = "<Report><Name>synthetic</Name></Report>"
        source = HttpFakeSource(
            routes=[
                (
                    r"/soap",
                    lambda _r: FakeResponse.text(xml, content_type="text/xml"),
                )
            ]
        )
        with source:
            status, body, headers = _request(f"{source.base_url}/soap")
        assert (status, body) == (200, xml.encode())
        assert headers["Content-Type"] == "text/xml"

    def test_raw_bytes_are_not_re_encoded(self) -> None:
        payload = b"\x00\x01\x02binary"
        source = HttpFakeSource(
            routes=[(r"/blob", lambda _r: FakeResponse.raw(payload))]
        )
        with source:
            status, body, _ = _request(f"{source.base_url}/blob")
        assert (status, body) == (200, payload)

    def test_handler_exception_becomes_a_500_not_a_dropped_connection(
        self, warnings: list[str]
    ) -> None:
        def boom(_request: FakeRequest) -> Any:
            raise RuntimeError("synthetic handler bug")

        with HttpFakeSource(routes=[(r"/a", boom)]) as source:
            status, payload = _json(f"{source.base_url}/a")
        assert status == 500
        assert payload is not None
        assert "synthetic handler bug" in payload["detail"]
        assert any("raised" in message for message in warnings)

    def test_head_sends_no_body_but_still_answers(self) -> None:
        source = HttpFakeSource()
        source.route(r"/a", lambda _r: {"x": 1}, methods=("GET", "HEAD"))
        with source:
            status, body, headers = _request(f"{source.base_url}/a", method="HEAD")
        assert status == 200
        assert body == b""
        assert headers["Content-Length"] == "0"


class TestFakeRequest:
    def test_exposes_method_path_params_and_headers(self) -> None:
        seen: list[FakeRequest] = []

        def capture(request: FakeRequest) -> Any:
            seen.append(request)
            return {"ok": True}

        source = HttpFakeSource()
        source.route(r"/api/(?P<kind>\w+)/(?P<item_id>\w+)", capture, methods=("POST",))
        with source:
            _request(
                f"{source.base_url}/api/tables/t1?verbose=true&tag=a&tag=b",
                method="POST",
                data=b'{"n": 1}',
                headers={"X-Project-Id": "project-1"},
            )

        request = seen[0]
        assert request.method == "POST"
        assert request.path == "/api/tables/t1"
        assert request.path_params == {"kind": "tables", "item_id": "t1"}
        assert request.params["verbose"] == "true"
        assert list(request.query["tag"]) == ["a", "b"]
        assert request.json() == {"n": 1}
        assert request.header("x-project-id") == "project-1"

    def test_header_lookup_is_case_insensitive_with_a_default(self) -> None:
        request = FakeRequest(
            method="GET",
            path="/a",
            params={},
            query={},
            path_params={},
            headers={"X-MSTR-ProjectID": "p1"},
            body=b"",
        )
        assert request.header("x-mstr-projectid") == "p1"
        assert request.header("absent", "fallback") == "fallback"

    def test_json_returns_default_on_empty_or_malformed_body(self) -> None:
        def build(body: bytes) -> FakeRequest:
            return FakeRequest(
                method="POST",
                path="/a",
                params={},
                query={},
                path_params={},
                headers={},
                body=body,
            )

        assert build(b"").json() is None
        assert build(b"not json").json(default={}) == {}

    def test_param_treats_blank_as_absent(self) -> None:
        request = FakeRequest(
            method="GET",
            path="/a",
            params={"cursor": "", "limit": "5"},
            query={},
            path_params={},
            headers={},
            body=b"",
        )
        assert request.param("cursor") is None
        assert request.param("cursor", "start") == "start"
        assert request.param("limit") == "5"

    def test_int_param_falls_back_rather_than_raising(self) -> None:
        request = FakeRequest(
            method="GET",
            path="/a",
            params={"limit": "abc", "offset": "5", "blank": ""},
            query={},
            path_params={},
            headers={},
            body=b"",
        )
        assert request.int_param("limit", 100) == 100
        assert request.int_param("offset", 0) == 5
        assert request.int_param("blank", 7) == 7
        assert request.int_param("absent", 9) == 9


class TestRecording:
    def test_requests_are_recorded_in_arrival_order(self, fake: HttpFakeSource) -> None:
        _json(f"{fake.base_url}/api/objects")
        _json(f"{fake.base_url}/api/objects/obj-01")
        assert [r.path for r in fake.requests] == [
            "/api/objects",
            "/api/objects/obj-01",
        ]

    def test_hits_counts_per_route_pattern(self, fake: HttpFakeSource) -> None:
        _json(f"{fake.base_url}/api/objects")
        _json(f"{fake.base_url}/api/objects/obj-01")
        _json(f"{fake.base_url}/api/objects/obj-02")
        assert fake.hits(r"/api/objects") == 1
        assert fake.hits(r"/api/objects/(?P<object_id>obj-\d+)") == 2
        assert fake.hits(r"/never/registered") == 0

    def test_unused_routes_names_what_was_never_called(
        self, fake: HttpFakeSource
    ) -> None:
        _json(f"{fake.base_url}/api/objects")
        assert fake.unused_routes() == [r"/api/objects/(?P<object_id>obj-\d+)"]

    def test_unused_routes_survives_reset(self, fake: HttpFakeSource) -> None:
        """The dead-route ledger is per-suite; only ``hits`` is per-test.

        ``reset`` runs before every test via the kit's autouse fixture, so a
        ``unused_routes`` that read the per-test counter would report every route
        the *other* tests exercised — failing any suite with more than one
        route-usage assertion.
        """
        _json(f"{fake.base_url}/api/objects")
        fake.reset()
        assert fake.hits(r"/api/objects") == 0
        assert fake.unused_routes() == [r"/api/objects/(?P<object_id>obj-\d+)"]

    def test_reset_clears_recordings_but_keeps_serving(
        self, fake: HttpFakeSource
    ) -> None:
        _json(f"{fake.base_url}/api/objects")
        _json(f"{fake.base_url}/api/absent")
        fake.reset()
        assert list(fake.requests) == []
        assert list(fake.unmatched) == []
        assert fake.hits(r"/api/objects") == 0
        assert _json(f"{fake.base_url}/api/objects")[0] == 200


class TestAuthorize:
    def test_authorizer_can_short_circuit_with_a_401(self) -> None:
        def require_token(request: FakeRequest) -> Any:
            if request.header("authorization") != "Bearer synthetic-token":
                return FakeResponse.json_({"error": "unauthorized"}, status=401)
            return None

        source = HttpFakeSource(
            routes=[(r"/api/objects", lambda _r: {"items": []})],
            authorize=require_token,
        )
        with source:
            assert _json(f"{source.base_url}/api/objects")[0] == 401
            status, payload = _json(
                f"{source.base_url}/api/objects",
                headers={"Authorization": "Bearer synthetic-token"},
            )
        assert (status, payload) == (200, {"items": []})

    def test_authorizer_returning_none_lets_the_request_through(self) -> None:
        source = HttpFakeSource(
            routes=[(r"/a", lambda _r: {"ok": True})],
            authorize=lambda _r: None,
        )
        with source:
            assert _json(f"{source.base_url}/a") == (200, {"ok": True})


class TestSessionScopedFixtureShape:
    """The primitive must survive the fixture slot a testcontainer occupies."""

    def test_one_server_serves_many_sequential_tests_after_reset(
        self, fake: HttpFakeSource
    ) -> None:
        for _ in range(3):
            fake.reset()
            assert _json(f"{fake.base_url}/api/objects") == (200, {"items": OBJECTS})
            assert len(fake.requests) == 1
            assert list(fake.unmatched) == []

    def test_concurrent_clients_are_served(self, fake: HttpFakeSource) -> None:
        import concurrent.futures

        url = fake.base_url
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda _n: _json(f"{url}/api/objects"), range(8)),
            )
        assert all(result == (200, {"items": OBJECTS}) for result in results)
        assert len(fake.requests) == 8


def _settled_thread_count(timeout: float = 2.0) -> int:
    """The live thread count once two consecutive samples agree.

    An unrelated pool spinning up mid-measurement would otherwise make the
    thread-baseline assertions flaky.
    """
    deadline = time.monotonic() + timeout
    previous = threading.active_count()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        current = threading.active_count()
        if current == previous:
            return current
        previous = current
    return previous


class TestTrailingSlash:
    def test_a_single_trailing_slash_is_tolerated(self, fake: HttpFakeSource) -> None:
        status, payload = _json(f"{fake.base_url}/api/objects/")
        assert status == 200
        assert payload == {"items": OBJECTS}

    def test_the_recorded_path_keeps_the_slash_as_sent(
        self, fake: HttpFakeSource
    ) -> None:
        _json(f"{fake.base_url}/api/objects/")
        assert [r.path for r in fake.requests] == ["/api/objects/"]

    def test_an_exact_match_beats_the_normalised_retry(self) -> None:
        source = HttpFakeSource()
        source.route(r"/api/thing/", lambda _r: {"which": "with-slash"})
        source.route(r"/api/thing", lambda _r: {"which": "without-slash"})
        with source:
            assert _json(f"{source.base_url}/api/thing/")[1] == {"which": "with-slash"}
            assert _json(f"{source.base_url}/api/thing")[1] == {
                "which": "without-slash"
            }

    def test_a_double_trailing_slash_is_not_normalised(
        self, fake: HttpFakeSource
    ) -> None:
        assert _json(f"{fake.base_url}/api/objects//")[0] == 404

    def test_normalisation_does_not_turn_a_prefix_into_a_match(
        self, fake: HttpFakeSource
    ) -> None:
        assert _json(f"{fake.base_url}/api/objects/obj-03/extra/")[0] == 404


class TestThreadTeardown:
    """stop() must join the per-connection handler threads, not just the accept loop."""

    def test_stop_returns_to_the_thread_baseline(self) -> None:
        baseline = _settled_thread_count()
        source = HttpFakeSource(connection_timeout=0.2)
        source.route(r"/ping", lambda _r: {"ok": True})
        source.start()
        try:
            assert _json(f"{source.base_url}/ping")[0] == 200
        finally:
            source.stop()
        assert threading.active_count() == baseline

    def test_stop_joins_a_handler_blocked_on_an_idle_keep_alive_connection(
        self,
    ) -> None:
        """The case that slipped through: the thread survived stop() in readline."""
        baseline = _settled_thread_count()
        source = HttpFakeSource(connection_timeout=0.2)
        source.route(r"/ping", lambda _r: {"ok": True})
        source.start()
        connection = _connect(source)
        try:
            connection.request("GET", "/ping")
            assert connection.getresponse().read()
            assert threading.active_count() > baseline
            source.stop()
            assert threading.active_count() == baseline
        finally:
            connection.close()
            source.stop()

    def test_stop_warns_about_a_thread_that_outlives_the_join_budget(
        self, warnings: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = threading.Event()
        straggler = threading.Thread(
            target=release.wait, name="wedged-handler", daemon=True
        )
        straggler.start()
        monkeypatch.setattr(fake_source, "_JOIN_GRACE_SECONDS", 0.05)
        monkeypatch.setattr(
            fake_source._FakeSourceServer,
            "handler_threads",
            lambda _self: [straggler],
        )
        source = HttpFakeSource(name="wedged", connection_timeout=0.01)
        source.start()
        try:
            source.stop()
            assert any("left 1 thread(s) running" in message for message in warnings)
            assert any("wedged-handler" in message for message in warnings)
        finally:
            release.set()
            straggler.join(timeout=5)

    def test_stop_on_a_never_started_source_does_nothing(
        self, warnings: list[str]
    ) -> None:
        HttpFakeSource().stop()
        assert warnings == []


class TestHyphenatedResponseHeaders:
    """``X-Fake-AuthToken=`` is a syntax error, so the mapping form is required."""

    def test_every_constructor_accepts_a_hyphenated_header(self) -> None:
        header = {HYPHENATED: "token-0123"}
        assert FakeResponse.json_({}, headers=header).headers == header
        assert FakeResponse.text("body", headers=header).headers == header
        assert FakeResponse.raw(b"body", headers=header).headers == header

    def test_keyword_headers_still_work(self) -> None:
        assert FakeResponse.json_({}, X_Token="a").headers == {"X_Token": "a"}
        assert FakeResponse.text("b", X_Token="a").headers == {"X_Token": "a"}
        assert FakeResponse.raw(b"c", X_Token="a").headers == {"X_Token": "a"}

    def test_the_two_forms_combine(self) -> None:
        response = FakeResponse.json_({}, headers={HYPHENATED: "a"}, X_Plain="b")
        assert response.headers == {HYPHENATED: "a", "X_Plain": "b"}

    def test_a_keyword_wins_over_the_mapping_on_the_same_key(self) -> None:
        response = FakeResponse.json_(
            {}, headers={"X_Token": "from-mapping"}, X_Token="from-keyword"
        )
        assert response.headers == {"X_Token": "from-keyword"}

    def test_positional_status_and_content_type_are_unchanged(self) -> None:
        assert FakeResponse.json_({}, 201).status == 201
        assert FakeResponse.text("b", 202, "text/html").content_type == "text/html"
        assert FakeResponse.raw(b"c", 203, "image/png").content_type == "image/png"

    def test_a_hyphenated_header_reaches_the_client(self) -> None:
        source = HttpFakeSource(
            routes=[
                (
                    r"/login",
                    lambda _r: FakeResponse.json_(
                        {"ok": True}, headers={HYPHENATED: "token-0123"}
                    ),
                )
            ]
        )
        with source:
            status, _, headers = _request(f"{source.base_url}/login")
        assert status == 200
        assert headers[HYPHENATED] == "token-0123"


class TestHttpFakeSourceFactory:
    def test_builds_started_sources_and_stops_them_all(self) -> None:
        baseline = _settled_thread_count()
        factory = HttpFakeSourceFactory()
        first = factory(name="first", connection_timeout=0.2)
        second = factory(name="second", connection_timeout=0.2)
        first.route(r"/ping", lambda _r: {"which": "first"})
        try:
            assert _json(f"{first.base_url}/ping")[1] == {"which": "first"}
            assert list(factory.sources) == [first, second]
        finally:
            factory.stop_all()
        assert list(factory.sources) == []
        assert threading.active_count() == baseline
        with pytest.raises(FakeSourceNotRunningError, match="not running"):
            _ = first.base_url

    def test_reset_all_clears_every_source(self) -> None:
        factory = HttpFakeSourceFactory()
        source = factory(name="resettable", connection_timeout=0.2)
        source.route(r"/ping", lambda _r: {"ok": True})
        try:
            _json(f"{source.base_url}/ping")
            assert source.hits(r"/ping") == 1
            factory.reset_all()
            assert source.hits(r"/ping") == 0
            assert list(source.requests) == []
        finally:
            factory.stop_all()

    def test_a_source_that_fails_to_start_is_not_remembered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_self: HttpFakeSource) -> None:
            raise OSError("cannot bind")

        monkeypatch.setattr(HttpFakeSource, "start", boom)
        factory = HttpFakeSourceFactory()
        with pytest.raises(OSError, match="cannot bind"):
            factory(name="doomed")
        assert list(factory.sources) == []


class TestResponseFramingIsServerOwned:
    """A handler must not be able to put a second framing header on the wire."""

    def test_handler_content_length_is_dropped(self) -> None:
        fake = HttpFakeSource(name="framing")
        fake.route(
            r"/dup",
            lambda _r: FakeResponse.json_({"a": 1}, headers={"Content-Length": "999"}),
        )
        with fake:
            conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
            conn.request("GET", "/dup")
            response = conn.getresponse()
            lengths = [
                v for k, v in response.getheaders() if k.lower() == "content-length"
            ]
            body = response.read()
            conn.close()
        assert lengths == [
            str(len(body))
        ], f"expected exactly the server's own Content-Length, got {lengths!r}"

    @pytest.mark.parametrize("header", ["Transfer-Encoding", "Connection"])
    def test_hop_by_hop_headers_are_dropped(self, header: str) -> None:
        fake = HttpFakeSource(name="framing")
        fake.route(
            r"/x", lambda _r: FakeResponse.json_({"a": 1}, headers={header: "chunked"})
        )
        with fake:
            conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
            conn.request("GET", "/x")
            response = conn.getresponse()
            sent = [v for k, v in response.getheaders() if k.lower() == header.lower()]
            response.read()
            conn.close()
        assert sent == [], f"{header} should not be echoed from a handler, got {sent!r}"

    def test_a_non_framing_header_still_reaches_the_client(self) -> None:
        fake = HttpFakeSource(name="framing")
        fake.route(
            r"/x", lambda _r: FakeResponse.json_({"a": 1}, headers={"X-Cursor": "abc"})
        )
        with fake:
            conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
            conn.request("GET", "/x")
            response = conn.getresponse()
            assert response.getheader("X-Cursor") == "abc"
            response.read()
            conn.close()


class TestRaisingAuthorizeAnswers:
    """An authorize hook is consumer code: a bug in it must answer, not hang."""

    def test_raising_authorize_returns_500(self) -> None:
        def boom(_request: FakeRequest) -> None:
            raise RuntimeError("authorize blew up")

        fake = HttpFakeSource(name="auth", authorize=boom)
        fake.route(r"/x", lambda _r: FakeResponse.json_({"ok": True}))
        with fake:
            conn = http.client.HTTPConnection("127.0.0.1", fake.port, timeout=5)
            conn.request("GET", "/x")
            response = conn.getresponse()
            status, body = response.status, response.read()
            conn.close()
        assert status == 500
        assert b"authorize" in body


class TestCursorPagination:
    def _page(self, params: dict[str, str], **kwargs: Any) -> CursorPage:
        request = FakeRequest(
            method="GET",
            path="/api/objects",
            params=params,
            query={},
            path_params={},
            headers={},
            body=b"",
        )
        return cursor_page(OBJECTS, request, **kwargs)

    def test_absent_cursor_is_the_first_page(self) -> None:
        page = self._page({"limit": "3"})
        assert [item["id"] for item in page.items] == ["obj-01", "obj-02", "obj-03"]
        assert page.has_more is True

    def test_token_is_opaque_not_a_bare_offset(self) -> None:
        token = self._page({"limit": "3"}).next_cursor
        assert token is not None
        assert token != "3"
        assert not token.isdigit()

    def test_full_traversal_visits_every_item_exactly_once(self) -> None:
        seen: list[str] = []
        params = {"limit": "2"}
        while True:
            page = self._page(params)
            seen.extend(item["id"] for item in page.items)
            if page.next_cursor is None:
                break
            params = {"limit": "2", "cursor": page.next_cursor}
        assert seen == [item["id"] for item in OBJECTS]

    def test_last_page_has_no_next_cursor(self) -> None:
        page = self._page({"limit": "100"})
        assert page.next_cursor is None
        assert page.has_more is False
        assert len(page.items) == 7

    def test_unparseable_cursor_serves_the_first_page_terminally(self) -> None:
        page = self._page({"cursor": "!!!not-a-token!!!", "limit": "2"})
        assert [item["id"] for item in page.items] == ["obj-01", "obj-02"]
        assert page.next_cursor is None

    def test_cursor_past_the_end_is_an_empty_terminal_page(self) -> None:
        far = cursor_page(
            OBJECTS[:1],
            FakeRequest(
                method="GET",
                path="/a",
                params={"cursor": fake_source._encode_cursor(999), "limit": "5"},
                query={},
                path_params={},
                headers={},
                body=b"",
            ),
        )
        assert far.items == []
        assert far.next_cursor is None

    def test_custom_encode_decode_reproduces_a_solr_style_mark(self) -> None:
        page = self._page(
            {"limit": "2"},
            encode=lambda offset: f"off:{offset}",
            decode=lambda token: int(token.split(":", 1)[1]),
        )
        assert page.next_cursor == "off:2"
        second = self._page(
            {"limit": "2", "cursor": "off:2"},
            encode=lambda offset: f"off:{offset}",
            decode=lambda token: int(token.split(":", 1)[1]),
        )
        assert [item["id"] for item in second.items] == ["obj-03", "obj-04"]

    def test_a_raising_custom_decode_serves_the_first_page_terminally(self) -> None:
        page = self._page(
            {"cursor": "garbage", "limit": "2"},
            decode=lambda token: int(token.split(":", 1)[1]),
        )
        assert [item["id"] for item in page.items] == ["obj-01", "obj-02"]
        assert page.next_cursor is None

    def test_bad_limit_and_max_limit(self) -> None:
        assert self._page({"limit": "abc"}, default_limit=3).limit == 3
        assert self._page({"limit": "-1"}, default_limit=3).limit == 3
        assert self._page({"limit": "50"}, max_limit=2).limit == 2


class TestCursorDecodeFailureIsTerminal:
    """A broken cursor token must fail an assertion, never hang a loop."""

    def _page(self, params: dict, **kwargs) -> CursorPage:
        request = FakeRequest(
            method="GET",
            path="/a",
            params=params,
            query={},
            path_params={},
            headers={},
            body=b"",
        )
        return cursor_page(OBJECTS, request, **kwargs)

    def test_an_undecodable_token_serves_a_terminal_page(self) -> None:
        page = self._page({"cursor": "!!!not-a-token!!!", "limit": "2"})
        assert [item["id"] for item in page.items] == ["obj-01", "obj-02"]
        assert page.next_cursor is None

    def test_a_client_loop_resending_a_bad_token_terminates(self) -> None:
        def bad_decode(token: str) -> int:
            raise ValueError(token)

        pages = 0
        params = {"cursor": "bzoz", "limit": "2"}
        while True:
            page = self._page(params, decode=bad_decode)
            pages += 1
            assert pages < 10, "cursor_page allowed an infinite pagination loop"
            if page.next_cursor is None:
                break
            params = {"cursor": page.next_cursor, "limit": "2"}
        assert pages == 1


class TestCursorPageLimitValidation:
    """Non-positive limits are fixture misconfiguration, rejected up front."""

    def _request(self, params: dict) -> FakeRequest:
        return FakeRequest(
            method="GET",
            path="/a",
            params=params,
            query={},
            path_params={},
            headers={},
            body=b"",
        )

    def test_zero_max_limit_is_rejected(self) -> None:
        with pytest.raises(CursorPageLimitError, match="max_limit"):
            cursor_page(OBJECTS, self._request({"limit": "2"}), max_limit=0)

    def test_negative_max_limit_is_rejected(self) -> None:
        with pytest.raises(CursorPageLimitError, match="max_limit"):
            cursor_page(OBJECTS, self._request({"limit": "10"}), max_limit=-3)

    def test_non_positive_default_limit_is_rejected(self) -> None:
        with pytest.raises(CursorPageLimitError, match="default_limit"):
            cursor_page(OBJECTS, self._request({}), default_limit=0)

    def test_non_positive_request_limit_still_falls_back(self) -> None:
        page = cursor_page(OBJECTS, self._request({"limit": "-1"}), default_limit=3)
        assert page.limit == 3
