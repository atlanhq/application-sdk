"""The warmup tier's settings, poll cadence and bounded probe, for every surface.

Handler-side, so the ``/workflows/v1/warmup`` route and the injected gate read
one set of numbers (FND-3280: imports run worker → handler only). The gate
re-exports what it needs from here. Nothing here touches Temporal, and every
function is pure or awaits only the probe it is handed, so the deterministic
workflow may call the settings and cadence helpers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import SourceUnavailableError
from application_sdk.handler.contracts import WarmupObservation

#: ``App.preflight_warmup_ceiling_seconds``: how long the gate waits, from gate
#: start, for the warmup to report ``READY``. Floored, never capped: any cap is
#: a guess about someone's warehouse that would keep needing revision.
WARMUP_CEILING_DEFAULT_SECONDS = 600
WARMUP_CEILING_MIN_SECONDS = 30

#: ``App.preflight_warmup_probe_timeout_seconds``: how long one probe may wait
#: on warm compute. Floored, and never more than the ceiling.
WARMUP_PROBE_TIMEOUT_DEFAULT_SECONDS = 10
WARMUP_PROBE_TIMEOUT_MIN_SECONDS = 1

#: Poll cadence with no hint from the source: 5s, doubling to a 30s cap.
WARMUP_POLL_FIRST_SECONDS = 5
WARMUP_POLL_MAX_BACKOFF_SECONDS = 30

#: The floor on a source's ``next_poll_seconds`` hint. A hint is otherwise
#: honoured as given; this only bounds the load on our own system.
WARMUP_POLL_HINT_FLOOR_SECONDS = 5

#: Categories a warmup probe can raise that end the wait at once: no amount of
#: waiting fixes a wrong password, a missing grant or a missing object.
WARMUP_TERMINAL_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {FailureCategory.AUTH, FailureCategory.PERMISSION, FailureCategory.NOT_FOUND}
)


def clamp_declared_int(
    raw: object, *, low: int, high: int | None, default: int, unit: str
) -> tuple[int, str]:
    """Coerce and clamp a declared ``ClassVar`` int. Returns ``(value, complaint)``.

    Pure and silent so the warn-once boot path and the per-run workflow path can
    share it and cannot disagree about the resulting number. ``complaint`` is
    empty when the declaration was already valid. ``high=None`` is a floor only.
    """
    if raw is None:
        return default, ""
    # bool is an int subclass; True would otherwise clamp to the floor and read
    # as a deliberate declaration.
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return default, f"{raw!r} is not a number"
    try:
        value = int(float(raw))
    # OverflowError, not ValueError, for inf / "1e400" — and this runs on the
    # workflow path, where an escaping exception becomes a workflow *task*
    # failure that Temporal retries indefinitely.
    except (TypeError, ValueError, OverflowError):
        return default, f"{raw!r} is not a usable number"
    clamped = max(low, value if high is None else min(high, value))
    if clamped != value:
        supported = f"at least {low}{unit}" if high is None else f"{low}-{high}{unit}"
        return clamped, f"{value}{unit} is outside the supported range ({supported})"
    return clamped, ""


def warmup_ceiling_seconds(raw: object) -> tuple[int, str]:
    """Clamp a declared ``App.preflight_warmup_ceiling_seconds``. Never raises."""
    return clamp_declared_int(
        raw,
        low=WARMUP_CEILING_MIN_SECONDS,
        high=None,
        default=WARMUP_CEILING_DEFAULT_SECONDS,
        unit="s",
    )


def warmup_probe_timeout_seconds(raw: object, ceiling_seconds: int) -> tuple[int, str]:
    """Clamp a declared ``App.preflight_warmup_probe_timeout_seconds`` into
    ``[1, ceiling]``. Never raises."""
    return clamp_declared_int(
        raw,
        low=WARMUP_PROBE_TIMEOUT_MIN_SECONDS,
        high=ceiling_seconds,
        default=min(WARMUP_PROBE_TIMEOUT_DEFAULT_SECONDS, ceiling_seconds),
        unit="s",
    )


def warmup_poll_delay(
    unhinted_polls: int, hint_seconds: int | None, remaining_seconds: float
) -> float:
    """Seconds to wait before the next warmup probe.

    A source's ``next_poll_seconds`` hint wins, floored at
    :data:`WARMUP_POLL_HINT_FLOOR_SECONDS`. Without one, the wait is 5s doubling
    to 30s, ``unhinted_polls`` being how many unhinted waits came before. Never
    past ``remaining_seconds``: a longer wait becomes one last probe at the
    ceiling.
    """
    if hint_seconds is not None:
        delay = float(max(WARMUP_POLL_HINT_FLOOR_SECONDS, hint_seconds))
    else:
        delay = float(
            min(
                WARMUP_POLL_MAX_BACKOFF_SECONDS,
                WARMUP_POLL_FIRST_SECONDS * 2 ** min(unhinted_polls, 8),
            )
        )
    return max(0.0, min(delay, remaining_seconds))


async def bounded_warmup_probe(
    probe: Awaitable[WarmupObservation], timeout_seconds: float
) -> WarmupObservation | None:
    """Await *probe* for at most *timeout_seconds*; ``None`` if it overran.

    Deliberately not ``asyncio.wait_for``: that cancels the probe and then
    awaits it, so a probe that swallows ``CancelledError`` would hold the caller
    past its bound. The overrunning task is cancelled and abandoned instead, its
    eventual exception consumed so asyncio does not log it on GC. A raise from
    the probe propagates.
    """
    task = asyncio.ensure_future(probe)
    done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    if done:
        return task.result()
    task.cancel()
    task.add_done_callback(lambda f: None if f.cancelled() else f.exception())
    return None


def warmup_progress_line(observation: WarmupObservation) -> str:
    """The observation in the customer's words: the source's own label when it
    gave one (``RESUMING``), else the state, plus any queue depth."""
    line = observation.source_state or observation.state.value
    if observation.queued_queries:
        line += f", {observation.queued_queries} queued"
    return line


def warmup_unavailable_error(
    observation: WarmupObservation, app_name: str
) -> SourceUnavailableError:
    """What an ``UNAVAILABLE`` observation is attributed to: the source, which
    reported that its compute will not get ready on its own."""
    return SourceUnavailableError(
        message=(
            "The source reported its compute as unavailable "
            f"({warmup_progress_line(observation)})"
        ),
        suggested_action=(
            "Check that the warehouse or cluster the connection uses exists, is "
            "enabled and can start, then retry."
        ),
        app_name=app_name,
        retryable=True,
    )
