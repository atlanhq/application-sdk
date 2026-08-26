"""One typed budget for every bounded wait in the harness.

Today "budget" is not one layer. It is ten class attributes and six budgets on
``BaseE2ETest``, plus seven more knobs hard-coded in ``testing/e2e/client.py``
and never exposed (``_HTTP_TIMEOUT``, ``_SUBMIT_TIMEOUT``,
``_REQUEST_MAX_ATTEMPTS``, ``_REQUEST_BACKOFF_SECONDS``,
``_MAX_RETRY_AFTER_SECONDS``, ``_RETRY_AFTER_BUDGET_SECONDS``,
``_HEARTBEAT_SECONDS``), plus ``max_forbidden_attempts`` and
``max_not_found_attempts``. :class:`Budget` absorbs all of it into one value a
call site can pass, and :class:`BudgetProfile` names a per-tier set of them —
which is what a scenario suite needs when the same wait has different timings
against ``kind`` than against a tenant.

Populating the named profiles from today's ``ClassVar`` defaults, verbatim, is
child B. This module scaffolds the types they are expressed in.

``timedelta`` rather than bare seconds throughout: an ``int`` named
``..._seconds`` is a unit convention that a call site can silently violate, and
this vocabulary is about to be shared across three repos.

**On the clock (D3).** The FND-224 decomposition asked for a ``Budget.clock``
mode preserving the ``elapsed += interval_seconds`` accumulator — a bug that
never charged HTTP round-trip time to the budget, so ``ae_poll_timeout_seconds
= 600`` bounded 600s of *sleeps* plus N round trips at up to 60s each. That
accumulator no longer exists: every deadline loop in ``testing/e2e/`` now runs
through :mod:`application_sdk.testing.e2e._poll`, which is monotonic and
re-clamps each gap against the clock read *after* the probe. So there is no
current behaviour to preserve, and no ``clock`` field here — shipping one would
be reintroducing the bug as a supported mode. See the D3 note on FND-224.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

__all__ = ["Budget", "BudgetProfile"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Budget:
    """Everything a single bounded wait is allowed to spend.

    Attributes:
        timeout: Total wall-clock bound for the whole wait, on a monotonic
            clock. Includes probe time, not just the gaps between probes.
        poll_interval: Nominal gap between probes. The real gap is clamped
            against the remaining budget, so a probe that outlasts its own
            interval shortens the next gap rather than overshooting the timeout.
        start_grace: How long to allow before *anything* has started, after
            which the wait returns
            :class:`~application_sdk.testing.harness.outcome.NeverStarted`.
            ``None`` disables the latch — correct for a wait against shared or
            autoscaled infrastructure, where legitimate pickup can outlast any
            fixed grace.
        stall_timeout: How long the progress fingerprint may go unchanged once
            work *has* started, after which the wait returns
            :class:`~application_sdk.testing.harness.outcome.Stalled`. ``None``
            disables the watchdog.
        max_transient_failures: How many probe errors to absorb before giving up
            with :class:`~application_sdk.testing.harness.outcome.Indeterminate`.
            ``0`` means the first probe error ends the wait.
        retry_after_budget: Ceiling on the *extra* waiting an origin may request
            via ``Retry-After`` across the whole wait, on top of the fixed gaps.
            Without it a slow origin can stretch the real wall-clock bound well
            past :attr:`timeout`. ``None`` means honour no origin backoff.
        heartbeat: Cadence of the "still waiting" progress line. ``None``
            silences it, which is right when the call site logs its own richer
            per-poll progress and a second line would be duplicate noise.
    """

    timeout: timedelta
    poll_interval: timedelta
    start_grace: timedelta | None = None
    stall_timeout: timedelta | None = None
    max_transient_failures: int = 0
    retry_after_budget: timedelta | None = None
    heartbeat: timedelta | None = timedelta(seconds=30)


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetProfile:
    """A named set of budgets for one execution tier.

    The same wait wants different timings against a local ``kind`` cluster, a
    docker-compose worker in connector CI, and a shared tenant. Naming the set
    means a suite selects a tier rather than re-deriving nine numbers.

    Attributes:
        name: Tier identifier, for reports and for the selecting env var.
        budgets: Wait label -> budget. Keys are the ``label`` values the waits
            pass, so a report can say which profile governed a given wait.
    """

    name: str
    budgets: dict[str, Budget]
