"""Size-tier routing: pick the worker pool an activity should run on.

One activity's memory demand varies by orders of magnitude across a fleet. A
single pool sized for the large end wastes memory on every small run; sized for
the small end it OOM-kills the large ones. Size tiers give an app several pools
with different envelopes, start each activity on the one its measured inputs
ask for, and leave ``temporal-activity-rerouter`` to move the few that turn out
to need more.

The split, and what is safe to call from where:

* :mod:`~application_sdk.tiering.ladder` — ``Band`` / ``Axis`` /
  ``ActivitySizer`` / ``Ladder``. Pure: activity + measured signal → tier, and
  tier → next tier. Safe in workflow code.
* :mod:`~application_sdk.tiering.queues` — tier → task queue, from
  ``ATLAN_APP_SIZE_QUEUE_MAP``. ``queue_map_from_env`` reads the environment and
  must be called from an activity; ``resolve`` and ``tier_of_queue`` are pure.
* :mod:`~application_sdk.tiering.dispatch` — ``run_on_queue``, which hands the
  queue to the SDK's own task wrapper.

An app supplies its own ladder: its tiers, their declared memory, and one sizer
per activity.

**Classification is total** — every activity always names a tier, because an
activity with no tier is one the rerouter will not escalate. Inertness lives in
``queues.resolve``: with ``ATLAN_APP_SIZE_QUEUE_MAP`` unset every tier resolves
to ``None`` and every activity keeps the queue it has today. A build carrying a
ladder routes nothing until the deployment sets that variable, and unsetting it
is the rollback.

Order of operations when turning this on, learned the hard way: the escalation
lane must be live *before* the queue map is set. Pin-and-climb with nothing to
climb to is strictly worse than not routing at all.
"""

from application_sdk.tiering.dispatch import run_on_queue
from application_sdk.tiering.ladder import INF, ActivitySizer, Axis, Band, Ladder, Tier
from application_sdk.tiering.queues import (
    SIZE_QUEUE_MAP_ENV,
    queue_map_from_env,
    resolve,
    tier_of_queue,
)

__all__ = [
    "INF",
    "SIZE_QUEUE_MAP_ENV",
    "ActivitySizer",
    "Axis",
    "Band",
    "Ladder",
    "Tier",
    "queue_map_from_env",
    "resolve",
    "run_on_queue",
    "tier_of_queue",
]
