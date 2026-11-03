"""Tests for the background-thread console sink (FND-4075)."""

import asyncio
import os
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import TextIO
from unittest import mock

import pytest
from loguru import logger

from application_sdk.observability.console_sink import ConsoleWriter, flush_console_logs
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter

#: How long the loop gets to log past a stalled pipe. A healthy run takes well
#: under a second; this only decides how long a regression takes to fail.
_STALL_SECONDS = 30.0


class _GatedStream:
    """A stream whose ``write`` blocks until ``release`` is set, like a full pipe."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.lines: list[str] = []

    def write(self, text: str) -> int:
        self.entered.set()
        self.release.wait()
        self.lines.append(text)
        return len(text)

    def flush(self) -> None:
        pass


@contextmanager
def _unread_pipe() -> Generator[tuple[Callable[[], None], TextIO], None, None]:
    """Yield ``(start_draining, stream)`` for a pipe nobody reads.

    ``start_draining`` starts reading the pipe so a writer blocked on it is
    released. It also runs on exit, before both ends are closed.
    """
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(write_fd, "w")
    started = threading.Event()

    def _drain() -> None:
        try:
            while os.read(read_fd, 65536):
                pass
        except OSError:
            pass
        finally:
            os.close(read_fd)

    def start_draining() -> None:
        if not started.is_set():
            started.set()
            threading.Thread(target=_drain, daemon=True).start()

    try:
        yield start_draining, stream
    finally:
        start_draining()
        flush_console_logs(5.0)
        stream.close()


def test_stalled_stdout_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logging from the loop to a pipe nobody reads must not stall the loop.

    About 600 KB is far past a kernel pipe buffer (64 KB on Linux and macOS).
    With a loguru stream sink, the first write past the buffer blocks the
    loop's thread, so ``logged`` never gets set and the heartbeat stops.

    Nothing drains the pipe until the wait below gives up, so a blocked write
    can never finish inside it: the bound only decides how long a regression
    takes to fail. An earlier version also bounded the gap between heartbeat
    ticks, which measured GIL contention with the writer thread on the runner
    (about 2s on a loaded macOS runner) and failed at random (FND-4233).
    """
    logged = threading.Event()

    async def main(adapter: AtlanLoggerAdapter) -> None:
        payload = "x" * 200
        for i in range(3000):
            adapter.info("%s %d", payload, i)
            if i % 100 == 0:
                await asyncio.sleep(0)
        logged.set()

    with _unread_pipe() as (start_draining, stream):
        monkeypatch.setattr(sys, "stdout", stream)
        AtlanLoggerAdapter._reset_for_testing()
        with mock.patch.dict(
            "os.environ", {"LOG_LEVEL": "INFO", "ENABLE_OTLP_LOGS": "false"}
        ):
            adapter = AtlanLoggerAdapter("console_stall_probe")
        loop_thread = threading.Thread(
            target=lambda: asyncio.run(main(adapter)), daemon=True
        )
        loop_thread.start()
        finished = logged.wait(timeout=_STALL_SECONDS)
        # Release a blocked writer before touching loguru. Without the fix,
        # the loop thread holds the stdout handler's lock inside write(), and
        # logger.remove() would wait on that lock indefinitely.
        start_draining()
        loop_thread.join(timeout=5.0)
        # Restore console sinks on the real stdout before the pipe closes,
        # so later records don't go to a dead stream.
        monkeypatch.undo()
        AtlanLoggerAdapter._reset_for_testing()
        AtlanLoggerAdapter("test_logger")
        assert finished, "event loop blocked writing to a stalled stdout"


def test_full_queue_drops_oldest_and_counts() -> None:
    writer = ConsoleWriter(max_records=3, drop_warning_interval=0.0)
    stream = _GatedStream()

    writer.put(stream, "first\n")
    # The writer thread now holds "first" and is blocked in write().
    assert stream.entered.wait(timeout=5.0)
    for i in range(5):
        writer.put(stream, f"queued-{i}\n")

    assert writer.dropped == 2
    stream.release.set()
    assert writer.flush(timeout=5.0)

    written = "".join(stream.lines)
    assert "queued-0" not in written
    assert "queued-1" not in written
    for i in (2, 3, 4):
        assert f"queued-{i}" in written
    assert "dropped 2 records (total 2)" in written


def test_drop_warning_is_rate_limited() -> None:
    writer = ConsoleWriter(max_records=1, drop_warning_interval=3600.0)
    stream = _GatedStream()
    writer.put(stream, "first\n")
    assert stream.entered.wait(timeout=5.0)
    for i in range(4):
        writer.put(stream, f"queued-{i}\n")
    stream.release.set()
    assert writer.flush(timeout=5.0)

    writer.put(stream, "second-burst-a\n")
    writer.put(stream, "second-burst-b\n")
    assert writer.flush(timeout=5.0)

    warnings = [line for line in stream.lines if "console log queue full" in line]
    assert len(warnings) == 1


def test_flush_times_out_while_stream_is_stalled() -> None:
    writer = ConsoleWriter()
    stream = _GatedStream()
    writer.put(stream, "stuck\n")
    assert stream.entered.wait(timeout=5.0)

    assert writer.flush(timeout=0.05) is False

    stream.release.set()
    assert writer.flush(timeout=5.0) is True
    assert stream.lines == ["stuck\n"]


def test_formatting_runs_on_the_logging_thread() -> None:
    """The format callable must run where contextvars are set, not on the writer thread."""
    writer = ConsoleWriter()
    stream = _GatedStream()
    stream.release.set()
    format_threads: list[str] = []

    def fmt(record: dict) -> str:
        format_threads.append(threading.current_thread().name)
        return "{message}\n"

    sink_id = logger.add(writer.sink(stream), format=fmt)
    try:
        logger.info("rendered-here")
    finally:
        logger.remove(sink_id)
    assert writer.flush(timeout=5.0)

    assert format_threads == [threading.current_thread().name]
    assert stream.lines == ["rendered-here\n"]


@pytest.mark.parametrize("max_records", [0, -1])
def test_non_positive_capacity_is_rejected(max_records: int) -> None:
    with pytest.raises(ValueError, match="max_records"):
        ConsoleWriter(max_records=max_records)


class _FailOnceStream(_GatedStream):
    """A gated stream that rejects the first drop warning written to it."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def write(self, text: str) -> int:
        if "console log queue full" in text and not self.failed:
            self.failed = True
            raise OSError("broken pipe")
        return super().write(text)


def test_failed_drop_warning_is_retried() -> None:
    writer = ConsoleWriter(max_records=1, drop_warning_interval=0.0)
    stream = _FailOnceStream()
    writer.put(stream, "first\n")
    assert stream.entered.wait(timeout=5.0)
    writer.put(stream, "queued-0\n")
    writer.put(stream, "queued-1\n")
    stream.release.set()
    assert writer.flush(timeout=5.0)
    assert stream.failed
    assert not any("console log queue full" in line for line in stream.lines)

    # The next written record carries the warning that failed before.
    writer.put(stream, "after\n")
    assert writer.flush(timeout=5.0)
    assert any("dropped 1 records" in line for line in stream.lines)
