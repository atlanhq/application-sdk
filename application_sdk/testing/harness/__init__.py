"""Shared test-harness plumbing: bounded waits, outcomes, budgets, readers, starters.

Not ``testing.e2e``, because this surface serves runtime scaling scenarios,
cluster reads and chaos injection as well as end-to-end connector runs — none of
which are end-to-end connector runs. ``testing.e2e`` keeps its name, its import
paths and its public surface; it is re-expressed *over* this package in place
(child H on FND-224), so no connector, no generated ``_e2e_base.py`` and no
conformance rule sees a diff.

Three consumers, one core:

* **connector suites** — ``BaseE2ETest`` / ``SQLAppE2ETest``, unchanged names and
  behaviour, expressed over these functions.
* **the runtime scenario suite** (``atlanhq/app-runtime-test-suite``) — Python,
  out-of-cluster, driving these readers and waits from its own scenario format.
* **composers** — anything that wants the setup/teardown steps as async fixtures
  rather than as a base class to inherit.

Everything below the pytest boundary is ``async`` (decision D1). The one bridge
back to blocking code is :func:`~application_sdk.testing.harness.bridge.run_sync`,
exported here and documented as test-harness-only — see that module for why it
does not live in ``application_sdk.common``.

Module map:

``bridge``
    The one sync/async bridge. One reused event loop per thread.
``_poll``
    The deadline arithmetic every bounded loop in the harness runs on — both
    wait shapes, and the handful whose probe is synchronous and so reads it
    directly. Private; moved here from ``testing/e2e`` in child C, and the sole
    remaining clock since child D (FND-240).
``waiting``
    ``poll_until`` and ``hold_stable`` — the only two bounded-wait shapes.
``outcome``
    What a wait returns: settled, never-started, stalled, expired, indeterminate
    — plus ``grade``, the precondition gate that reduces a scenario's whole pile
    of outcomes and findings to one verdict.
``budgets``
    One typed ``Budget`` per wait, and named per-tier profiles.
``identity``
    Run-id and unique-name minting, behind a clock seam.
``expectations``
    The two pure evaluators — asset counts and qualified-name locations.
``evidence``
    Evidence bundle collection, and redaction at the boundary that ships it.
``spec``
    ``AppUnderTest`` — where to find the app under test in a cluster.
``cluster``
    Read-only Kubernetes Protocols, the states they return, and the typed
    ``kubeconfig`` backend behind them — which retired ``testing/e2e/pods.py``
    rather than wrapping it. ``kubectl`` survives there only as transport, for
    the port-forward that reaches an app handler Service.
``temporal``
    Read-only Temporal Protocol — pollers and workflow status — and the
    ``temporalio`` backend behind it. The poller read makes an *observation*
    available where ``NoWorkerOnTaskQueueError`` currently reasons from three
    minutes of silence; it does not yet replace it. Nothing in ``testing/e2e``
    calls this module, and it was not lifted from code in this repo — the poller
    half re-expresses ``suite/ports/temporal.py`` in
    ``atlanhq/app-runtime-test-suite``, so its unit tests are a golden table for
    the plain reason that there is no local original to run a differential
    against. Not the only module here whose pin is a captured table, and not a
    claim that it is: see the provenance paragraph below. Adopting it in
    ``poll_native_status`` is child H's, with the rest of the re-expression.
    No extra to install: ``temporalio`` has been a core dependency since v3.1.
``atlas``
    Atlas reads, split out of ``testing/e2e/client.py`` — async, one client per
    batch, and an unreadable search reported as a verdict rather than as zero.
``automation_engine``
    The async AE reader and the non-idempotent submit's retry, from the same
    split, plus the ``native-status`` wire types and the leaves they raise.
``starters``
    Three ways to start a workflow; deliberately no shared signature.
``teardown``
    Purge mechanics, including the batching that is a correctness bound.

``bridge``, ``waiting``, ``outcome``, ``spec``, ``budgets``, ``expectations``,
``identity``, ``atlas``, ``automation_engine``, ``cluster`` and ``temporal`` are
real; the rest are typed stubs, each naming the child issue that fills it in.

``atlas`` and ``automation_engine`` are the first two that ``testing/e2e``
actually calls: since child F, ``AEWorkflowClient`` is a set of one-line
``run_sync`` shims over them and holds no logic of its own. ``waiting`` joined
them in child D (FND-240) — ``poll_native_status``, ``atlas.poll_for_connection``,
``workflows.wait_for_workflow`` and ``AEClient.wait_for_slug`` are all expressed
over :func:`~application_sdk.testing.harness.waiting.poll_until`, and the loops
whose probe is synchronous read ``_poll``'s deadline arithmetic directly. No
bounded loop in the harness owns a deadline any more.

The rest are pinned by their unit tests rather than by being called from
``testing/e2e``, which is child H's change. What that pin *is* differs by
provenance, and the difference is worth knowing before trusting one: a module
lifted from code in this repo can be tested **differentially**, against the
implementation it replaced on identical inputs, while one that was not has to be
pinned against **captured numbers** instead. ``cluster`` is the first kind — it
retired ``testing/e2e/pods.py``, so there was an original to compare against.
``temporal`` is the second, and says so in its own entry above — so "pinned
against the code they were lifted from" is not true of every module here.

``waiting`` started as the first kind and became the second in child D, which is
the case worth reading carefully because the change was not a choice: its pin
*was* a differential against ``poll_native_status`` on identical scripted
readings, and that stopped meaning anything the moment that function became a
call to ``poll_until``. A loop compared against itself agrees by construction. So
the numbers the hand-rolled loop produced — verdict, probe count and exact sleep
sequence, for fifteen scripts — were captured before the conversion and frozen in
``test_waiting_equivalence.py`` as a golden table. A differential that has
quietly become circular is worse than no differential, because it still passes.

The optional ``harness`` extra carries the typed Kubernetes backend for
``cluster`` (``pip install 'atlan-application-sdk[harness]'``). It is
deliberately *not* folded into ``[tests]``: a connector installing test extras
should not pull a Kubernetes client. Importing ``harness.cluster`` without the
extra is free — the client is imported when a reader is *built*, and the miss
raises :class:`KubernetesExtraMissingError` naming the extra rather than an
``ImportError`` naming a module nobody asked for.
"""

from application_sdk.testing.harness._errors import (
    HarnessNotBuiltError,
    MissingTenantEnvError,
    SyncBridgeInAsyncContextError,
    WaitExpiredError,
    WaitIndeterminateError,
    WaitNeverStartedError,
    WaitStalledError,
)
from application_sdk.testing.harness.bridge import close_loop, run_sync
from application_sdk.testing.harness.budgets import (
    CONNECTOR_CI,
    Budget,
    BudgetProfile,
    Call,
    RequestBudget,
    Wait,
)
from application_sdk.testing.harness.outcome import (
    Expired,
    Indeterminate,
    NeverStarted,
    Outcome,
    Settled,
    Stalled,
    Verdict,
    assert_settled,
    grade,
)
from application_sdk.testing.harness.spec import AppUnderTest
from application_sdk.testing.harness.waiting import (
    Classifier,
    Probe,
    hold_stable,
    poll_until,
)

__all__ = [
    # The sync bridge — test-harness only
    "SyncBridgeInAsyncContextError",
    # Raised by every not-yet-implemented scaffold function; also a
    # NotImplementedError, so `except NotImplementedError` still catches it.
    "HarnessNotBuiltError",
    # No tenant in the ambient environment to run against
    "MissingTenantEnvError",
    "close_loop",
    "run_sync",
    # Bounded waits
    "Classifier",
    "Probe",
    "hold_stable",
    "poll_until",
    # Outcomes, and the leaves assert_settled raises for the failing four
    "Expired",
    "Indeterminate",
    "NeverStarted",
    "Outcome",
    "Settled",
    "Stalled",
    "Verdict",
    "WaitExpiredError",
    "WaitIndeterminateError",
    "WaitNeverStartedError",
    "WaitStalledError",
    "assert_settled",
    "grade",
    # Budgets
    "CONNECTOR_CI",
    "Budget",
    "BudgetProfile",
    "Call",
    "RequestBudget",
    "Wait",
    # App under test
    "AppUnderTest",
]
