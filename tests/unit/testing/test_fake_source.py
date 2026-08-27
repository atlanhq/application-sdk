"""Unit tests for the HttpFakeSource primitive.

All fixture data here is synthetic — this is a public repo, so no captured
customer payloads, hostnames or object ids appear.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from application_sdk.testing import fake_source
from application_sdk.testing.fake_source import (
    CursorPage,
    FakeRequest,
    FakeResponse,
    FakeSourceGroup,
    HttpFakeSource,
    OffsetPage,
    assert_extract_roundtrip,
    cursor_page,
    offset_page,
    serve,
)

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

    def test_base_url_before_start_is_an_error_not_a_hang(self) -> None:
        source = HttpFakeSource()
        with pytest.raises(RuntimeError, match="not running"):
            _ = source.base_url
        with pytest.raises(RuntimeError, match="not running"):
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
        with pytest.raises(RuntimeError):
            _ = source.base_url

    def test_serve_contextmanager_yields_a_working_base_url(self) -> None:
        with serve([(r"/ping", lambda _r: {"ok": True})]) as url:
            assert _json(f"{url}/ping") == (200, {"ok": True})


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
        with pytest.raises(ValueError, match="at least one HTTP method"):
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
            assert conn.getresponse().read() == b'{"body": "payload"}'
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


class TestOffsetPagination:
    def _page(self, params: dict[str, str], **kwargs: Any) -> OffsetPage:
        request = FakeRequest(
            method="GET",
            path="/api/objects",
            params=params,
            query={},
            path_params={},
            headers={},
            body=b"",
        )
        return offset_page(OBJECTS, request, **kwargs)

    def test_first_page_and_has_more(self) -> None:
        page = self._page({"offset": "0", "limit": "3"})
        assert [item["id"] for item in page.items] == ["obj-01", "obj-02", "obj-03"]
        assert (page.offset, page.limit, page.total) == (0, 3, 7)
        assert page.has_more is True
        assert page.next_offset == 3

    def test_last_partial_page_terminates(self) -> None:
        page = self._page({"offset": "6", "limit": "3"})
        assert [item["id"] for item in page.items] == ["obj-07"]
        assert page.has_more is False
        assert page.next_offset is None

    def test_offset_past_the_end_is_an_empty_terminal_page(self) -> None:
        page = self._page({"offset": "99", "limit": "3"})
        assert list(page.items) == []
        assert page.has_more is False

    def test_full_traversal_visits_every_item_exactly_once(self) -> None:
        seen: list[str] = []
        offset: int | None = 0
        while offset is not None:
            page = self._page({"offset": str(offset), "limit": "2"})
            seen.extend(item["id"] for item in page.items)
            offset = page.next_offset
        assert seen == [item["id"] for item in OBJECTS]

    def test_defaults_apply_when_params_absent(self) -> None:
        page = self._page({}, default_limit=5)
        assert (page.offset, page.limit) == (0, 5)
        assert len(page.items) == 5

    def test_negative_offset_and_bad_limit_are_clamped_not_rejected(self) -> None:
        page = self._page({"offset": "-4", "limit": "0"}, default_limit=2)
        assert page.offset == 0
        assert page.limit == 2

    def test_max_limit_caps_a_client_asking_for_too_much(self) -> None:
        page = self._page({"limit": "1000"}, max_limit=4)
        assert page.limit == 4
        assert len(page.items) == 4

    def test_custom_param_names(self) -> None:
        page = self._page(
            {"start": "2", "count": "2"}, offset_param="start", limit_param="count"
        )
        assert [item["id"] for item in page.items] == ["obj-03", "obj-04"]


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

    def test_unparseable_cursor_restarts_rather_than_erroring(self) -> None:
        page = self._page({"cursor": "!!!not-a-token!!!", "limit": "2"})
        assert [item["id"] for item in page.items] == ["obj-01", "obj-02"]

    def test_cursor_past_the_end_is_an_empty_terminal_page(self) -> None:
        far = cursor_page(
            OBJECTS[:1],
            FakeRequest(
                method="GET",
                path="/a",
                params={"limit": "5"},
                query={},
                path_params={},
                headers={},
                body=b"",
            ),
        )
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

    def test_a_raising_custom_decode_falls_back_to_the_first_page(self) -> None:
        page = self._page(
            {"cursor": "garbage", "limit": "2"},
            decode=lambda token: int(token.split(":", 1)[1]),
        )
        assert [item["id"] for item in page.items] == ["obj-01", "obj-02"]

    def test_bad_limit_and_max_limit(self) -> None:
        assert self._page({"limit": "abc"}, default_limit=3).limit == 3
        assert self._page({"limit": "-1"}, default_limit=3).limit == 3
        assert self._page({"limit": "50"}, max_limit=2).limit == 2


class TestFakeSourceGroup:
    def _group(self) -> FakeSourceGroup:
        token = HttpFakeSource(name="token-host")
        token.route(r"/oauth/token", lambda _r: {"access_token": "synthetic-token"})
        data = HttpFakeSource(name="data-host")
        data.route(r"/api/objects", lambda _r: {"items": OBJECTS})
        return FakeSourceGroup(token=token, data=data)

    def test_each_host_gets_its_own_port(self) -> None:
        with self._group() as group:
            assert group.url("token") != group.url("data")
            assert set(group.base_urls) == {"token", "data"}

    def test_both_hosts_serve_their_own_routes(self) -> None:
        with self._group() as group:
            assert _json(f"{group.url('token')}/oauth/token")[1] == {
                "access_token": "synthetic-token"
            }
            assert _json(f"{group.url('data')}/api/objects")[0] == 200
            assert _json(f"{group.url('token')}/api/objects")[0] == 404

    def test_getitem_reaches_the_underlying_source(self) -> None:
        with self._group() as group:
            assert group["data"].name == "data-host"

    def test_unmatched_aggregates_across_hosts(self) -> None:
        with self._group() as group:
            _json(f"{group.url('token')}/absent-a")
            _json(f"{group.url('data')}/absent-b")
            assert sorted(r.path for r in group.unmatched) == [
                "/absent-a",
                "/absent-b",
            ]

    def test_reset_clears_every_host(self) -> None:
        with self._group() as group:
            _json(f"{group.url('data')}/api/objects")
            group.reset()
            assert list(group["data"].requests) == []

    def test_exit_stops_every_host(self) -> None:
        group = self._group()
        with group:
            pass
        for source in group.sources.values():
            with pytest.raises(RuntimeError):
                _ = source.base_url

    def test_empty_group_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one source"):
            FakeSourceGroup()


GOLDEN = [
    {"id": "obj-01", "name": "object 01"},
    {"id": "obj-02", "name": "object 02"},
]


def _extract_all(base_url: str, *, limit: int = 1) -> list[dict[str, Any]]:
    """A stand-in connector extract: pages the fake until exhausted."""
    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        _, payload = _json(f"{base_url}/api/objects?offset={offset}&limit={limit}")
        records.extend(payload["items"])
        if payload["nextOffset"] is None:
            return records
        offset = payload["nextOffset"]


def _paged_source(items: list[dict[str, Any]]) -> HttpFakeSource:
    def list_objects(request: FakeRequest) -> Any:
        page = offset_page(items, request, default_limit=1)
        return {"items": list(page.items), "nextOffset": page.next_offset}

    return HttpFakeSource(routes=[(r"/api/objects", list_objects)], name="roundtrip")


class TestAssertExtractRoundtrip:
    def test_passes_when_the_extract_reproduces_golden(self) -> None:
        with _paged_source(GOLDEN) as fake:
            actual = assert_extract_roundtrip(fake, _extract_all, GOLDEN)
        assert actual == GOLDEN

    def test_fails_when_output_differs_from_golden(self) -> None:
        with (
            _paged_source([{"id": "obj-01", "name": "drifted"}]) as fake,
            pytest.raises(AssertionError, match="does not match golden"),
        ):
            assert_extract_roundtrip(fake, _extract_all, GOLDEN)

    def test_fails_when_the_extract_called_an_unmodelled_endpoint(self) -> None:
        def extract(base_url: str) -> list[dict[str, Any]]:
            _json(f"{base_url}/api/objects?offset=0&limit=99")
            _json(f"{base_url}/api/not/modelled")
            return GOLDEN

        with (
            _paged_source(GOLDEN) as fake,
            pytest.raises(AssertionError, match="does not model"),
        ):
            assert_extract_roundtrip(fake, extract, GOLDEN)

    def test_unmatched_check_precedes_the_equality_check(self) -> None:
        """A 404'd call must be reported even when the output happens to match."""

        def extract(base_url: str) -> list[dict[str, Any]]:
            _json(f"{base_url}/api/objects?offset=0&limit=99")
            _json(f"{base_url}/api/absent")
            return GOLDEN

        with (
            _paged_source(GOLDEN) as fake,
            pytest.raises(AssertionError, match="ran against a 404"),
        ):
            assert_extract_roundtrip(fake, extract, GOLDEN)

    def test_fails_when_a_route_was_never_used(self) -> None:
        with _paged_source(GOLDEN) as fake:
            fake.route(r"/api/never-called", lambda _r: {})
            with pytest.raises(AssertionError, match="never called these"):
                assert_extract_roundtrip(fake, _extract_all, GOLDEN)

    def test_unused_route_check_can_be_waived(self) -> None:
        with _paged_source(GOLDEN) as fake:
            fake.route(r"/api/never-called", lambda _r: {})
            assert_extract_roundtrip(
                fake, _extract_all, GOLDEN, require_all_routes_used=False
            )

    def test_key_sorts_both_sides_before_comparing(self) -> None:
        with _paged_source(list(reversed(GOLDEN))) as fake:
            assert_extract_roundtrip(
                fake, _extract_all, GOLDEN, key=lambda record: record["id"]
            )

    def test_normalise_drops_fields_that_cannot_be_reconstructed(self) -> None:
        served = [dict(record, source_url="http://fake-host/x") for record in GOLDEN]

        def drop_source_url(records: Any) -> Any:
            return [
                {k: v for k, v in record.items() if k != "source_url"}
                for record in records
            ]

        with _paged_source(served) as fake:
            assert_extract_roundtrip(
                fake, _extract_all, GOLDEN, normalise=drop_source_url
            )

    def test_works_with_a_group_passing_urls_as_kwargs(self) -> None:
        token = HttpFakeSource(name="token-host")
        token.route(r"/oauth/token", lambda _r: {"access_token": "synthetic-token"})
        group = FakeSourceGroup(token=token, data=_paged_source(GOLDEN))

        def extract(*, token: str, data: str) -> list[dict[str, Any]]:
            _json(f"{token}/oauth/token")
            return _extract_all(data)

        with group:
            assert assert_extract_roundtrip(group, extract, GOLDEN) == GOLDEN

    def test_rejects_a_non_fake_first_argument(self) -> None:
        with pytest.raises(TypeError, match="HttpFakeSource"):
            assert_extract_roundtrip("not-a-fake", _extract_all, GOLDEN)  # type: ignore[arg-type]

    def test_diff_message_reports_the_first_differing_index(self) -> None:
        served = [GOLDEN[0], {"id": "obj-02", "name": "drifted"}]
        with (
            _paged_source(served) as fake,
            pytest.raises(AssertionError, match="first difference at index 1"),
        ):
            assert_extract_roundtrip(fake, _extract_all, GOLDEN)

    def test_diff_message_handles_non_sequence_output(self) -> None:
        source = HttpFakeSource(routes=[(r"/api/one", lambda _r: {"count": 1})])
        with source, pytest.raises(AssertionError, match="expected:"):
            assert_extract_roundtrip(
                source,
                lambda url: _json(f"{url}/api/one")[1],
                {"count": 2},
            )


class TestSessionScopedFixtureShape:
    """The primitive must survive the fixture slot a testcontainer occupies."""

    def test_one_server_serves_many_sequential_tests_after_reset(self) -> None:
        with _paged_source(GOLDEN) as fake:
            for _ in range(3):
                fake.reset()
                assert_extract_roundtrip(fake, _extract_all, GOLDEN)

    def test_concurrent_clients_are_served(self) -> None:
        import concurrent.futures

        with _paged_source(GOLDEN) as fake:
            url = fake.base_url
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(
                    pool.map(lambda _n: _extract_all(url), range(8)),
                )
        assert all(result == GOLDEN for result in results)
        assert len(fake.requests) == 8 * len(GOLDEN)
