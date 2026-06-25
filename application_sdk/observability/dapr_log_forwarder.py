"""Forward the daprd sidecar's logs into the SDK observability pipeline.

In production the container entrypoint launches ``daprd`` as a sibling process
of the Python application, so daprd's own logs go straight to the container's
stdout/stderr and never reach the SDK observability sink — which means, in SDR
mode, they never reach the central lakehouse either. Operators can only see them
by reading the customer's pod/container logs directly.

This module closes that gap. When ``ATLAN_ENABLE_DAPR_LOG_FORWARDING`` is set,
the entrypoint runs daprd *under* this module:

    python -m application_sdk.observability.dapr_log_forwarder -- daprd <flags> --log-as-json

It spawns daprd as a child, reads its (JSON) log stream line by line, and
re-emits each line through a ``dapr.runtime`` logger obtained from
:func:`application_sdk.observability.logger_adaptor.get_logger`. Those records
flow through the same handler → loguru → store-sink path as the app's own logs,
so they land in the lakehouse ``app_logs`` table with ``app_name`` / ``is_sdr``
populated. The SDK logger's existing stdout/stderr sinks keep the lines visible
in the container's own logs too, so nothing is lost from ``kubectl logs``.

Design constraints:

* **Never take daprd down.** Forwarding is best-effort: any failure to parse or
  emit a line falls back to writing the raw line to stderr; daprd keeps running.
* **Preserve graceful shutdown.** SIGTERM/SIGINT are forwarded to daprd so its
  ``--dapr-graceful-shutdown-seconds`` behaviour (the reason the entrypoint runs
  daprd directly) is unchanged. This process exits with daprd's exit code.
* **Transparent when disabled.** If the flag is off, the child command is exec'd
  directly with zero overhead, so the process tree and PID semantics match
  running daprd without the wrapper.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
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


def _exec_transparently(child_cmd: list[str]) -> "None":
    """Replace this process with *child_cmd* (used when forwarding is disabled)."""
    os.execvp(child_cmd[0], child_cmd)


def _make_emitter() -> "Any":
    """Build a best-effort ``emit(level, message)`` callable backed by the SDK logger.

    Returns a callable that never raises. If the SDK logger can't be set up, the
    callable falls back to writing the message to stderr so the line is never
    silently dropped.
    """

    def _stderr_fallback(_level: str, message: str) -> None:
        # Deliberate stderr write: this path runs when the SDK logger itself is
        # unavailable/failing, so we must not route through it.
        print(message, file=sys.stderr, flush=True)  # noqa: T201

    try:
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — deferred: avoid importing the heavy logging stack at module load
            get_logger,
        )

        logger = get_logger("dapr.runtime")
    except Exception:  # pragma: no cover - defensive: logging must never break daprd
        return _stderr_fallback

    def _emit(level: str, message: str) -> None:
        try:
            method = getattr(logger, _LEVEL_TO_METHOD.get(level, "info"), logger.info)
            method(message)
        except Exception:
            _stderr_fallback(level, message)

    return _emit


def _format_line(raw: str) -> "tuple[str, str]":
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


def _flush_observability() -> None:
    """Flush buffered observability records so daprd logs aren't lost on exit."""
    try:
        import asyncio  # noqa: PLC0415 — deferred: only needed on shutdown

        from application_sdk.observability.observability import (  # noqa: PLC0415 — deferred: cold shutdown path
            AtlanObservability,
        )

        asyncio.run(AtlanObservability.flush_all())
    except Exception:  # noqa: S110 — pragma: no cover; best-effort shutdown flush
        pass


def main(argv: "list[str] | None" = None) -> int:
    """Run daprd as a child and forward its logs to the observability pipeline."""
    argv = list(sys.argv if argv is None else argv)
    child_cmd = _split_child_command(argv)
    if not child_cmd:
        print(  # noqa: T201 — startup arg error, before any logger is set up
            "dapr_log_forwarder: no child command supplied (expected '-- daprd ...')",
            file=sys.stderr,
        )
        return 2

    # Defensive double-gate: if forwarding is off, behave exactly like running
    # the child directly. The entrypoint only wraps when the flag is on, but this
    # keeps the module safe to invoke unconditionally.
    from application_sdk.constants import (  # noqa: PLC0415 — deferred: read at call time so the flag is patchable/env-fresh
        ENABLE_DAPR_LOG_FORWARDING,
    )

    if not ENABLE_DAPR_LOG_FORWARDING:
        _exec_transparently(child_cmd)  # never returns

    emit = _make_emitter()

    proc = subprocess.Popen(
        child_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered
    )

    # Forward termination signals to daprd; let its graceful shutdown run and the
    # read loop drain to EOF naturally rather than exiting abruptly here.
    def _forward_signal(signum: int, _frame: "Any") -> None:
        if proc.poll() is None:
            try:
                proc.send_signal(signum)
            except Exception:  # noqa: S110 — pragma: no cover; daprd may have just exited
                pass

    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line:
                continue
            try:
                level, message = _format_line(line)
                emit(level, message)
            except Exception:
                # Forwarding must never interrupt the stream — fall back to raw.
                print(line.rstrip("\n"), file=sys.stderr, flush=True)  # noqa: T201
    finally:
        returncode = proc.wait()
        _flush_observability()

    return returncode


if __name__ == "__main__":
    sys.exit(main())
