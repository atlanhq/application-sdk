"""Forward the daprd sidecar's logs into the SDK observability pipeline.

In production the container entrypoint launches ``daprd`` as a sibling process
of the Python application, so daprd's own logs go straight to the container's
stdout/stderr and never reach the SDK observability sink — which means, in SDR
mode, they never reach the central lakehouse either. Operators can only see them
by reading the customer's pod/container logs directly.

This module closes that gap. In SDR mode (``ENABLE_ATLAN_UPLOAD=true``) — the
same signal that tells the app it runs on customer infra with no node-level log
collector — the entrypoint runs daprd *under* this module:

    python -m application_sdk.observability.dapr_log_forwarder -- daprd <flags> --log-as-json

It spawns daprd as a child, reads its (JSON) log stream line by line, and
re-emits each line through a ``dapr.runtime`` logger obtained from
:func:`application_sdk.observability.logger_adaptor.get_logger`. Those records
flow through the same handler → loguru → store-sink path as the app's own logs,
so they land in the lakehouse ``app_logs`` table with ``app_name`` / ``is_sdr``
populated. The SDK logger's existing stdout/stderr sinks keep the lines visible
in the container's own logs too, so nothing is lost from ``kubectl logs``.

On atlan-infra (``ENABLE_ATLAN_UPLOAD=false``) the node-level filelog collector
already scrapes the container's stdout (daprd included), so forwarding is a
no-op there and the child is exec'd directly.

Design constraints:

* **Run under an event loop.** The SDK's store sink (``parquet_sink``) is an
  async loguru sink: records only get buffered for upload while an event loop is
  running in the emitting thread. So the read/emit loop runs inside
  ``asyncio.run`` and drains the sink (``logger.complete()`` + ``flush_all()``)
  before exit — otherwise lines reach stdout but never the lakehouse.
* **Never take daprd down.** Forwarding is best-effort: any failure to parse or
  emit a line falls back to writing the raw line to stderr; daprd keeps running.
* **Preserve graceful shutdown.** SIGTERM/SIGINT are forwarded to daprd so its
  ``--dapr-graceful-shutdown-seconds`` behaviour (the reason the entrypoint runs
  daprd directly) is unchanged. This process exits with daprd's exit code.
* **Transparent when disabled.** Outside SDR mode the child is exec'd directly,
  so the process tree and PID semantics match running daprd without the wrapper.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import Any

# daprd (logrus) log levels → AtlanLoggerAdapter method names.
_LEVEL_TO_METHOD = {
    "debug": "debug",
    "info": "info",
    "warning": "warning",
    "warn": "warning",
    "error": "error",
    "fatal": "critical",
    "panic": "critical",
}


def _split_child_command(argv: list[str]) -> list[str]:
    """Return the child command — everything after the first ``--`` separator.

    Falls back to all args (minus the program name) if no separator is present.
    """
    if "--" in argv:
        return argv[argv.index("--") + 1 :]
    return argv[1:]


def _exec_transparently(child_cmd: list[str]) -> None:
    """Replace this process with *child_cmd* (used when forwarding is disabled)."""
    os.execvp(child_cmd[0], child_cmd)


def _make_emitter() -> tuple[Any, Any]:
    """Return ``(emit, complete)`` backed by the SDK logger.

    ``emit(level, message)`` never raises — if the SDK logger can't be set up it
    falls back to writing the message to stderr so the line is never silently
    dropped. ``complete`` is an awaitable that drains loguru's async sinks (so
    buffered records reach the upload buffer), or ``None`` if logging is
    unavailable.
    """

    def _stderr_fallback(_level: str, message: str) -> None:
        # Deliberate stderr write: this path runs when the SDK logger itself is
        # unavailable/failing, so we must not route through it.
        print(message, file=sys.stderr, flush=True)  # noqa: T201

    try:
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — deferred: avoid importing the heavy logging stack at module load
            get_logger,
        )

        # Created inside the running loop (see main) so the SDK's async store
        # sink and periodic flush bind to that loop.
        logger = get_logger("dapr.runtime")
    except Exception:  # pragma: no cover - defensive: logging must never break daprd
        return _stderr_fallback, None

    def _emit(level: str, message: str) -> None:
        try:
            method = getattr(logger, _LEVEL_TO_METHOD.get(level, "info"), logger.info)
            method(message)
        except Exception:
            _stderr_fallback(level, message)

    async def _complete() -> None:
        # Drain loguru's pending coroutine-sink tasks into the upload buffer.
        await logger.logger.complete()

    return _emit, _complete


def _format_line(raw: str) -> tuple[str, str]:
    """Parse one daprd log line into ``(level, message)``.

    daprd emits JSON when run with ``--log-as-json``. The ``scope`` field (e.g.
    ``dapr.runtime.loader.disk``) is folded into the message so it stays
    queryable in the lakehouse ``message`` column regardless of which structured
    fields the schema retains. Non-JSON lines are forwarded verbatim at INFO.
    """
    raw = raw.rstrip("\n")
    try:
        record = json.loads(raw)
        if not isinstance(record, dict):
            return "info", raw
    except (json.JSONDecodeError, ValueError):
        return "info", raw

    level = str(record.get("level", "info")).lower()
    msg = str(record.get("msg", raw))
    scope = record.get("scope")
    message = f"[{scope}] {msg}" if scope else msg
    return level, message


def _install_signal_forwarding(proc: asyncio.subprocess.Process) -> None:
    """Forward SIGTERM/SIGINT to daprd so its graceful shutdown runs."""

    def _forward(signum: int) -> None:
        if proc.returncode is None:
            try:
                proc.send_signal(signum)
            # conformance: ignore[E002] TOCTOU race: daprd exited between returncode check and send_signal; no logger available in this signal-handler closure
            except ProcessLookupError:  # pragma: no cover - daprd already gone
                pass

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _forward, sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-Unix
            signal.signal(sig, lambda s, _f: _forward(s))


async def _drain_and_flush(complete: Any) -> None:
    """Drain async sinks and flush buffered records so daprd logs aren't lost."""
    if complete is not None:
        try:
            await complete()
        except Exception:  # noqa: S110 — best-effort drain
            pass
    try:
        from application_sdk.observability.observability import (  # noqa: PLC0415 — deferred: cold shutdown path
            AtlanObservability,
        )

        await AtlanObservability.flush_all()
    except Exception:  # noqa: S110 — best-effort flush
        pass


async def _periodic_drain_flush(complete: Any, interval: float) -> None:
    """Drain + upload buffered records on this loop, every *interval* seconds.

    We can't rely on the SDK's own periodic flush here: this forwarder imports
    SDK modules before the event loop starts, so the SDK's flush task binds to a
    background thread (the no-running-loop fallback) which doesn't reliably
    upload from this second process. Driving the flush on the forwarder's own
    loop makes delivery deterministic. Unlike the SDK's task, this stays alive
    across transient upload errors instead of dying on the first one.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            await _drain_and_flush(complete)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: S110 — keep flushing across transient errors
            pass


async def _run(child_cmd: list[str]) -> int:
    """Spawn daprd, forward its logs through the SDK pipeline, return its code."""
    emit, complete = _make_emitter()

    proc = await asyncio.create_subprocess_exec(
        *child_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=2
        * 1024
        * 1024,  # 2 MiB — panic/stack traces can exceed the 64 KiB default
    )
    _install_signal_forwarding(proc)

    from application_sdk.constants import (  # noqa: PLC0415 — deferred: env-driven flush cadence
        LOG_FLUSH_INTERVAL_SECONDS,
    )

    flusher = asyncio.create_task(
        _periodic_drain_flush(complete, LOG_FLUSH_INTERVAL_SECONDS)
    )

    try:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                # Line longer than 2 MiB — forward a truncated prefix so the line
                # is never silently dropped, then drain the rest of it.
                chunk = await proc.stdout.readexactly(exc.consumed)
                try:
                    await proc.stdout.readuntil(b"\n")
                except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
                    pass
                prefix = chunk[:256].decode("utf-8", errors="replace").rstrip("\n")
                emit("warning", f"{prefix} ...[{exc.consumed} bytes, line truncated]")
                continue
            except asyncio.IncompleteReadError as exc:
                # EOF — process any trailing bytes without a final newline.
                if exc.partial:
                    raw = exc.partial
                else:
                    break
            line = raw.decode("utf-8", errors="replace")
            try:
                level, message = _format_line(line)
                emit(level, message)
            except Exception:
                # Forwarding must never interrupt the stream — fall back to raw.
                print(line.rstrip("\n"), file=sys.stderr, flush=True)  # noqa: T201
    finally:
        flusher.cancel()
        try:
            await flusher
        # conformance: ignore[E002] expected: CancelledError is the normal result of awaiting an explicitly cancelled asyncio task
        except asyncio.CancelledError:
            pass
        returncode = await proc.wait()
        await _drain_and_flush(complete)

    return returncode


def main(argv: list[str] | None = None) -> int:
    """Run daprd as a child and forward its logs to the observability pipeline."""
    argv = list(sys.argv if argv is None else argv)
    child_cmd = _split_child_command(argv)
    if not child_cmd:
        print(  # noqa: T201 — startup arg error, before any logger is set up
            "dapr_log_forwarder: no child command supplied (expected '-- daprd ...')",
            file=sys.stderr,
        )
        return 2

    # Defensive double-gate: the entrypoint.sh already gates the forwarder on
    # ENABLE_ATLAN_UPLOAD, but `python -m application_sdk.observability.dapr_log_forwarder`
    # is a documented entry point ("safe to invoke unconditionally"), so this gate
    # makes direct invocation safe regardless of the environment.
    from application_sdk.constants import (  # noqa: PLC0415 — deferred: read at call time so the flag is env-fresh/patchable
        ENABLE_ATLAN_UPLOAD,
    )

    if not ENABLE_ATLAN_UPLOAD:
        _exec_transparently(child_cmd)  # never returns

    return asyncio.run(_run(child_cmd))


if __name__ == "__main__":
    sys.exit(main())
