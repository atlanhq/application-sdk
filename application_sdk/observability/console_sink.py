"""Console log sink whose writes happen on a background thread.

loguru's stream sinks call ``stream.write`` on the thread that logs. When
stdout or stderr stalls, for example on a full pipe or a slow log collector,
that thread blocks, and when it's the event loop's thread the activity
heartbeats stop with it.

:class:`ConsoleWriter` separates formatting from writing. loguru still renders
each record on the calling thread, because the sink is a plain callable, so
contextvars such as trace and correlation ids and the ``diagnose`` setting
apply exactly as before. The sink only appends the finished string to a
bounded in-memory queue. A single daemon thread performs the blocking
``write`` and ``flush``.

loguru's ``enqueue=True`` is deliberately not used: it pickles every record
and runs its own unbounded queue.

When the queue is full, the oldest record is dropped. Each drop is counted, and
a rate-limited WARNING line goes to the console once the stream accepts writes
again.
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from collections import deque
from typing import Callable, TextIO

# The most records allowed to wait for the writer thread. At a typical
# 200-byte line this caps the queue at about 2 MB.
CONSOLE_QUEUE_MAX_RECORDS = 10_000

# The shortest gap between two dropped-record warnings, so a long stall
# produces one line every interval rather than one per drop.
DROP_WARNING_INTERVAL_SECONDS = 30.0


class ConsoleWriter:
    """A bounded queue of pre-formatted console lines, drained by one daemon thread."""

    def __init__(
        self,
        max_records: int = CONSOLE_QUEUE_MAX_RECORDS,
        drop_warning_interval: float = DROP_WARNING_INTERVAL_SECONDS,
    ) -> None:
        if max_records < 1:
            raise ValueError(f"max_records must be at least 1, got {max_records}")
        self._max_records = max_records
        self._drop_warning_interval = drop_warning_interval
        self._init_state()

    def _init_state(self) -> None:
        self._pid = os.getpid()
        self._cond = threading.Condition(threading.Lock())
        self._queue: deque[tuple[TextIO, str]] = deque()
        # Counts a record popped off the queue but not yet written, so
        # flush() doesn't return while a write is still in progress.
        self._in_flight = 0
        self._dropped = 0
        self._dropped_reported = 0
        self._last_drop_warning = float("-inf")
        self._thread: threading.Thread | None = None

    @property
    def dropped(self) -> int:
        """Total records dropped because the queue was full."""
        return self._dropped

    def sink(self, stream: TextIO) -> Callable[[str], None]:
        """Return a loguru sink that queues each formatted message for *stream*.

        The stream is bound when the sink is created, which matches how
        loguru's own stream sink treats ``sys.stdout``.
        """

        def _write(message: str) -> None:
            # str() removes loguru's Message subclass, so the queue doesn't
            # keep the record dict alive (exception, traceback, frames).
            self.put(stream, str(message))

        return _write

    def put(self, stream: TextIO, text: str) -> None:
        """Queue *text* for *stream* without blocking. When full, drop the oldest record."""
        if self._pid != os.getpid():
            # A forked child inherits the queue and lock but not the thread.
            self._init_state()
        with self._cond:
            # max_records >= 1 is enforced in __init__, so a full queue is never empty here.
            if len(self._queue) >= self._max_records:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append((stream, text))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="atlan-console-log-writer", daemon=True
                )
                self._thread.start()
            self._cond.notify_all()

    def flush(self, timeout: float) -> bool:
        """Wait until all queued records are written, or until *timeout* seconds pass.

        Returns ``True`` when the queue fully drained. Never call this on an
        event loop's thread: it blocks the caller for up to *timeout* seconds.
        """
        if self._pid != os.getpid():
            return True
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                stream, text = self._queue.popleft()
                self._in_flight = 1
                pending_drops = self._dropped - self._dropped_reported
            try:
                self._write(stream, text)
                if pending_drops and self._drop_warning_due():
                    # Count the drops as reported only once the warning is
                    # actually written. A failed write is retried after the
                    # next interval.
                    if self._write(stream, self._drop_warning(pending_drops)):
                        with self._cond:
                            self._dropped_reported += pending_drops
            finally:
                with self._cond:
                    self._in_flight = 0
                    self._cond.notify_all()

    def _drop_warning_due(self) -> bool:
        now = time.monotonic()
        if now - self._last_drop_warning < self._drop_warning_interval:
            return False
        self._last_drop_warning = now
        return True

    def _drop_warning(self, count: int) -> str:
        return (
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} [WARNING] "
            f"{__name__} - console log queue full: dropped {count} records "
            f"(total {self._dropped}); stdout/stderr is not keeping up\n"
        )

    @staticmethod
    def _write(stream: TextIO, text: str) -> bool:
        """Write and flush *text*. Return ``False`` if the stream rejected it."""
        try:
            stream.write(text)
            stream.flush()
        # A closed or broken stream (ValueError, OSError) must not kill the
        # writer thread. There's nowhere left to report it, so the line is lost.
        # conformance: ignore[E002] the failing sink is the console itself; logging would recurse into it
        except (OSError, ValueError):
            return False
        return True


# One writer for the whole process: one thread and one ordering across
# stdout and stderr, however many times the console sinks are registered.
console_writer = ConsoleWriter()


def console_log_drop_count() -> int:
    """Return how many console log records were dropped because stdout or stderr fell behind."""
    return console_writer.dropped


def flush_console_logs(timeout: float = 5.0) -> bool:
    """Wait up to *timeout* seconds for queued console records to be written.

    Safe for shutdown paths and for threads without a running event loop.
    Returns ``True`` when everything was written.
    """
    return console_writer.flush(timeout)


# Interpreter exit stops daemon threads where they stand, so give the writer a
# bounded window to empty the queue first. A stalled stream can't hold exit
# open past the timeout.
atexit.register(flush_console_logs, 2.0)
