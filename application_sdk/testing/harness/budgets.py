"""One typed budget for every bounded wait in the harness.

Today "budget" is not one layer. It is ten class attributes and six budgets on
``BaseE2ETest``, plus seven more knobs hard-coded in ``testing/e2e/client.py``
and never exposed (``_HTTP_TIMEOUT``, ``_SUBMIT_TIMEOUT``,
``_REQUEST_MAX_ATTEMPTS``, ``_REQUEST_BACKOFF_SECONDS``,
``_MAX_RETRY_AFTER_SECONDS``, ``_RETRY_AFTER_BUDGET_SECONDS``,
``_HEARTBEAT_SECONDS``), plus ``max_forbidden_attempts`` and
``max_not_found_attempts``. :class:`Budget` absorbs the waits into one value a
call site can pass, :class:`RequestBudget` absorbs the per-call retry knobs, and
:class:`BudgetProfile` names a per-tier set of both — which is what a scenario
suite needs when the same wait has different timings against ``kind`` than
against a tenant.

:data:`CONNECTOR_CI` is that set for the tier the SDK ships today, populated
from ``BaseE2ETest``'s ``ClassVar`` defaults verbatim (child B on FND-224).
Verbatim is checked, not asserted in prose: ``test_budgets.py`` reads the class
attributes back off ``BaseE2ETest`` and compares, so the profile cannot drift
away from the class it was lifted from before child H rewires the class to read
it. No second profile ships here — the tiers that want different numbers
(``kind``, a shared tenant) land with the consumer that knows what those numbers
are, and inventing them now would put values in the SDK that nothing has ever
run.

``timedelta`` rather than bare seconds throughout: an ``int`` named
``..._seconds`` is a unit convention that a call site can silently violate, and
this vocabulary is about to be shared across three repos. Two of the absorbed
knobs are attempt *counts* rather than durations —
``poll_atlas_for_connection``'s ``max_not_found_attempts``, and
``submit_workflow``'s ``retries`` — and both convert cleanly, because an attempt
cap on a fixed-interval loop is a duration wearing a disguise. See
:data:`CONNECTOR_CI` for each conversion.

**On the clock (D3).** The FND-224 decomposition asked for a ``Budget.clock``
mode preserving the ``elapsed += interval_seconds`` accumulator — a bug that
never charged HTTP round-trip time to the budget, so ``ae_poll_timeout_seconds
= 600`` bounded 600s of *sleeps* plus N round trips at up to 60s each. That
accumulator no longer exists: every deadline loop in ``testing/e2e/`` now runs
through :mod:`application_sdk.testing.harness._poll`, which is monotonic and
re-clamps each gap against the clock read *after* the probe. So there is no
current behaviour to preserve, and no ``clock`` field here — shipping one would
be reintroducing the bug as a supported mode. See the D3 note on FND-224.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

__all__ = [
    "CONNECTOR_CI",
    "Budget",
    "BudgetProfile",
    "Call",
    "RequestBudget",
    "Wait",
]


class Wait(StrEnum):
    """The bounded waits a connector run performs, as profile keys.

    A :class:`StrEnum` rather than bare strings so a profile lookup is a name
    the type checker knows, while ``profile.budgets["ae_run"]`` still works for
    a scenario suite reading its keys out of a config file.
    """

    #: The LOCAL CI worker container's ``/server/health``.
    WORKER_HEALTH = "worker_health"
    #: The TENANT-installed app pod becoming reachable, observed only through
    #: the AE submit's own retry — there is no other tenant-facing probe.
    APP_READY = "app_ready"
    #: AE serving a published version that supersedes the harness's seed.
    DEPLOYED_MANIFEST = "deployed_manifest"
    #: The AE run's ``native-status`` reaching a terminal state.
    AE_RUN = "ae_run"
    #: The Connection becoming searchable in Atlas.
    ATLAS_CONNECTION = "atlas_connection"
    #: Per-type asset counts settling after publish.
    ATLAS_ASSET_COUNTS = "atlas_asset_counts"


class Call(StrEnum):
    """The outbound call shapes a run makes, as profile keys."""

    #: Every tenant HTTP call except the submit.
    HTTP = "http"
    #: The AE submit, which is non-idempotent and gets its own budget.
    SUBMIT = "submit"


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
        max_transient_failures: Length of the *consecutive* probe-error streak
            that ends the wait with
            :class:`~application_sdk.testing.harness.outcome.Indeterminate`. The
            Nth consecutive error is the one that gives up, so ``5`` absorbs
            four and stops on the fifth — the boundary
            ``poll_native_status`` has today, pinned by
            ``test_gives_up_at_max_transient_failures``, and preserved here
            rather than corrected because a drift would change every connector's
            tolerance the moment child D rewired the loop — which it has now
            done, on this budget, with the same boundary.

            ``0`` and ``1`` are therefore the same instruction: end on the first
            error. FND-240 normalised that degenerate pair by *pinning* it rather
            than by making them differ — ``0`` reads naturally as "no tolerance"
            and already means exactly that, and the alternative (shifting the
            boundary so ``N`` absorbs ``N``) would change every connector's
            tolerance to buy a tidier arithmetic. Every reader of the field
            handles the pair the same way, and
            ``test_the_degenerate_pair_both_give_up_on_the_first_error`` is what
            stops one of them quietly disagreeing.

            A streak, not a total: any successful probe resets it, so a wait
            spanning a twenty-minute VPN tunnel is not bounded by the *sum* of
            the blips it survived. Set per backend — a kubectl read over a
            tunnel and an in-process HTTP call have different normal failure
            rates (FND-227's 2026-08-17 amendment).
        retry_after_budget: Ceiling on the *extra* waiting an origin may request
            via ``Retry-After`` across the whole wait, on top of the fixed gaps.
            Without it a slow origin can stretch the real wall-clock bound well
            past :attr:`timeout`. ``None`` means honour no origin backoff.
        max_retry_after: Ceiling on any *single* origin-requested wait, so one
            pathological value cannot hang a CI leg inside its own backoff.
            Same field, same meaning, as
            :attr:`RequestBudget.max_retry_after`: a wait that honours origin
            backoff needs both bounds, and only one of them was here.
            ``None`` honours whatever the origin asks for, up to
            :attr:`retry_after_budget`.
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
    max_retry_after: timedelta | None = None
    heartbeat: timedelta | None = timedelta(seconds=30)


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestBudget:
    """Everything one outbound call, including its retries, is allowed to spend.

    Separate from :class:`Budget` because a request retry is not a bounded wait
    and folding it into one would make :attr:`Budget.timeout` mean two things —
    a ceiling on the whole loop in one case, a ceiling on each attempt in the
    other. A retry loop has no total wall-clock bound today; giving it one here
    would be inventing a number rather than absorbing one.

    Attributes:
        timeout: Ceiling on a single attempt. The loop above it is what bounds
            the whole call.
        max_attempts: Total attempts including the first, so ``1`` means no
            retry.
        backoff: Fixed gap between attempts, before any origin-requested
            backoff is honoured.
        max_retry_after: Ceiling on any *single* origin-requested ``Retry-After``
            wait, so one pathological value cannot hang a CI leg. ``None``
            honours whatever the origin asks for.
        retry_after_budget: Ceiling on the total honoured-above-the-fixed-gap
            waiting across the whole call. Once spent, the remaining attempts
            fall back to :attr:`backoff`.
    """

    timeout: timedelta
    max_attempts: int = 1
    backoff: timedelta = timedelta(0)
    max_retry_after: timedelta | None = None
    retry_after_budget: timedelta | None = None


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
        requests: Call label -> per-call budget.
    """

    name: str
    budgets: Mapping[str, Budget]
    requests: Mapping[str, RequestBudget] = field(default_factory=dict)


#: Today's connector-CI tier: ``BaseE2ETest``'s ``ClassVar`` defaults and
#: ``testing/e2e/client.py``'s module constants, verbatim.
#:
#: Every value here has exactly one source in the tree, and
#: ``tests/unit/testing/harness/test_budgets.py`` reads that source back and
#: compares — so this is a second spelling of those numbers, never a second
#: opinion about them.
#:
#: Two entries convert an attempt cap into the duration it already was:
#:
#: * ``ATLAS_CONNECTION.start_grace`` is ``poll_atlas_for_connection``'s
#:   ``max_not_found_attempts = 10``. Every probe that reaches the check is an
#:   empty search (a hit returns immediately), so the cap fires on attempt 10 —
#:   at ``9 x 30s`` elapsed, since attempt 1 runs at zero. "The connection never
#:   appeared" is what
#:   :class:`~application_sdk.testing.harness.outcome.NeverStarted` says, so it
#:   is the start-grace latch rather than a guard of its own.
#: * ``APP_READY`` is ``cold_start_submit_kwargs``, which divides the same two
#:   numbers back into ``retries`` and ``retry_sleep_seconds`` for the submit's
#:   own retry loop.
#:
#: ``max_forbidden_attempts`` is absorbed by being dropped: the Atlas poll went
#: through the search index, whose ACL is permissive, so the knob stopped being
#: reachable and ``poll_atlas_for_connection`` already ``del``\\s it unread.
CONNECTOR_CI = BudgetProfile(
    name="connector_ci",
    budgets={
        Wait.WORKER_HEALTH: Budget(
            timeout=timedelta(seconds=120),
            poll_interval=timedelta(seconds=3),
        ),
        Wait.APP_READY: Budget(
            timeout=timedelta(seconds=300),
            poll_interval=timedelta(seconds=5),
            # Bounds the extra waiting an overloaded AE can request on top of
            # the fixed gaps, so the ~5 min headline cannot silently become ~10.
            retry_after_budget=timedelta(seconds=300),
            # Retries inside a single submit call; no poll loop to narrate.
            heartbeat=None,
        ),
        Wait.DEPLOYED_MANIFEST: Budget(
            timeout=timedelta(seconds=60),
            poll_interval=timedelta(seconds=5),
            heartbeat=None,
        ),
        Wait.AE_RUN: Budget(
            timeout=timedelta(seconds=600),
            poll_interval=timedelta(seconds=10),
            start_grace=timedelta(seconds=180),
            # Derived from the ceiling rather than pinned, so raising the
            # ceiling widens the watchdog instead of putting it out of reach:
            # max(600 // 3, 300) clamped to 600 // 2.
            stall_timeout=timedelta(seconds=300),
            max_transient_failures=5,
            retry_after_budget=timedelta(seconds=300),
            # The same per-single-wait cap _retry_gap already applies inside
            # this loop; it had no home on Budget until child C needed to
            # honour origin backoff from inside the primitive.
            max_retry_after=timedelta(seconds=120),
            # poll_native_status throttles its own richer progress line to the
            # heartbeat cadence and disables the generic one.
            heartbeat=None,
        ),
        Wait.ATLAS_CONNECTION: Budget(
            timeout=timedelta(seconds=1500),
            poll_interval=timedelta(seconds=30),
            start_grace=timedelta(seconds=270),
            # The other half of the same ``max_not_found_attempts = 10``. The
            # cap it replaced bounded ten consecutive *non-positive* probes,
            # and a search that errored was one of them — which is how an Atlas
            # outage came to be reported as "the connection never materialised".
            # Splitting the one number in two keeps the total tolerance
            # identical and separates the two diagnoses: ten empty reads is
            # NeverStarted, ten unreadable ones is Indeterminate.
            max_transient_failures=10,
            heartbeat=None,
        ),
        Wait.ATLAS_ASSET_COUNTS: Budget(
            timeout=timedelta(seconds=15),
            poll_interval=timedelta(seconds=5),
            heartbeat=None,
        ),
    },
    requests={
        Call.HTTP: RequestBudget(
            timeout=timedelta(seconds=60),
            max_attempts=4,
            backoff=timedelta(seconds=3),
            max_retry_after=timedelta(seconds=120),
            retry_after_budget=timedelta(seconds=300),
        ),
        Call.SUBMIT: RequestBudget(
            # Longer than the rest so a Cloudflare 504 arrives as an HTTP error
            # the retry loop understands, rather than a raw TimeoutError.
            timeout=timedelta(seconds=120),
            max_attempts=4,
            backoff=timedelta(seconds=3),
            max_retry_after=timedelta(seconds=120),
            retry_after_budget=timedelta(seconds=300),
        ),
    },
)
