"""The testcontainers equivalent for sources that cannot be containerized.

Most connectors get their integration source from a container: a real Postgres,
a real Kafka, started by testcontainers and torn down after the session. SaaS and
on-prem HTTP sources — MicroStrategy, NetSuite, PowerCenter, SSRS — have no image
to pull, so their suites instead stand up a local HTTP server that replays
reconstructed responses. Four connectors wrote that server independently, and all
four wrote the same thing: a stdlib :class:`~http.server.ThreadingHTTPServer` on
an ephemeral loopback port, a hand-rolled path dispatch, a JSON writer, a
silenced access log, and a thread they remember to join on teardown.

:class:`HttpFakeSource` is that plumbing, once. It occupies exactly the slot a
testcontainer occupies — a session-scoped fixture yielding a base URL — so a
connector's integration tier reads the same whether its source is a container or
a fake::

    @pytest.fixture(scope="session")
    def source_url() -> Iterator[str]:
        fake = HttpFakeSource()
        fake.route(r"/api/v1/objects", list_objects)
        fake.route(r"/api/v1/objects/(?P<object_id>[^/]+)", get_object)
        with fake:
            yield fake.base_url

What stays with the connector is the part that is genuinely per-source: the
endpoint map, the response envelope, and any auth-signature scheme. Those are
irreducible — a NetSuite swagger fragment and a MicroStrategy folder listing have
nothing in common — so the connector supplies handlers and this module supplies
everything beneath them.

Two behaviours here are load-bearing rather than cosmetic:

*Catch-all fast 404.* Every method verb is bound, and any path no route matches
gets an immediate 404 instead of falling through. A connector's client calling an
endpoint the fake does not model then fails fast with a status code, rather than
blocking on a socket until the suite's timeout — which is how this failure mode
presents when the server only implements ``do_GET``.

*Unmatched-request recording.* The 404s are kept, so a test can assert the extract
called nothing the fake did not model. That assertion is what makes a
reverse-engineered fake evidence rather than a tautology, and
:func:`assert_extract_roundtrip` makes it for you.

Stdlib only, by constraint: ``pytest-httpserver`` and ``respx`` are not fleet
dependencies, and adding one would make these suites unrunnable in normal CI.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

from application_sdk.observability.logger_adaptor import get_logger

__all__ = [
    "CursorPage",
    "FakeRequest",
    "FakeResponse",
    "FakeSourceGroup",
    "HttpFakeSource",
    "OffsetPage",
    "assert_extract_roundtrip",
    "cursor_page",
    "offset_page",
    "serve",
]

logger = get_logger(__name__)

_LOOPBACK = "127.0.0.1"
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_MAX_BODY_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class FakeRequest:
    """One inbound request, parsed into the pieces a handler actually wants.

    ``path_params`` holds the route pattern's named groups, so a handler reads
    ``request.path_params["object_id"]`` rather than re-splitting the path.
    ``params`` is the flattened query string (first value per key) because that is
    what almost every handler needs; ``query`` keeps the full multi-value mapping
    for the rare repeated parameter.
    """

    method: str
    path: str
    params: Mapping[str, str]
    query: Mapping[str, Sequence[str]]
    path_params: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes

    def json(self, default: Any = None) -> Any:
        """The request body decoded as JSON, or ``default`` if absent/undecodable."""
        if not self.body:
            return default
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            return default

    def param(self, name: str, default: str | None = None) -> str | None:
        """Query parameter ``name``, or ``default`` when absent or empty."""
        value = self.params.get(name)
        return value if value else default

    def int_param(self, name: str, default: int) -> int:
        """Query parameter ``name`` as an int, falling back to ``default``.

        A malformed value falls back rather than raising: a fake source that 500s
        because a client sent ``limit=abc`` tells the connector's author nothing
        about their extract, and the real source would coerce or ignore it too.
        """
        raw = self.params.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def header(self, name: str, default: str | None = None) -> str | None:
        """Header ``name``, matched case-insensitively."""
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default


@dataclass(frozen=True)
class FakeResponse:
    """What a handler returns: a status, a body, and optional headers.

    A handler may return this, or any of the shorthands
    :meth:`HttpFakeSource.route` documents — ``dict``/``list`` for a 200 JSON
    body, ``(status, body)`` for an explicit status, or ``None`` for a 404.
    """

    status: int = 200
    body: Any = None
    content_type: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def json_(cls, body: Any, status: int = 200, **headers: str) -> FakeResponse:
        """A JSON response; ``body`` is serialised with :func:`json.dumps`."""
        return cls(
            status=status,
            body=body,
            content_type="application/json",
            headers=headers,
        )

    @classmethod
    def text(
        cls,
        body: str,
        status: int = 200,
        content_type: str = "text/plain; charset=utf-8",
        **headers: str,
    ) -> FakeResponse:
        """A text response, for sources that answer in XML, CSV or SOAP."""
        return cls(status=status, body=body, content_type=content_type, headers=headers)

    @classmethod
    def raw(
        cls,
        body: bytes,
        status: int = 200,
        content_type: str = "application/octet-stream",
        **headers: str,
    ) -> FakeResponse:
        """A byte-for-byte response, for a source whose payload must not be re-encoded."""
        return cls(status=status, body=body, content_type=content_type, headers=headers)

    def encode(self, default_content_type: str) -> tuple[bytes, str]:
        """Serialise the body, returning the bytes and the content type to send."""
        body = self.body
        content_type = self.content_type
        if isinstance(body, bytes):
            return body, content_type or "application/octet-stream"
        if isinstance(body, str):
            return body.encode(), content_type or "text/plain; charset=utf-8"
        if body is None:
            return b"", content_type or default_content_type
        return (
            json.dumps(body, default=str).encode(),
            content_type or default_content_type,
        )


Handler = Callable[[FakeRequest], Any]
Authorizer = Callable[[FakeRequest], Any]


@dataclass(frozen=True)
class _Route:
    pattern: re.Pattern[str]
    methods: frozenset[str]
    handler: Handler

    def matches_path(self, path: str) -> re.Match[str] | None:
        return self.pattern.fullmatch(path)


class HttpFakeSource:
    """A loopback HTTP server that replays a connector's reconstructed responses.

    Register routes, enter as a context manager, hand :attr:`base_url` to the
    connector's client. Routes are matched in registration order by
    :meth:`re.Pattern.fullmatch` against the path (no query string), and named
    groups arrive as ``request.path_params``.

    A handler may return a :class:`FakeResponse`, a ``dict``/``list`` (200 JSON),
    a ``str``/``bytes`` (200, default content type), a ``(status, body)`` tuple,
    or ``None`` for a 404 — so the common case stays a one-liner and the
    uncommon one is still expressible.

    Binding is always loopback: the server is reachable from the test process and
    from nothing else on the network.
    """

    def __init__(
        self,
        *,
        routes: Iterable[tuple[str, Handler]] = (),
        default_content_type: str = "application/json",
        not_found_body: Any = None,
        authorize: Authorizer | None = None,
        warn_unmatched: bool = True,
        name: str = "fake-source",
    ) -> None:
        self.name = name
        self.default_content_type = default_content_type
        self.not_found_body = (
            {"error": "not found"} if not_found_body is None else not_found_body
        )
        self.authorize = authorize
        self.warn_unmatched = warn_unmatched
        self._routes: list[_Route] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._requests: list[FakeRequest] = []
        self._unmatched: list[FakeRequest] = []
        self._hits: dict[int, int] = {}
        for pattern, handler in routes:
            self.route(pattern, handler)

    def route(
        self,
        pattern: str,
        handler: Handler,
        *,
        methods: Sequence[str] = ("GET",),
    ) -> Self:
        """Register ``handler`` for paths fully matching ``pattern``.

        Returns ``self``, so registrations chain. ``pattern`` is a regex matched
        against the path with :meth:`~re.Pattern.fullmatch`; use named groups for
        path parameters. Registration order is match order, so a specific route
        must be registered before a broader one that would also match it.
        """
        upper = frozenset(method.upper() for method in methods)
        if not upper:
            raise ValueError("route requires at least one HTTP method")
        self._routes.append(
            _Route(
                pattern=re.compile(pattern),
                methods=upper,
                handler=handler,
            )
        )
        return self

    @property
    def routes(self) -> Sequence[tuple[str, frozenset[str]]]:
        """The registered routes as ``(pattern, methods)``, in match order."""
        return [(route.pattern.pattern, route.methods) for route in self._routes]

    @property
    def base_url(self) -> str:
        """``http://127.0.0.1:<port>`` — the value a fixture yields.

        Only meaningful while the server is running; raises otherwise, because a
        base URL for a stopped server is a hang or a connection error later, far
        from its cause.
        """
        if self._server is None:
            raise RuntimeError(
                f"{self.name} is not running — use it as a context manager "
                "or call start() before reading base_url"
            )
        host, port = self._server.server_address[:2]
        host_text = host.decode() if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{port}"

    @property
    def port(self) -> int:
        """The ephemeral port the server bound."""
        if self._server is None:
            raise RuntimeError(f"{self.name} is not running")
        return int(self._server.server_address[1])

    @property
    def requests(self) -> Sequence[FakeRequest]:
        """Every request received, in arrival order — matched and unmatched."""
        with self._lock:
            return list(self._requests)

    @property
    def unmatched(self) -> Sequence[FakeRequest]:
        """Requests no route matched, i.e. every fast 404 this server served.

        Non-empty means the connector's extract called an endpoint the fake does
        not model, so whatever the test asserted was asserted against a 404.
        """
        with self._lock:
            return list(self._unmatched)

    def hits(self, pattern: str) -> int:
        """How many requests matched the route registered with ``pattern``."""
        with self._lock:
            return sum(
                count
                for index, count in self._hits.items()
                if self._routes[index].pattern.pattern == pattern
            )

    def unused_routes(self) -> Sequence[str]:
        """Patterns of routes no request ever matched.

        A route the extract never called is either dead fixture weight or a code
        path the test believes it covers and does not.
        """
        with self._lock:
            return [
                route.pattern.pattern
                for index, route in enumerate(self._routes)
                if not self._hits.get(index)
            ]

    def reset(self) -> None:
        """Forget all recorded requests and route hits, keeping the server up.

        The move that lets one session-scoped fake serve per-test assertions.
        """
        with self._lock:
            self._requests.clear()
            self._unmatched.clear()
            self._hits.clear()

    def start(self) -> Self:
        """Bind an ephemeral loopback port and serve in a daemon thread."""
        if self._server is not None:
            return self
        server = ThreadingHTTPServer((_LOOPBACK, 0), _make_handler_class(self))
        server.daemon_threads = True
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"{self.name}-server",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        return self

    def stop(self) -> None:
        """Shut the server down and join its thread. Idempotent."""
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _dispatch(self, request: FakeRequest) -> FakeResponse:
        with self._lock:
            self._requests.append(request)

        if self.authorize is not None:
            denied = _coerce(self.authorize(request))
            if denied is not None:
                return denied

        for index, route in enumerate(self._routes):
            match = route.matches_path(request.path)
            if match is None or request.method not in route.methods:
                continue
            with self._lock:
                self._hits[index] = self._hits.get(index, 0) + 1
            matched = FakeRequest(
                method=request.method,
                path=request.path,
                params=request.params,
                query=request.query,
                path_params=match.groupdict(),
                headers=request.headers,
                body=request.body,
            )
            try:
                response = _coerce(route.handler(matched))
            except Exception as exc:  # noqa: BLE001 — a handler bug must answer, not hang
                logger.warning(
                    "%s handler for %r raised: %r",
                    self.name,
                    route.pattern.pattern,
                    exc,
                    exc_info=True,
                )
                return FakeResponse.json_(
                    {"error": "fake source handler raised", "detail": repr(exc)},
                    status=500,
                )
            return response if response is not None else _not_found(self.not_found_body)

        with self._lock:
            self._unmatched.append(request)
        if self.warn_unmatched:
            logger.warning(
                "%s has no route for %s %s — answering 404",
                self.name,
                request.method,
                request.path,
            )
        return _not_found(self.not_found_body)


def _not_found(body: Any) -> FakeResponse:
    return FakeResponse(status=404, body=body)


def _coerce(result: Any) -> FakeResponse | None:
    """Normalise a handler's return value into a :class:`FakeResponse`, or ``None``."""
    if result is None:
        return None
    if isinstance(result, FakeResponse):
        return result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
        return FakeResponse(status=result[0], body=result[1])
    return FakeResponse(status=200, body=result)


def _make_handler_class(fake: HttpFakeSource) -> type[BaseHTTPRequestHandler]:
    """Build the handler class bound to one :class:`HttpFakeSource`.

    Every verb in :data:`_METHODS` is bound to the same dispatcher. That is the
    catch-all: a ``POST`` to a GET-only fake gets a 404, not a 501 with a body
    the connector's client may not expect — and never a socket that just sits
    there while the client waits for a response that no ``do_POST`` will write.
    """

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "HttpFakeSource/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Silenced: the default access log is stderr noise under pytest."""

        def _handle(self) -> None:
            split = urlsplit(self.path)
            query = parse_qs(split.query, keep_blank_values=True)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, _MAX_BODY_BYTES)) if length > 0 else b""
            request = FakeRequest(
                method=self.command.upper(),
                path=split.path,
                params={key: values[0] for key, values in query.items() if values},
                query=query,
                path_params={},
                headers=dict(self.headers.items()),
                body=body,
            )
            response = fake._dispatch(request)
            payload, content_type = response.encode(fake.default_content_type)
            if request.method == "HEAD":
                payload = b""
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            if payload:
                self.wfile.write(payload)

    for method in _METHODS:
        setattr(_Handler, f"do_{method}", _Handler._handle)
    return _Handler


class FakeSourceGroup:
    """Several fakes started together, for a source split across hosts.

    Some connectors authenticate against one host and read data from another. Each
    host is its own :class:`HttpFakeSource` on its own port; this starts and stops
    them as one unit and hands back the URLs by name::

        group = FakeSourceGroup(token=token_fake, data=data_fake)
        with group:
            client = Client(token_url=group.url("token"), data_url=group.url("data"))
    """

    def __init__(self, **sources: HttpFakeSource) -> None:
        if not sources:
            raise ValueError("FakeSourceGroup requires at least one source")
        self.sources: dict[str, HttpFakeSource] = dict(sources)

    def __getitem__(self, name: str) -> HttpFakeSource:
        return self.sources[name]

    def url(self, name: str) -> str:
        """Base URL of the named host."""
        return self.sources[name].base_url

    @property
    def base_urls(self) -> Mapping[str, str]:
        """Every host's base URL, keyed by name."""
        return {name: source.base_url for name, source in self.sources.items()}

    def reset(self) -> None:
        """Reset the recorded requests on every host."""
        for source in self.sources.values():
            source.reset()

    @property
    def unmatched(self) -> Sequence[FakeRequest]:
        """Unmatched requests across every host, so one assertion covers the group."""
        return [
            request for source in self.sources.values() for request in source.unmatched
        ]

    def __enter__(self) -> Self:
        started: list[HttpFakeSource] = []
        try:
            for source in self.sources.values():
                started.append(source.start())
        except Exception:
            for source in reversed(started):
                source.stop()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        for source in reversed(list(self.sources.values())):
            source.stop()


@contextmanager
def serve(
    routes: Iterable[tuple[str, Handler]] = (),
    **kwargs: Any,
) -> Iterator[str]:
    """Run a fake for the duration of the block, yielding just its ``base_url``.

    The one-liner form, for a fixture that needs nothing but the URL::

        @pytest.fixture(scope="session")
        def source_url() -> Iterator[str]:
            with serve([(r"/api/objects", list_objects)]) as url:
                yield url

    Keep the :class:`HttpFakeSource` itself when the test asserts on what was
    called — the recordings live on the instance, not the URL.
    """
    fake = HttpFakeSource(routes=routes, **kwargs)
    with fake:
        yield fake.base_url


@dataclass(frozen=True)
class OffsetPage:
    """One page of an offset/limit scheme, plus everything an envelope needs.

    The envelope stays with the connector — one source calls the next-page field
    ``nextOffset``, another nests the count under ``meta.total`` — so this carries
    the numbers and the connector spells them.
    """

    items: Sequence[Any]
    offset: int
    limit: int
    total: int

    @property
    def has_more(self) -> bool:
        """Whether any item remains after this page."""
        return self.offset + len(self.items) < self.total

    @property
    def next_offset(self) -> int | None:
        """Offset of the next page, or ``None`` on the last one."""
        return self.offset + len(self.items) if self.has_more else None


@dataclass(frozen=True)
class CursorPage:
    """One page of a cursor scheme; ``next_cursor`` is opaque, as a real one is.

    The default token encodes only a position, but as base64 of an internal form
    rather than a bare integer — a connector that parses the cursor instead of
    echoing it back would pass against a fake that handed out plain offsets and
    then fail against the real source. Pass ``encode``/``decode`` to
    :func:`cursor_page` when the captured traffic shows a specific token format
    the connector's loop depends on.

    ``next_cursor`` is ``None`` on the last page. Some sources instead signal
    exhaustion by echoing the *same* cursor back, and their clients loop until the
    token stops changing; spell that in the envelope with
    ``page.next_cursor or request.param("cursor")`` rather than changing the page.
    """

    items: Sequence[Any]
    limit: int
    total: int
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        """Whether any item remains after this page."""
        return self.next_cursor is not None


def offset_page(
    items: Sequence[Any],
    request: FakeRequest,
    *,
    offset_param: str = "offset",
    limit_param: str = "limit",
    default_limit: int = 100,
    max_limit: int | None = None,
) -> OffsetPage:
    """Slice ``items`` per the request's offset/limit parameters.

    Negative offsets and non-positive limits are clamped rather than rejected: a
    fake that 400s on a client's off-by-one hides the extract bug behind an error
    the real source would not return.
    """
    total = len(items)
    offset = max(0, request.int_param(offset_param, 0))
    limit = request.int_param(limit_param, default_limit)
    if limit <= 0:
        limit = default_limit
    if max_limit is not None:
        limit = min(limit, max_limit)
    return OffsetPage(
        items=list(items[offset : offset + limit]),
        offset=offset,
        limit=limit,
        total=total,
    )


def cursor_page(
    items: Sequence[Any],
    request: FakeRequest,
    *,
    cursor_param: str = "cursor",
    limit_param: str = "limit",
    default_limit: int = 100,
    max_limit: int | None = None,
    encode: Callable[[int], str] | None = None,
    decode: Callable[[str], int] | None = None,
) -> CursorPage:
    """Slice ``items`` per the request's cursor/limit parameters.

    An absent cursor is the first page. An unparseable cursor also starts from the
    beginning, because a real source's answer to a stale token is a fresh page far
    more often than a 400, and a hard failure here would be indistinguishable from
    the connector never having sent a cursor at all.

    ``encode``/``decode`` override the token format when the connector's client
    depends on the shape the real source emits — a Solr-style ``off:<n>`` mark,
    say. They are position-in/position-out; ``decode`` returning anything
    unparseable is treated as the first page.
    """
    encode_token = _encode_cursor if encode is None else encode
    decode_token = _decode_cursor if decode is None else decode
    total = len(items)
    start = _decode_with(decode_token, request.param(cursor_param))
    start = min(max(0, start), total)
    limit = request.int_param(limit_param, default_limit)
    if limit <= 0:
        limit = default_limit
    if max_limit is not None:
        limit = min(limit, max_limit)
    page = list(items[start : start + limit])
    end = start + len(page)
    return CursorPage(
        items=page,
        limit=limit,
        total=total,
        next_cursor=encode_token(end) if end < total else None,
    )


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _decode_with(decode: Callable[[str], int], cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return decode(cursor)
    except Exception:  # noqa: BLE001 — any token a caller's decoder rejects is page one
        return 0


def _decode_cursor(cursor: str) -> int:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return 0
    if not decoded.startswith("o:"):
        return 0
    try:
        return int(decoded[2:])
    except ValueError:
        return 0


def assert_extract_roundtrip(
    fake: HttpFakeSource | FakeSourceGroup,
    extract_fn: Callable[..., Any],
    golden: Any,
    *,
    key: Callable[[Any], Any] | None = None,
    normalise: Callable[[Any], Any] | None = None,
    require_all_routes_used: bool = True,
) -> Any:
    """Run the real extract against the fake and assert it reproduces ``golden``.

    A fake source reconstructed from captured traffic is only evidence if the
    connector's own extract, unmodified, turns the fake's responses back into the
    raw output that was captured. Asserting that is what separates a fake that
    proves the extract works from one that merely agrees with itself.

    Three things are checked, and the second is the one usually forgotten:

    1. the extract's output equals ``golden``;
    2. the fake served no unmatched request — otherwise part of the extract ran
       against a 404 and the comparison silently covered less than it appears to;
    3. every registered route was used (``require_all_routes_used``) — an unused
       route is a code path the suite is believed to cover and does not.

    ``extract_fn`` is called with the base URL (a single fake) or with the host
    URLs as keyword arguments (a :class:`FakeSourceGroup`). ``key`` sorts both
    sides before comparison, for an extract whose record order is not guaranteed;
    ``normalise`` is applied to both sides, to drop fields that cannot be stable
    (timestamps, run ids). Returns the extract's output.
    """
    if isinstance(fake, FakeSourceGroup):
        actual = extract_fn(**dict(fake.base_urls))
    elif isinstance(fake, HttpFakeSource):
        actual = extract_fn(fake.base_url)
    else:
        raise TypeError(
            "fake must be an HttpFakeSource or a FakeSourceGroup, "
            f"got {type(fake).__name__}"
        )

    unmatched = list(fake.unmatched)
    if unmatched:
        calls = ", ".join(
            f"{request.method} {request.path}" for request in unmatched[:10]
        )
        raise AssertionError(
            f"extract called {len(unmatched)} endpoint(s) the fake source does not "
            f"model, so part of it ran against a 404: {calls}"
        )

    if require_all_routes_used:
        unused: list[str] = []
        if isinstance(fake, FakeSourceGroup):
            for name, host in fake.sources.items():
                unused.extend(f"{name}:{pattern}" for pattern in host.unused_routes())
        else:
            unused.extend(fake.unused_routes())
        if unused:
            raise AssertionError(
                "extract never called these fake-source routes, so they cover "
                f"nothing: {', '.join(unused)}"
            )

    left, right = actual, golden
    if normalise is not None:
        left, right = normalise(left), normalise(right)
    if key is not None:
        left, right = _sorted_by(left, key), _sorted_by(right, key)
    if left != right:
        raise AssertionError(
            "extract output over the fake source does not match golden raw output\n"
            f"{_diff(left, right)}"
        )
    return actual


def _sorted_by(value: Any, key: Callable[[Any], Any]) -> Any:
    if isinstance(value, (list, tuple)):
        return sorted(value, key=key)
    return value


def _diff(actual: Any, expected: Any) -> str:
    """A compact, readable difference for the assertion message."""
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        lines = [f"actual has {len(actual)} record(s), golden has {len(expected)}"]
        for index, (left, right) in enumerate(zip(actual, expected)):
            if left != right:
                lines.append(f"first difference at index {index}:")
                lines.append(f"  actual:   {left!r}")
                lines.append(f"  expected: {right!r}")
                break
        return "\n".join(lines)
    return f"actual:   {actual!r}\nexpected: {expected!r}"
