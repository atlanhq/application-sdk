"""``kubectl port-forward`` as transport, moved here from ``testing/e2e``.

**Transport, not a reader verb.** Everything else in this package retires
``kubectl`` in favour of the in-process typed client (FND-241); this one file
keeps it, because it is not a read. It is how an out-of-cluster driver reaches an
app handler ``Service`` at all, and the typed client's equivalent
(``kubernetes.stream.portforward``) is a socket-level API rather than a drop-in:
adopting it would mean writing the HTTP client too.

It moved for the same reason ``_poll`` and the AE error leaves moved in the
earlier children on FND-224 — direction. :class:`ClusterReader.http` sits on
this, and a harness module cannot import from the package child H re-expresses
*over* it without making a ``harness -> e2e -> harness`` cycle.
:mod:`application_sdk.testing.e2e.portforward` re-exports the same function
object, so no import path and no call site changes.

**One tunnel per session, rebuilt on a transport failure.** The version this
replaces opened and tore down a tunnel per call, and
``workflows.wait_for_workflow`` therefore paid a fresh ``kubectl`` process,
handshake and readiness poll on *every* poll of a 300-second wait. Its stated
reason — idle TCP timeouts on a long-lived forward — is real but does not apply
inside a poll: a 5-second cadence never leaves the tunnel idle. So the session
holds one tunnel, and the case the per-call teardown was really buying — a
tunnel that has already died — is handled where it belongs, by rebuilding once
on a transport error and re-attempting. That is the same trade child F made for
the AE client's pool: pooled on the happy path, a fresh connection on the retry.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness._poll import until_deadline_async

logger = get_logger(__name__)

__all__ = ["PortForward", "kube_http_call", "kubectl_argv", "port_forward"]

# Tight cadence: the port opens within a few hundred ms once kubectl's tunnel is
# up, and every attempt already carries its own 1s connect timeout.
_PORT_POLL_INTERVAL_SECONDS = 0.1

#: Longest wait for the local end of the tunnel to accept connections. Bounded
#: separately from the request timeout because a tunnel that never opens is a
#: different failure from a handler that never answers.
_PORT_READY_TIMEOUT_SECONDS = 10.0

#: Grace given to ``kubectl`` to exit after ``terminate()`` before it is killed.
_TERMINATE_GRACE_SECONDS = 5.0

#: Local end of every tunnel. ``kubectl port-forward`` binds loopback by
#: default, and the address is handed to callers verbatim, so it is named once
#: rather than formatted in two places that could disagree.
_LOCAL_HOST = "127.0.0.1"


def _find_free_port() -> int:  # pragma: no cover — binds a real socket
    """Bind port 0 on loopback to let the OS pick a free port, and return it.

    Loopback rather than every interface because that is the interface whose port
    actually has to be free: ``kubectl port-forward`` binds its local end to
    loopback, which is what :func:`_wait_for_port` and the client's ``base_url``
    both already assume. Probing ``""`` asked a broader question than the tunnel
    needs, and tripped CodeQL's all-interfaces bind rule on a socket that is
    never listened on.

    What this is **not** is a correctness fix, and the distinction is worth
    keeping because the plausible version of it is wrong. ``("", 0)`` could not
    hand back a port that loopback then refused: a TCP bind conflicts
    symmetrically in both directions — a loopback-held port refuses an
    all-interfaces bind and an all-interfaces-held port refuses a loopback one
    (checked on macOS, ``errno 48`` each way) — so the kernel's ephemeral choice avoided
    conflicts whichever address it was given. The change buys a truthful
    expression of the constraint and a quiet scanner, not a fixed bug.

    Exempt from unit coverage rather than untested: the unit suite runs with
    ``--disable-socket``, so the only place this can execute is
    ``tests/integration/testing/test_portforward.py``, which does.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((_LOCAL_HOST, 0))
        return s.getsockname()[1]


async def _wait_for_port(
    port: int, host: str = "127.0.0.1", timeout: float = _PORT_READY_TIMEOUT_SECONDS
) -> None:
    """Poll until TCP port accepts connections or timeout."""
    async for attempt in until_deadline_async(
        timeout,
        _PORT_POLL_INTERVAL_SECONDS,
        label=f"tcp://{host}:{port}",
        # A 10s budget never reaches the 30s heartbeat cadence, and the caller
        # already logs the port-forward it is waiting on.
        heartbeat_seconds=0,
    ):
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.0
            )
            writer.close()
            await writer.wait_closed()
            return
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
            if attempt.is_last:
                raise TimeoutError(
                    f"Port {port} did not become ready within {timeout}s "
                    f"({attempt.number} attempts)"
                ) from exc


class PortForward:
    """One ``kubectl port-forward`` tunnel to a Service, and calls over it.

    Obtained from :func:`port_forward`, which owns its lifetime. Not constructed
    directly: a tunnel that nothing closes leaks a ``kubectl`` process for the
    rest of the test session. The one exception is a caller that needs the
    tunnel's address to outlive any ``with`` block — see :meth:`address` — and
    owning it in that case means calling :meth:`aclose`.

    Args:
        namespace: Namespace the Service is in.
        service: Service name, without any ``svc/`` prefix.
        port: Remote port to forward to.
        timeout: Default per-request timeout in seconds.
        kube_context: Kubeconfig context to tunnel through. **Required for
            correctness whenever the caller reads from a named context**: without
            it ``kubectl`` uses whichever context the kubeconfig marks current, so
            a reader built with ``kube_context="e2e-gcp"`` would list pods from
            one cluster and HTTP-tunnel into another — with no error anywhere,
            because both calls succeed. ``None`` means "whatever is current",
            which is only right when the reader did not name one either.
    """

    def __init__(
        self,
        namespace: str,
        service: str,
        port: int,
        *,
        timeout: float = 30.0,
        kube_context: str | None = None,
    ) -> None:
        self.namespace = namespace
        self.service = service
        self.port = port
        self.timeout = timeout
        self.kube_context = kube_context
        self._proc: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._local_port: int | None = None
        # Guards the lazy open, which is check-then-act: two concurrent callers
        # could each spawn a `kubectl` and only one would be recorded — leaking
        # the other for the rest of the session. Normal use is sequential (one
        # tunnel per `port_forward()`/`http()`), but a leaked subprocess is not
        # the kind of thing to leave to usage.
        #
        # One lock, two lazy-open entry points since FND-247: `_opened` for the
        # HTTP client and `address` for the raw target. Each takes this lock for
        # its whole job rather than sharing a helper that takes it — a guard on
        # `_opened` alone would let `address()` race `request()` into two
        # processes, and routing `_opened` through `_tunnel` would need two
        # acquisitions and reopen the window between them.
        self._opening = asyncio.Lock()

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Make one call over the tunnel, rebuilding it once on a transport error.

        A non-2xx response is returned, not raised: which statuses count as
        failure is the caller's predicate to apply — the same decision
        :class:`~application_sdk.testing.harness.cluster.ClusterReader.http`
        records. ``raise_for_status`` therefore stays at the call site (as
        ``workflows.run_workflow`` already does), not here.

        Args:
            method: HTTP method.
            path: Request path, query string included.
            body: JSON body, or ``None``.
            headers: Extra request headers.
            timeout: Per-request override of the session's default. ``None``
                means "no override", and is *omitted* rather than forwarded:
                ``httpx`` reads an explicit ``timeout=None`` as "no timeout at
                all", so passing it through would silently remove the bound the
                session was configured with.

        Returns:
            The response, whatever its status.

        Raises:
            httpx.TransportError: If the rebuilt tunnel also fails to carry the
                call. Two consecutive transport failures are the tunnel being
                genuinely unreachable, not a stale connection.
        """
        overrides: dict[str, Any] = {} if timeout is None else {"timeout": timeout}

        async def _send() -> httpx.Response:
            client = await self._opened()
            return await client.request(
                method, path, json=body, headers=dict(headers or {}), **overrides
            )

        try:
            return await _send()
        except httpx.TransportError:
            logger.warning(
                "port-forward to %s/%s:%d failed mid-call — rebuilding the "
                "tunnel and re-attempting %s %s once",
                self.namespace,
                self.service,
                self.port,
                method,
                path,
                exc_info=True,
            )
        await self.aclose()
        return await _send()

    async def address(self) -> str:
        """Return the tunnel's local ``host:port``, opening the tunnel if needed.

        For a client this class cannot make the call for. ``temporalio`` speaks
        gRPC and takes a bare ``host:port`` target
        (:mod:`application_sdk.testing.harness.temporal`), so the Temporal reader
        needs the tunnel's near end rather than an HTTP session over it — and a
        second implementation of "shell out, find a free port, wait for it to
        accept" is exactly the divergence this module was consolidated to end.

        The caller owns the tunnel's lifetime in that case, which is the one
        exception to "obtained from :func:`port_forward`": whatever holds the
        address has to call :meth:`aclose` when it is finished, because the
        address outlives any ``with`` block that produced it.

        Returns:
            ``127.0.0.1:{port}``, where the port is the OS-assigned local end.

        Raises:
            TimeoutError: If the tunnel's local port never accepted connections.
        """
        return f"{_LOCAL_HOST}:{await self._tunnel()}"

    async def _tunnel(self) -> int:
        """Return the local port, spawning the tunnel if it is not up.

        The raw-target entry point, for :meth:`address`. :meth:`_opened` does not
        route through here — it holds the lock across its own spawn, for the
        reason given there — so both entry points serialise on the same lock and
        each completes its whole job inside one critical section.

        The double check is deliberate: the common path is a tunnel that is
        already up, and taking a lock to discover that would put every call
        through a serialisation point it does not need.
        """
        if self._local_port is not None:
            return self._local_port
        async with self._opening:
            if self._local_port is not None:
                return self._local_port
            return await self._spawn()

    async def _spawn(self) -> int:
        """Spawn the tunnel and wait for its local port. Callers hold the lock."""
        local_port = _find_free_port()
        self._proc = await asyncio.create_subprocess_exec(
            *kubectl_argv(
                "port-forward",
                f"svc/{self.service}",
                f"{local_port}:{self.port}",
                "-n",
                self.namespace,
                kube_context=self.kube_context,
            ),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await _wait_for_port(
                local_port, timeout=min(_PORT_READY_TIMEOUT_SECONDS, self.timeout)
            )
        except BaseException:
            # The process is already running; leaving it behind on a failed
            # readiness wait is the leak this except exists to prevent.
            #
            # This call is also why `aclose` must never take `self._opening`:
            # we are holding it right now. See the warning on `aclose`.
            await self.aclose()
            raise
        self._local_port = local_port
        return local_port

    async def _opened(self) -> httpx.AsyncClient:
        """Return the tunnel's client, opening the tunnel if it is not up.

        Spawns the tunnel and builds the client inside **one** acquisition, by
        calling :meth:`_spawn` directly rather than going through
        :meth:`_tunnel`. Two acquisitions would be the obvious composition and it
        is wrong: :class:`asyncio.Lock` is not reentrant, so the lock cannot be
        held across a call that takes it, and the handoff between them leaves a
        window where an :meth:`aclose` can terminate the ``kubectl`` this client
        is about to be pointed at. One critical section closes *that* window, at
        the cost of repeating the port check.

        It does **not** make open-vs-close atomic: :meth:`aclose` takes no lock,
        so a close concurrent with this critical section can still strand a fresh
        client on a terminated process. That residue is deliberate, and the
        tempting fix for it deadlocks — see the warning on :meth:`aclose`, which
        is where someone would go to make the mistake.

        The client is guarded as well as the process for the smaller version of
        the same reason: a dropped :class:`httpx.AsyncClient` leaks a connection
        pool.
        """
        if self._client is not None:
            return self._client
        async with self._opening:
            if self._client is not None:
                return self._client
            local_port = self._local_port
            if local_port is None:
                local_port = await self._spawn()
            self._client = httpx.AsyncClient(
                base_url=f"http://{_LOCAL_HOST}:{local_port}", timeout=self.timeout
            )
            return self._client

    async def aclose(self) -> None:
        """Close the HTTP client and terminate the ``kubectl`` process.

        Idempotent, and safe to call on a tunnel that never opened.

        .. warning::
            **Do not guard this with** :attr:`_opening`. It is the obvious way to
            close the residual open-vs-close race — a concurrent ``aclose`` can
            still land mid-open — and it **deadlocks**: :meth:`_spawn` runs with
            the lock held and calls this from its readiness-failure path, so a
            lock here would wait on one the same task already owns
            (:class:`asyncio.Lock` is not reentrant).

            The failure mode is what earns this a warning rather than a comment:
            every happy-path test passes, and the hang arrives the first time a
            port never comes up — the unhappy path, in production, on a day
            something else is already wrong.

            The residue is left open deliberately. A caller closing a session
            while another task is still using it is doing something odd, and the
            transport retry in :meth:`request` rebuilds a tunnel closed
            underneath it. Closing it properly needs a locked public ``aclose``
            over an unlocked internal one — more machinery than a self-healing
            race is worth.
        """
        client, self._client = self._client, None
        self._local_port = None
        if client is not None:
            await client.aclose()
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()


def kubectl_argv(*args: str, kube_context: str | None = None) -> tuple[str, ...]:
    """Build a ``kubectl`` argv, pinning the context when one was named.

    One function so every ``kubectl`` this package still shells out to — the
    port-forward here, and ``LogCollector``'s ``describe`` / ``get pods -o wide``
    / ``get events`` — pins the same context as the typed reads do. Splitting
    that decision across call sites is how a run comes to read one cluster and
    write evidence about another.

    Args:
        *args: The ``kubectl`` arguments, without the leading ``kubectl``.
        kube_context: Context to pin, or ``None`` to accept the current one.

    Returns:
        The full argv, ready for ``create_subprocess_exec``.
    """
    context = ("--context", kube_context) if kube_context else ()
    return ("kubectl", *args, *context)


@asynccontextmanager
async def port_forward(
    namespace: str,
    service: str,
    port: int,
    *,
    timeout: float = 30.0,
    kube_context: str | None = None,
) -> AsyncIterator[PortForward]:
    """Hold one tunnel to a Service for a batch of calls.

    Use this when more than one call goes to the same Service — a poll loop, or a
    submit followed by a status read. For a single call,
    :func:`kube_http_call` is the one-liner over it.

    Args:
        namespace: Namespace the Service is in.
        service: Service name, without any ``svc/`` prefix.
        port: Remote port to forward to.
        timeout: Default per-request timeout in seconds, and the bound on waiting
            for the tunnel's local port to open.
        kube_context: Kubeconfig context to tunnel through. Pass the same one the
            reads use, or the tunnel may reach a different cluster entirely.

    Yields:
        The session. The tunnel opens on first use, not on entry, and is torn
        down on exit whether or not any call was made.
    """
    session = PortForward(
        namespace, service, port, timeout=timeout, kube_context=kube_context
    )
    try:
        yield session
    finally:
        await session.aclose()


async def kube_http_call(
    namespace: str,
    service: str,
    port: int,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
    kube_context: str | None = None,
) -> httpx.Response:
    """Make one HTTP call to a K8s Service via an ephemeral port-forward.

    Opens a ``kubectl port-forward`` tunnel for the duration of the request,
    makes the HTTP call, then closes the tunnel. For more than one call to the
    same Service, hold a :func:`port_forward` session instead — one tunnel for
    the batch rather than one per call.

    Args:
        namespace: K8s namespace where the service lives.
        service: Service name (without ``svc/`` prefix).
        port: Remote port to forward to.
        method: HTTP method (``GET``, ``POST``, etc.).
        path: Request path, e.g. ``"/health"``.
        body: Optional JSON body for POST/PUT requests.
        timeout: Total timeout in seconds for port-forward + HTTP request.
        kube_context: Kubeconfig context to tunnel through, or ``None`` for the
            current one.

    Returns:
        The :class:`httpx.Response`, whatever its status.
    """
    async with port_forward(
        namespace, service, port, timeout=timeout, kube_context=kube_context
    ) as session:
        return await session.request(method, path, body=body)
