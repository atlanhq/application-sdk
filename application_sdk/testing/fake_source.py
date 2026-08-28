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
testcontainer occupies — the kit's session-scoped ``integration_source`` — so a
connector's integration tier reads the same whether its source is a container or
a fake::

    @pytest.fixture(scope="session")
    def integration_source(http_fake_source_factory) -> HttpFakeSource:
        fake = http_fake_source_factory(name="my-source")
        fake.route(r"/api/v1/objects", list_objects)
        fake.route(r"/api/v1/objects/(?P<object_id>[^/]+)", get_object)
        return fake

``http_fake_source_factory`` in :mod:`application_sdk.testing.integration.fixtures`
owns the session lifecycle, and the autouse ``reset_http_fake_sources`` alongside
it clears every fake's per-test recordings, leaving the connector only its routes.
Both arrive with the kit's star-import, so there is no second place to wire them
up from.

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
reverse-engineered fake evidence rather than a tautology.

Stdlib only, by constraint: ``pytest-httpserver`` and ``respx`` are not fleet
dependencies, and adding one would make these suites unrunnable in normal CI.

:data:`_RESERVED_RESPONSE_HEADERS` names the framing and hop-by-hop headers the
server owns. A handler that replays a captured response verbatim will carry them,
and emitting them alongside the server's own ``Content-Length`` puts two
conflicting values on the wire: a client that honours the last one waits for a
body that never arrives.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

import orjson

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing._errors import (
    FakeSourceNotRunningError,
    FakeSourceRouteError,
)

__all__ = [
    "Authorizer",
    "FakeRequest",
    "FakeResponse",
    "FakeSourceNotRunningError",
    "FakeSourceRouteError",
    "Handler",
    "HandlerResult",
    "HttpFakeSource",
    "HttpFakeSourceFactory",
]

logger = get_logger(__name__)

_LOOPBACK = "127.0.0.1"
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_CONNECTION_TIMEOUT_SECONDS = 5.0
_JOIN_GRACE_SECONDS = 5.0
_MAX_BODY_BYTES = 64 * 1024 * 1024
_MAX_CHUNK_LINE_BYTES = 8192
_RESERVED_RESPONSE_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection"}
)

_BODY_REFUSALS = {
    400: {"error": "malformed request body"},
    413: {"error": "request body too large"},
}


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

    def json(self, default: object = None) -> object:
        """The request body decoded as JSON, or ``default`` if absent/undecodable."""
        if not self.body:
            return default
        try:
            return orjson.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            return default  # conformance: ignore[E007] a malformed body is the handler's call, not the fake's: it branches on ``default`` to pick a status code, and a log here would fire on every deliberate negative-path test

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
            return default  # conformance: ignore[E007] documented above: a malformed query value falls back the way the real source would, and the handler sees only the default

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

    The primary constructor takes headers as a mapping only. The classmethod
    constructors (:meth:`json_`, :meth:`text`, :meth:`raw`) take them two ways: a
    ``headers`` mapping and keyword arguments. The mapping is the general form — a
    real source's header names are routinely not Python identifiers, and
    ``X-Some-Token`` cannot be spelled as a keyword at all — while the keyword form
    stays the shorthand for the names that happen to be identifiers. Given the same
    key both ways, the keyword wins.
    """

    status: int = 200
    body: object = None
    content_type: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def json_(
        cls,
        body: object,
        status: int = 200,
        *,
        headers: Mapping[str, str] | None = None,
        **header_kwargs: str,
    ) -> FakeResponse:
        """A JSON response; ``body`` is serialised with :func:`orjson.dumps`."""
        return cls(
            status=status,
            body=body,
            content_type="application/json",
            headers=_merge_headers(headers, header_kwargs),
        )

    @classmethod
    def text(
        cls,
        body: str,
        status: int = 200,
        content_type: str = "text/plain; charset=utf-8",
        *,
        headers: Mapping[str, str] | None = None,
        **header_kwargs: str,
    ) -> FakeResponse:
        """A text response, for sources that answer in XML, CSV or SOAP."""
        return cls(
            status=status,
            body=body,
            content_type=content_type,
            headers=_merge_headers(headers, header_kwargs),
        )

    @classmethod
    def raw(
        cls,
        body: bytes,
        status: int = 200,
        content_type: str = "application/octet-stream",
        *,
        headers: Mapping[str, str] | None = None,
        **header_kwargs: str,
    ) -> FakeResponse:
        """A byte-for-byte response, for a source whose payload must not be re-encoded."""
        return cls(
            status=status,
            body=body,
            content_type=content_type,
            headers=_merge_headers(headers, header_kwargs),
        )

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
            # OPT_NON_STR_KEYS keeps stdlib json's coercion of non-str dict keys:
            # a handler replaying a captured payload keyed by int would otherwise
            # raise where it previously serialised.
            orjson.dumps(body, default=str, option=orjson.OPT_NON_STR_KEYS),
            content_type or default_content_type,
        )


HandlerResult = (
    FakeResponse
    | Mapping[str, object]
    | Sequence[object]
    | str
    | bytes
    | tuple[int, object]
    | None
)
"""Everything :func:`_coerce` accepts from a handler or authorizer."""

Handler = Callable[[FakeRequest], HandlerResult]
Authorizer = Callable[[FakeRequest], HandlerResult]


def _merge_headers(
    headers: Mapping[str, str] | None, header_kwargs: Mapping[str, str]
) -> Mapping[str, str]:
    if not headers:
        return dict(header_kwargs)
    merged = dict(headers)
    merged.update(header_kwargs)
    return merged


def _candidate_paths(path: str) -> tuple[str, ...]:
    """The path as sent, then the same path without a single trailing slash."""
    if len(path) > 1 and path.endswith("/") and not path.endswith("//"):
        return (path, path[:-1])
    return (path,)


@dataclass(frozen=True)
class _Route:
    pattern: re.Pattern[str]
    methods: frozenset[str]
    handler: Handler

    def matches_path(self, path: str) -> re.Match[str] | None:
        return self.pattern.fullmatch(path)


class _FakeSourceServer(ThreadingHTTPServer):
    """Tracks the per-connection handler threads it starts, so they can be joined.

    :class:`~socketserver.ThreadingMixIn` only remembers non-daemon threads, and
    these are daemons on purpose — a wedged fake must never keep the interpreter
    alive. Remembering them here is what lets :meth:`HttpFakeSource.stop` wait for
    them and report the ones that outlive their budget.
    """

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._handler_threads: set[threading.Thread] = set()
        self._handler_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        current = threading.current_thread()
        with self._handler_lock:
            self._handler_threads.add(current)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._handler_lock:
                self._handler_threads.discard(current)

    def handler_threads(self) -> list[threading.Thread]:
        with self._handler_lock:
            return list(self._handler_threads)


class HttpFakeSource:
    """A loopback HTTP server that replays a connector's reconstructed responses.

    Register routes, enter as a context manager, hand :attr:`base_url` to the
    connector's client. Routes are matched by :meth:`re.Pattern.fullmatch` against
    the path (no query string), and named groups arrive as ``request.path_params``.
    Ordering is precise: for each path candidate — the path exactly as sent, then
    the same path with a single trailing slash removed — the first registered
    route whose methods include the request's and whose pattern fullmatches wins.
    Only if no route matches the exact path is the normalised path tried at all.

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
        not_found_body: object = None,
        authorize: Authorizer | None = None,
        warn_unmatched: bool = True,
        name: str = "fake-source",
        connection_timeout: float = _CONNECTION_TIMEOUT_SECONDS,
    ) -> None:
        self.name = name
        self.default_content_type = default_content_type
        self.not_found_body = (
            {"error": "not found"} if not_found_body is None else not_found_body
        )
        self.authorize = authorize
        self.warn_unmatched = warn_unmatched
        self.connection_timeout = connection_timeout
        self._routes: list[_Route] = []
        self._server: _FakeSourceServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._requests: list[FakeRequest] = []
        self._unmatched: list[FakeRequest] = []
        self._hits: dict[int, int] = {}
        self._lifetime_hits: dict[int, int] = {}
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
        path parameters.

        **Trailing slash.** The path is tried as sent, then again without a single
        trailing slash. A source that tolerates ``/objects/`` for ``/objects``
        needs no ``/?`` in the pattern, and an exact match always beats the
        normalised retry, so a route written with a trailing slash still owns it.
        """
        upper = frozenset(method.upper() for method in methods)
        if not upper:
            raise FakeSourceRouteError(
                message="route requires at least one HTTP method"
            )
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
            raise FakeSourceNotRunningError(
                message=f"{self.name} is not running — use it as a context "
                "manager or call start() before reading base_url"
            )
        host, port = self._server.server_address[:2]
        host_text = host.decode() if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{port}"

    @property
    def port(self) -> int:
        """The ephemeral port the server bound."""
        if self._server is None:
            raise FakeSourceNotRunningError(message=f"{self.name} is not running")
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
        """Patterns of routes no request has matched for the life of this fake.

        A route the extract never called is either dead fixture weight or a code
        path the test believes it covers and does not.

        Deliberately reads a counter :meth:`reset` does not clear, because the two
        counters answer questions at different scopes. :meth:`hits` is per-test —
        "did *this* test call that endpoint?" — and must start at zero for every
        test. "Is this route dead fixture weight?" is per-suite, and can only be
        answered once every test has run. Reading the per-test counter here would
        report every route the *other* tests exercised as unused, which is a
        failure in any suite with more than one route-usage assertion.
        """
        with self._lock:
            return [
                route.pattern.pattern
                for index, route in enumerate(self._routes)
                if not self._lifetime_hits.get(index)
            ]

    def reset(self) -> None:
        """Forget this test's recorded requests and route hits, keeping the server up.

        The move that lets one session-scoped fake serve per-test assertions.
        :meth:`unused_routes` reads a separate lifetime counter and is unaffected;
        see its docstring for why the two scopes cannot share one counter.
        """
        with self._lock:
            self._requests.clear()
            self._unmatched.clear()
            self._hits.clear()

    def start(self) -> Self:
        """Bind an ephemeral loopback port and serve in a daemon thread."""
        if self._server is not None:
            return self
        server = _FakeSourceServer((_LOOPBACK, 0), _make_handler_class(self))
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
        """Shut the server down and wait for the threads it started. Idempotent.

        The per-connection handler threads are waited on too, not just the accept
        loop's. An idle keep-alive connection leaves its handler blocked in
        ``readline``, which ``shutdown()`` does nothing about — so the handler
        class carries a socket ``timeout`` (``connection_timeout``): the read wakes,
        the handler closes the connection, and the thread exits to be joined here.
        The wait is bounded by ``connection_timeout`` plus a grace period; any
        thread still alive when that budget runs out is logged by name and
        abandoned, not joined further.
        """
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None and thread is None:
            return
        pending: list[threading.Thread] = []
        if server is not None:
            server.shutdown()
            pending.extend(server.handler_threads())
            server.server_close()
        if thread is not None:
            pending.append(thread)
        budget = self.connection_timeout + _JOIN_GRACE_SECONDS
        deadline = time.monotonic() + budget
        for pending_thread in pending:
            pending_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        stranded = sorted(item.name for item in pending if item.is_alive())
        if stranded:
            logger.warning(
                "%s stop() left %d thread(s) running after %.1fs: %s",
                self.name,
                len(stranded),
                budget,
                ", ".join(stranded),
            )

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _dispatch(self, request: FakeRequest) -> FakeResponse:
        with self._lock:
            self._requests.append(request)

        if self.authorize is not None:
            try:
                denied = _coerce(self.authorize(request))
            except Exception as exc:
                logger.warning(
                    "%s authorize hook raised: %r", self.name, exc, exc_info=True
                )
                return FakeResponse.json_(
                    {"error": "fake source authorize raised", "detail": repr(exc)},
                    status=500,
                )
            if denied is not None:
                return denied

        selected = self._select(request)
        if selected is None:
            return self._record_unmatched(request)

        index, route, match = selected
        with self._lock:
            self._hits[index] = self._hits.get(index, 0) + 1
            self._lifetime_hits[index] = self._lifetime_hits.get(index, 0) + 1
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
        except Exception as exc:
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

    def _select(self, request: FakeRequest) -> tuple[int, _Route, re.Match[str]] | None:
        """The first registered route matching the path and method.

        The path as sent is resolved fully before the trailing-slash-stripped form
        is tried, so an exact match can never lose to a normalised one.
        """
        for path in _candidate_paths(request.path):
            for index, route in enumerate(self._routes):
                match = route.matches_path(path)
                if match is None or request.method not in route.methods:
                    continue
                return index, route, match
        return None

    def _record_unmatched(self, request: FakeRequest) -> FakeResponse:
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


def _not_found(body: object) -> FakeResponse:
    return FakeResponse(status=404, body=body)


def _coerce(result: HandlerResult) -> FakeResponse | None:
    """Normalise a handler's return value into a :class:`FakeResponse`, or ``None``.

    Raising inside a handler or authorizer is caught by the dispatcher and
    answered with a 500, so a bug in either answers the client instead of
    leaving it hanging on the socket.
    """
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

    The class-level ``timeout`` is the other half of a clean teardown: it puts a
    read deadline on each accepted connection, so a handler parked in ``readline``
    on an idle keep-alive socket wakes, closes, and lets its thread end instead of
    outliving :meth:`HttpFakeSource.stop`.
    """

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "HttpFakeSource/1.0"
        timeout = fake.connection_timeout

        def log_message(self, format: str, *args: Any) -> None:
            """Silenced: the default access log is stderr noise under pytest."""

        def _read_chunked_body(self) -> bytes | None:
            """Decode a ``Transfer-Encoding: chunked`` body, or ``None`` if malformed."""
            decoded = bytearray()
            while True:
                line = self.rfile.readline(_MAX_CHUNK_LINE_BYTES + 1)
                if not line or len(line) > _MAX_CHUNK_LINE_BYTES:
                    return None
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    return None  # conformance: ignore[E007] a malformed chunk header is a protocol error the caller turns into a 400; the status code is the report
                if size < 0 or len(decoded) + size > _MAX_BODY_BYTES:
                    return None
                if size == 0:
                    break
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    return None
                decoded += chunk
            while True:
                line = self.rfile.readline(_MAX_CHUNK_LINE_BYTES + 1)
                if not line or len(line) > _MAX_CHUNK_LINE_BYTES:
                    return None
                if line in (b"\r\n", b"\n"):
                    return bytes(decoded)

        def _read_body(self) -> tuple[bytes, int | None]:
            """Read the request body, returning it with a refusal status or ``None``."""
            encoding = self.headers.get("Transfer-Encoding", "")
            if "chunked" in encoding.lower():
                decoded = self._read_chunked_body()
                return (b"", 400) if decoded is None else (decoded, None)
            raw = (self.headers.get("Content-Length") or "").strip()
            if not raw:
                return b"", None
            try:
                length = int(raw)
            except ValueError:
                # conformance: ignore[E007] a malformed Content-Length is a protocol error reported to the client as the 400 returned here
                return b"", 400
            if length < 0:
                return b"", 400
            if length > _MAX_BODY_BYTES:
                return b"", 413
            payload = self.rfile.read(length) if length else b""
            return (payload, None) if len(payload) == length else (b"", 400)

        def _handle(self) -> None:
            split = urlsplit(self.path)
            query = parse_qs(split.query, keep_blank_values=True)
            body, refusal = self._read_body()
            if refusal is not None:
                self.close_connection = True
                logger.warning(
                    "%s could not read the body of %s %s — answering %s",
                    fake.name,
                    self.command.upper(),
                    split.path,
                    refusal,
                )
                self._respond(
                    FakeResponse.json_(
                        _BODY_REFUSALS[refusal], status=refusal, Connection="close"
                    ),
                    self.command.upper(),
                    server_owned=True,
                )
                return
            request = FakeRequest(
                method=self.command.upper(),
                path=split.path,
                params={key: values[0] for key, values in query.items() if values},
                query=query,
                path_params={},
                headers=dict(self.headers.items()),
                body=body,
            )
            self._respond(fake._dispatch(request), request.method)

        def _respond(
            self, response: FakeResponse, method: str, *, server_owned: bool = False
        ) -> None:
            """Write *response*.

            ``server_owned`` marks a response this module generated itself (the
            body refusals, which must send ``Connection: close``). Only handler
            headers are filtered — a handler cannot be allowed to set framing,
            but the server still needs to.
            """
            payload, content_type = response.encode(fake.default_content_type)
            if method == "HEAD":
                payload = b""
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in response.headers.items():
                if not server_owned and key.lower() in _RESERVED_RESPONSE_HEADERS:
                    logger.warning(
                        "%s dropped handler-supplied %r header: the server owns "
                        "response framing",
                        fake.name,
                        key,
                    )
                    continue
                self.send_header(key, value)
            self.end_headers()
            if payload:
                self.wfile.write(payload)

    for method in _METHODS:
        setattr(_Handler, f"do_{method}", _Handler._handle)
    return _Handler


class HttpFakeSourceFactory:
    """The fakes one pytest session built, owned in one place.

    Backs the shipped ``http_fake_source_factory`` fixture in
    :mod:`application_sdk.testing.integration.fixtures`. Routes are the
    per-connector part and stay in the connector's own session fixture; starting
    the servers, resetting recordings before every test and stopping everything at
    the end are the same everywhere and happen here::

        @pytest.fixture(scope="session")
        def integration_source(http_fake_source_factory) -> HttpFakeSource:
            fake = http_fake_source_factory(name="my-source")
            fake.route(r"/api/v1/objects", list_objects)
            return fake
    """

    def __init__(self) -> None:
        self._sources: list[HttpFakeSource] = []

    def __call__(
        self,
        *,
        routes: Iterable[tuple[str, Handler]] = (),
        default_content_type: str = "application/json",
        not_found_body: object = None,
        authorize: Authorizer | None = None,
        warn_unmatched: bool = True,
        name: str = "fake-source",
        connection_timeout: float = _CONNECTION_TIMEOUT_SECONDS,
    ) -> HttpFakeSource:
        """Build and start one fake; the parameters are :class:`HttpFakeSource`'s."""
        source = HttpFakeSource(
            routes=routes,
            default_content_type=default_content_type,
            not_found_body=not_found_body,
            authorize=authorize,
            warn_unmatched=warn_unmatched,
            name=name,
            connection_timeout=connection_timeout,
        )
        self._sources.append(source)
        try:
            source.start()
        except Exception:
            self._sources.remove(source)
            raise
        return source

    @property
    def sources(self) -> Sequence[HttpFakeSource]:
        """Every fake built so far, in creation order."""
        return list(self._sources)

    def reset_all(self) -> None:
        """:meth:`HttpFakeSource.reset` every fake, leaving the servers running."""
        for source in self._sources:
            source.reset()

    def stop_all(self) -> None:
        """Stop every fake, newest first, and forget them."""
        sources, self._sources = self._sources, []
        for source in reversed(sources):
            source.stop()
