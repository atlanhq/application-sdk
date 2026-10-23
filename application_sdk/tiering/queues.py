"""Tier → task-queue resolution, read from the environment.

**Not safe to call from workflow code.** :func:`queue_map_from_env` reads
``os.environ``, which the workflow determinism contract forbids. Call it once
per run from the activity that already owns the run's environment reads, and let
the resolved mapping travel on that activity's recorded result. That placement
gives the snapshot-once-per-run property for free: the map cannot change
mid-run, so it cannot change an in-flight command stream.

:func:`resolve` and :func:`tier_of_queue` are pure and may be called anywhere.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import orjson

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.tiering.ladder import Tier

logger = get_logger(__name__)

#: The environment variable carrying ``{tier: task_queue}`` as JSON.
#:
#: The name is not the app's to choose freely: ``temporal-activity-rerouter``
#: reads this same variable off the pod template to build its escalation ladder,
#: so the app and the watcher cannot disagree about which pools form a series.
SIZE_QUEUE_MAP_ENV = "ATLAN_APP_SIZE_QUEUE_MAP"


def queue_map_from_env(env_var: str = SIZE_QUEUE_MAP_ENV) -> dict[Tier, str]:
    """``{tier: task_queue}`` from a JSON env var; ``{}`` on anything unusable.

    Unset, empty, malformed JSON or a non-object yield an empty mapping — which
    routes nothing and leaves every activity where it is. A parse failure is
    warned about rather than raised: a typo in a deployment variable must not
    fail a run that would otherwise have worked exactly as it did yesterday.

    An individual entry is kept only when its tier and queue are both non-empty
    strings. The rest are dropped and named in a warning. They are **not**
    coerced: ``{"s": ["a"]}`` would otherwise become the queue ``"['a']"``, and
    a queue no worker polls is worse than no routing at all — the activity is
    accepted and then sits unscheduled until its start-to-close timeout, with
    every health check green. Dropping the entry leaves that tier unrouted,
    which is the safe direction.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return {}
    try:
        parsed = orjson.loads(raw)
    except ValueError:
        # exc_info carries the decoder's position ("line 1 column 7"), which is
        # the only part of this that tells an operator where the typo is. The
        # value itself is not logged: it is deployment config, and its length
        # is enough to tell "unset" from "mangled".
        logger.warning(
            "Size routing disabled: %s is not valid JSON (%d chars). "
            "Activities keep their current queues.",
            env_var,
            len(raw),
            exc_info=True,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning("Size routing disabled: %s is not a JSON object.", env_var)
        return {}
    usable = {
        k: v for k, v in parsed.items() if _is_queue_name(k) and _is_queue_name(v)
    }
    dropped = sorted(set(parsed) - set(usable))
    if dropped:
        logger.warning(
            "Ignoring %d unusable %s entr%s (%s): a tier and its queue must both be "
            "non-empty strings. Those tiers stay unrouted.",
            len(dropped),
            env_var,
            "y" if len(dropped) == 1 else "ies",
            ", ".join(repr(tier) for tier in dropped),
        )
    return usable


def _is_queue_name(value: object) -> bool:
    """A usable tier name or task queue: a string with something in it."""
    return isinstance(value, str) and bool(value.strip())


def resolve(tier: Tier | None, queues: Mapping[Tier, str]) -> str | None:
    """The task queue for ``tier``, or ``None`` meaning "leave it where it is".

    **This is the whole fail-open surface.** Classification upstream is total —
    every activity always names a tier — so the only thing standing between a
    build carrying this module and a build routing traffic is an empty
    ``queues``. Unset env var, malformed JSON, or a tier the deployment does not
    map: all three answer ``None`` here, and every activity keeps the queue it
    has today.

    That placement is deliberate. Making *classification* decline would mean a
    run's tier depended on how much the fleet measurement happened to know about
    it, and an activity with no tier is an activity the rerouter will not
    escalate — it only moves work *out of* a pool that is a declared tier. So the
    decision to route at all belongs to the deployment, not to the sizer.
    """
    if not tier:
        return None
    return queues.get(tier)


def tier_of_queue(task_queue: str | None, queues: Mapping[Tier, str]) -> Tier | None:
    """The tier whose pool is ``task_queue``, or ``None`` for any other queue.

    The inverse of :func:`resolve`, for reading *where an activity actually ran*
    off its result: the rerouter moves a killed activity to the next pool by
    patching its options server-side, so the only record of the final rung is the
    activity reporting its own ``task_queue``. The default worker and anything
    not in the map answer ``None`` — the same rule the rerouter applies to
    escalation.
    """
    if not task_queue:
        return None
    for tier, queue in queues.items():
        if queue == task_queue:
            return tier
    return None
