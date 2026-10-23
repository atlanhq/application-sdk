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

The names above are re-exported lazily, so importing a ladder in a workflow
module does not also load the activity-side half of the package — see
``_OWNER`` below for what that costs when it is eager.

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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import shapes for type checkers only
    from application_sdk.tiering.dispatch import run_on_queue
    from application_sdk.tiering.ladder import (
        INF,
        ActivitySizer,
        Axis,
        Band,
        Ladder,
        Tier,
    )
    from application_sdk.tiering.queues import (
        SIZE_QUEUE_MAP_ENV,
        queue_map_from_env,
        resolve,
        tier_of_queue,
    )

#: Which submodule owns each exported name, for the lazy import below.
#:
#: Eager re-exports would make ``from application_sdk.tiering import Ladder``
#: — the workflow-side import — also load ``queues``, whose module-level logger
#: pulls in roughly 800 modules including pydantic, httpx and the temporalio
#: API surface. The ladder is meant to be askable from workflow code without
#: dragging the activity-side half of the package in behind it, so each name is
#: resolved on first use instead (PEP 562).
#:
#: One consequence to know about: ``gen_api_files.py`` computes the api
#: distribution's file list by following module-level imports, and neither a
#: ``TYPE_CHECKING`` import nor the ``importlib`` call below is visible to it.
#: Nothing in this package is an api seed today, so the list is correct. If one
#: ever becomes a seed, list the submodules in that generator's input rather
#: than assuming the closure found them.
_OWNER: dict[str, str] = {
    "INF": "ladder",
    "ActivitySizer": "ladder",
    "Axis": "ladder",
    "Band": "ladder",
    "Ladder": "ladder",
    "Tier": "ladder",
    "run_on_queue": "dispatch",
    "SIZE_QUEUE_MAP_ENV": "queues",
    "queue_map_from_env": "queues",
    "resolve": "queues",
    "tier_of_queue": "queues",
}


def __getattr__(name: str) -> Any:
    """Resolve an exported name from its submodule on first access."""
    module = _OWNER.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib  # noqa: PLC0415 - deferred so the package root stays cheap

    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value  # resolve once, then it is an ordinary attribute
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


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
