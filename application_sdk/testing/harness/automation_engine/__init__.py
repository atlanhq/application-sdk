"""Automation Engine reads and write retries, lifted out of ``testing/e2e/client.py``.

The other half of the ``client.py`` split (child F on FND-224); see
:mod:`application_sdk.testing.harness.atlas` for the reasoning shared by both.

What is specific to this half is the **submit**, which is the one non-idempotent
write in the harness. Re-issuing a submit the origin already processed spawns a
duplicate run, so the retry logic cannot simply count attempts — it has to know
whether a failed request can have taken effect.
:class:`~application_sdk.testing.harness.automation_engine._errors.RequestDelivery`
encodes that (a connect-phase failure never left the client; a follow-up read
that finds no trace proves it was not applied), and it lifts across with the
submit rather than being re-derived.

The retry budget here is also the widest in the harness and for a
counter-intuitive reason: an AE submit is the *only* tenant-facing probe of the
installed app pod on the connector CI path, because the runner has no kubectl
route into the tenant vcluster. A pod that is minutes from serving arrives as a
generic 5xx on submit, so the submit's own retry has to be sized for a cold
start rather than for transient AE overload.

Module map:

``wire``
    The ``native-status`` shape as typed values — the status enums, the per-node
    and per-run results, the published-version record, and the defensive parsers
    that keep an unknown future status from crashing a poll.
``retry``
    The pure predicates the write loops act on: what the origin asked us to
    wait, whether a transport failure can have been delivered, whether a
    conflict is terminal or a rotatable credential-name collision.
``client``
    :class:`~application_sdk.testing.harness.automation_engine.client.AEClient`
    — the async reader and writer over one pooled ``httpx.AsyncClient``.
``_errors``
    The typed leaves those raise, moved here for the same reason ``_poll``
    moved: a harness module cannot raise a leaf that lives in the package child
    H re-expresses over it. ``testing/e2e/_errors`` re-exports every one.

**Two scaffold sketches are not built, deliberately.** ``AERunHandle`` and
``NativeStatus`` were sketched here before the move; FND-242's own text assigns
the *existing* wire types — ``DAGNodeStatus``, ``DAGRunStatus``,
``DAGNodeResult``, ``DAGRunResult`` — to this half, and shipping a second
vocabulary for one reading would leave every consumer choosing between them.
The one field the sketch had that the lifted types lacked, the progress
fingerprint carried on the reading, is
:attr:`~application_sdk.testing.harness.automation_engine.wire.DAGRunResult.fingerprint`.
``submit_run`` / ``poll_native_status`` are methods on
:class:`~application_sdk.testing.harness.automation_engine.client.AEClient`
under their existing names (``submit_workflow`` / ``poll_native_status``) rather
than module-level functions: both need the tenant URL, the token and the pooled
connection, and threading three of those through every call site is a worse
seam than the object that already holds them.
"""

from __future__ import annotations

from application_sdk.testing.harness.automation_engine._errors import (
    AppNotReadyError,
    AtlanAEWorkflowAlreadyActiveError,
    AtlanApiHttpError,
    AtlanApiResponseInvariantError,
    AtlanApiTimeoutError,
    AutomationEngineNotDispatchingError,
    DAGProgressStalledError,
    NoWorkerOnTaskQueueError,
    RequestDelivery,
)
from application_sdk.testing.harness.automation_engine.client import AEClient
from application_sdk.testing.harness.automation_engine.retry import (
    RunLookup,
    WriteRecovery,
    cold_start_submit_kwargs,
)
from application_sdk.testing.harness.automation_engine.wire import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
    PublishedVersion,
)

__all__ = [
    "AEClient",
    "AppNotReadyError",
    "AtlanAEWorkflowAlreadyActiveError",
    "AtlanApiHttpError",
    "AtlanApiResponseInvariantError",
    "AtlanApiTimeoutError",
    "AutomationEngineNotDispatchingError",
    "DAGNodeResult",
    "DAGNodeStatus",
    "DAGProgressStalledError",
    "DAGRunResult",
    "DAGRunStatus",
    "NoWorkerOnTaskQueueError",
    "PublishedVersion",
    "RequestDelivery",
    "RunLookup",
    "WriteRecovery",
    "cold_start_submit_kwargs",
]
