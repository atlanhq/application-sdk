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

That fixture ships too — ``http_fake_source_factory`` and
``clean_http_fake_sources`` in :mod:`application_sdk.testing.fixtures` handle the
session lifecycle and the per-test reset, leaving the connector only its routes.

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

The same reasoning is why :meth:`HttpFakeSource.route` can constrain query
parameters. A source whose distinct request shapes share one path and differ only
by a parameter would otherwise register as a single route, and the
"every registered route was exercised" check would pass while covering one of
three shapes. One route per shape keeps that check honest.

Stdlib only, by constraint: ``pytest-httpserver`` and ``respx`` are not fleet
dependencies, and adding one would make these suites unrunnable in normal CI.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
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
    "HttpFakeSourceFactory",
    "OffsetPage",
    "assert_extract_roundtrip",
    "cursor_page",
    "offset_page",
    "serve",
]

logger = get_logger(__name__)

_LOOPBACK = "127.0.0.1"
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_CONNECTION_TIMEOUT_SECONDS = 5.0
_JOIN_GRACE_SECONDS = 5.0
_MAX_BODY_BYTES = 64 * 1024 * 1024
_MAX_CHUNK_LINE_BYTES = 8192
# Framing and hop-by-hop headers the server owns. A handler that replays a
# captured response verbatim will carry these, and emitting them alongside the
# server's own Content-Length puts two conflicting values on the wire: a client
# that honours the last one waits for a body that never arrives.
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

    Every constructor takes headers two ways: a ``headers`` mapping and keyword
    arguments. The mapping is the general form — a real source's header names are
    routinely not Python identifiers, and ``X-Some-Token`` cannot be spelled as a
    keyword at all — while the keyword form stays the shorthand for the names that
    happen to be identifiers. Given the same key both ways, the keyword wins.
    """

    status: int = 200
    body: Any = None
    content_type: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def json_(
        cls,
        body: Any,
        status: int = 200,
        *,
        headers: Mapping[str, str] | None = None,
        **header_kwargs: str,
    ) -> FakeResponse:
        """A JSON response; ``body`` is serialised with :func:`json.dumps`."""
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
            json.dumps(body, default=str).encode(),
            content_type or default_content_type,
        )


Handler = Callable[[FakeRequest], Any]
Authorizer = Callable[[FakeRequest], Any]

QueryConstraint = str | re.Pattern[str] | bool
"""One query-parameter condition on a route.

A ``str`` means the parameter's value must equal it exactly; a compiled regex
means :meth:`~re.Pattern.fullmatch` against the value; ``True`` means the
parameter must be present with any value (including empty); ``False`` means it
must be absent.

A repeated query parameter (``?type=a&type=b``) is tested against its FIRST
value only — a source whose request shapes differ in a later repetition of the
same key cannot be discriminated by ``query=``; model those as separate
handler-side branches on ``request.query`` instead (see :meth:`HttpFakeSource.route`).
"""

QuerySpec = Mapping[str, QueryConstraint]
"""A route's query conditions, all of which must hold for the route to match."""


def _merge_headers(
    headers: Mapping[str, str] | None, header_kwargs: Mapping[str, str]
) -> Mapping[str, str]:
    if not headers:
        return dict(header_kwargs)
    merged = dict(headers)
    merged.update(header_kwargs)
    return merged


def _spell_constraint(constraint: QueryConstraint) -> str:
    if constraint is True:
        return "<present>"
    if constraint is False:
        return "<absent>"
    if isinstance(constraint, re.Pattern):
        return f"~{constraint.pattern}"
    return constraint


def _constraint_key(constraint: QueryConstraint) -> tuple[str, str]:
    """A comparable form, since two equal :func:`re.compile` results are not."""
    if constraint is True:
        return ("present", "")
    if constraint is False:
        return ("absent", "")
    if isinstance(constraint, re.Pattern):
        return ("regex", constraint.pattern)
    return ("exact", constraint)


def _query_key(
    query: Sequence[tuple[str, QueryConstraint]],
) -> tuple[tuple[str, tuple[str, str]], ...]:
    return tuple(sorted((name, _constraint_key(c)) for name, c in query))


def _validated_query(
    query: QuerySpec | None,
) -> tuple[tuple[str, QueryConstraint], ...]:
    if not query:
        return ()
    for name, constraint in query.items():
        if not isinstance(constraint, (bool, str, re.Pattern)):
            raise TypeError(
                f"query constraint for {name!r} must be a str (exact value), a "
                "compiled regex (fullmatch), True (present) or False (absent), "
                f"got {type(constraint).__name__}"
            )
    return tuple(query.items())


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
    query: tuple[tuple[str, QueryConstraint], ...] = ()

    @property
    def label(self) -> str:
        """The pattern, plus its query conditions when it has any.

        Routes sharing one path pattern are otherwise indistinguishable in
        :meth:`HttpFakeSource.unused_routes`, which is exactly the diagnostic that
        has to name them.
        """
        if not self.query:
            return self.pattern.pattern
        spelled = "&".join(
            f"{name}={_spell_constraint(constraint)}" for name, constraint in self.query
        )
        return f"{self.pattern.pattern}?{spelled}"

    def matches_path(self, path: str) -> re.Match[str] | None:
        return self.pattern.fullmatch(path)

    def matches_query(self, query: Mapping[str, Sequence[str]]) -> bool:
        for name, constraint in self.query:
            values = query.get(name)
            value = values[0] if values else None
            if constraint is True:
                if value is None:
                    return False
            elif constraint is False:
                if value is not None:
                    return False
            elif isinstance(constraint, re.Pattern):
                if value is None or constraint.fullmatch(value) is None:
                    return False
            elif value != constraint:
                return False
        return True


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
    A route may additionally constrain query parameters — see :meth:`route`.

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
        self._unmatched_query: list[FakeRequest] = []
        self._hits: dict[int, int] = {}
        for pattern, handler in routes:
            self.route(pattern, handler)

    def route(
        self,
        pattern: str,
        handler: Handler,
        *,
        methods: Sequence[str] = ("GET",),
        query: QuerySpec | None = None,
    ) -> Self:
        """Register ``handler`` for paths fully matching ``pattern``.

        Returns ``self``, so registrations chain. ``pattern`` is a regex matched
        against the path with :meth:`~re.Pattern.fullmatch`; use named groups for
        path parameters.

        ``query`` narrows the route to requests whose query string satisfies every
        condition in it (see :data:`QueryConstraint` for the four forms). That is
        for the source whose distinct request shapes share one path and differ only
        by a parameter — a metadata catalog answering a collection GET, a
        per-record schema GET and a property-resolution GET all off the same path,
        told apart by ``select``::

            CATALOG = r"/services/rest/record/v1/metadata-catalog"
            fake.route(CATALOG, catalog_collection, query={"select": False})
            fake.route(CATALOG, record_schema, query={"select": re.compile(r"[^,]+")})
            fake.route(
                CATALOG, property_schema, query={"select": re.compile(r"[^,]+,[^,]+")}
            )

        Registering those as three routes rather than one branching handler is what
        keeps :meth:`hits`, :meth:`unused_routes` and
        :func:`assert_extract_roundtrip`'s route-usage check meaningful: each shape
        is separately counted, so a shape the extract never exercises is reported
        instead of hidden behind a sibling's hit.

        **Precedence.** Among the routes matching a request's path and method, the
        one with the most query conditions satisfied wins; ties go to the earliest
        registered. A route with no ``query`` is therefore the fallback for its
        path, whenever it was registered — while routes that are equally specific
        keep the registration-order rule they have always had.

        **Trailing slash.** The path is tried as sent, then again without a single
        trailing slash. A source that tolerates ``/objects/`` for ``/objects``
        needs no ``/?`` in the pattern, and an exact match always beats the
        normalised retry, so a route written with a trailing slash still owns it.

        Query conditions gate matching only — ``request.params`` and
        ``request.query`` still carry the whole query string, so a route pinned on
        one parameter paginates on the others as usual.

        **Repeated parameters.** A condition tests only the FIRST value of a
        repeated query parameter (``?type=a&type=b``); a source that dispatches
        on the full multiset of a repeated key cannot be expressed as separate
        ``query=``-constrained routes. Model that dispatch inside one handler via
        ``request.query``, which keeps every value.
        """
        upper = frozenset(method.upper() for method in methods)
        if not upper:
            raise ValueError("route requires at least one HTTP method")
        self._routes.append(
            _Route(
                pattern=re.compile(pattern),
                methods=upper,
                handler=handler,
                query=_validated_query(query),
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

    @property
    def unmatched_query(self) -> Sequence[FakeRequest]:
        """The subset of :attr:`unmatched` that missed on the query, not the path.

        A request here reached a route's path and method but satisfied no
        registered query variant — a modelled endpoint called with a parameter
        shape the fake does not model, which is a different mistake from calling an
        endpoint that was never modelled at all, and usually a missing
        :meth:`route` ``query`` variant rather than a missing route.
        """
        with self._lock:
            return list(self._unmatched_query)

    def hits(self, pattern: str, *, query: QuerySpec | None = None) -> int:
        """How many requests matched the route registered with ``pattern``.

        When several routes share one path pattern and differ only by their query
        conditions, pass the same ``query`` the route was registered with to count
        just that variant. Omitting ``query`` counts every variant of the pattern
        together.
        """
        wanted = None if query is None else _query_key(_validated_query(query))
        with self._lock:
            return sum(
                count
                for index, count in self._hits.items()
                if self._routes[index].pattern.pattern == pattern
                and (wanted is None or _query_key(self._routes[index].query) == wanted)
            )

    def unused_routes(self) -> Sequence[str]:
        """Labels of routes no request ever matched.

        A route the extract never called is either dead fixture weight or a code
        path the test believes it covers and does not. A query-constrained route's
        label carries its conditions, so same-path variants are told apart.
        """
        with self._lock:
            return [
                route.label
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
            self._unmatched_query.clear()
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
        """Shut the server down and join every thread it started. Idempotent.

        The per-connection handler threads are joined too, not just the accept
        loop's. An idle keep-alive connection leaves its handler blocked in
        ``readline``, which ``shutdown()`` does nothing about — so the handler
        class carries a socket ``timeout`` (``connection_timeout``): the read wakes,
        the handler closes the connection, and the thread exits to be joined here.
        A thread still alive when the budget runs out is logged rather than
        silently left behind.
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
            except Exception as exc:  # an authorize bug must answer, not hang
                logger.warning(
                    "%s authorize hook raised: %r", self.name, exc, exc_info=True
                )
                return FakeResponse.json_(
                    {"error": "fake source authorize raised", "detail": repr(exc)},
                    status=500,
                )
            if denied is not None:
                return denied

        selected, query_variant_missed = self._select(request)
        if selected is None:
            return self._record_unmatched(request, query_variant_missed)

        index, route, match = selected
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
        except Exception as exc:  # a handler bug must answer, not hang
            logger.warning(
                "%s handler for %r raised: %r",
                self.name,
                route.label,
                exc,
                exc_info=True,
            )
            return FakeResponse.json_(
                {"error": "fake source handler raised", "detail": repr(exc)},
                status=500,
            )
        return response if response is not None else _not_found(self.not_found_body)

    def _select(
        self, request: FakeRequest
    ) -> tuple[tuple[int, _Route, re.Match[str]] | None, bool]:
        """The winning route, and whether the miss was a query miss.

        Most-specific-wins: among the routes matching this path and method, the one
        with the most query conditions, earliest registration breaking a tie. The
        path as sent is resolved fully before the trailing-slash-stripped form is
        tried, so an exact match can never lose to a normalised one.
        """
        method_matched = False
        for path in _candidate_paths(request.path):
            best: tuple[int, _Route, re.Match[str]] | None = None
            for index, route in enumerate(self._routes):
                match = route.matches_path(path)
                if match is None or request.method not in route.methods:
                    continue
                method_matched = True
                if not route.matches_query(request.query):
                    continue
                if best is None or len(route.query) > len(best[1].query):
                    best = (index, route, match)
            if best is not None:
                return best, False
        return None, method_matched

    def _record_unmatched(
        self, request: FakeRequest, query_variant_missed: bool
    ) -> FakeResponse:
        with self._lock:
            self._unmatched.append(request)
            if query_variant_missed:
                self._unmatched_query.append(request)
        if self.warn_unmatched:
            if query_variant_missed:
                logger.warning(
                    "%s matched the path of %s %s but no registered query variant "
                    "(query %r) — answering 404",
                    self.name,
                    request.method,
                    request.path,
                    dict(request.query),
                )
            else:
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
                    return None
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


class HttpFakeSourceFactory:
    """The fakes one pytest session built, owned in one place.

    Backs the shipped fixture pair (``http_fake_source_factory`` and
    ``clean_http_fake_sources`` in :mod:`application_sdk.testing.fixtures`). Routes
    are the per-connector part and stay in the connector's own session fixture;
    starting the servers, resetting recordings between tests and stopping
    everything at the end are the same everywhere and happen here::

        @pytest.fixture(scope="session")
        def source_url(http_fake_source_factory) -> str:
            fake = http_fake_source_factory(name="my-source")
            fake.route(r"/api/v1/objects", list_objects)
            return fake.base_url

        def test_extract(source_url, clean_http_fake_sources) -> None:
            ...
    """

    def __init__(self) -> None:
        self._sources: list[HttpFakeSource] = []

    def __call__(self, **kwargs: Any) -> HttpFakeSource:
        """Build and start one fake; ``kwargs`` are :class:`HttpFakeSource`'s."""
        source = HttpFakeSource(**kwargs)
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

    **Direct-call tier only.** This assertion calls ``extract_fn`` synchronously
    and inspects ``fake.unmatched`` / ``fake.unused_routes()`` immediately after,
    so it only works for a suite that invokes the extract function in-process.
    Once the test routes through a Temporal workflow (``execute_app``) there is
    no callable to hand it, so call the pieces separately instead: run the
    workflow, then assert on the fake directly from the test after the run
    completes — the two checks accumulate on the fake instance across its
    lifetime, not per call, so this still works::

        await executor.execute_app(YourApp, input_data)
        assert not fake.unmatched
        assert fake.unused_routes() == []
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
