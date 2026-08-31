"""Shared test-harness plumbing: bounded waits, outcomes, budgets, readers, starters.

Not ``testing.e2e``, because this surface serves runtime scaling scenarios,
cluster reads and chaos injection as well as end-to-end connector runs — none of
which are end-to-end connector runs. ``testing.e2e`` keeps its name, its import
paths and its public surface; it **is** re-expressed over this package in place
(child H on FND-224, landed), so no connector, no generated ``_e2e_base.py`` and
no conformance rule saw a diff.

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
    Evidence bundle collection, and redaction at the boundary that ships it —
    two filters, because key-name matching alone cannot see a value a driver
    echoed back with no key beside it. Child G (FND-243); wired into the
    connector failure path, which writes into the directory the shared CI
    composite already uploads, so a failed leg leaves a redacted bundle behind
    with no workflow change.
``spec``
    ``AppUnderTest`` — where to find the app under test in a cluster.
``substrate``
    Where the code driving the harness runs relative to the app, and the cluster
    reader that follows from it. The fourth point of variance, which neither
    source design document names (child I on FND-224).
``preconditions``
    The gate every scenario runs before it dispatches work — the two built-in
    checks (a worker's health endpoint, and who is polling a task queue), the
    runner that accumulates their outcomes, and the two leaves it raises for
    "the starting state was wrong" and "the starting state could not be read".
``fixtures``
    The same lifecycle as async pytest fixtures, for a suite that inherits
    nothing (child I). **Not imported here**: it imports ``pytest``, and this
    package stays importable in a process with no test framework. Import it as
    ``application_sdk.testing.harness.fixtures``, or register it as a plugin.
``cluster``
    Read-only Kubernetes Protocols, the states they return, and the typed
    ``kubeconfig`` backend behind them — which retired ``testing/e2e/pods.py``
    rather than wrapping it. ``kubectl`` survives there only as transport, for
    the port-forward that reaches an app handler Service.
``temporal``
    Read-only Temporal Protocol — pollers and workflow status — and the
    ``temporalio`` backend behind it. The poller read makes an *observation*
    available where ``NoWorkerOnTaskQueueError`` reasons from three minutes of
    silence. Child H adopted it, and adopted it as an **addition**: the leaf
    still fires on the inference, and where a suite has a route to a frontend
    (``BaseE2ETest.temporal_address`` / ``E2E_TEMPORAL_ADDRESS``, off by default)
    it now carries what Temporal reported alongside it. Off by default because
    the connector CI runner has no route into a tenant's vcluster — the same
    constraint that makes the AE submit the only tenant-facing probe of the
    installed app pod — so replacing the inference outright would have replaced
    a working diagnosis with an unreachable one.

    It was not lifted from code in this repo — the poller half re-expresses
    ``suite/ports/temporal.py`` in ``atlanhq/app-runtime-test-suite``, so its
    unit tests are a golden table for the plain reason that there is no local
    original to run a differential against. Not the only module here whose pin
    is a captured table, and not a claim that it is: see the provenance
    paragraph below. No extra to install: ``temporalio`` has been a core
    dependency since v3.1.
``atlas``
    Atlas reads, split out of ``testing/e2e/client.py`` — async, one client per
    batch, and an unreadable search reported as a verdict rather than as zero.
    Plus the two calls child H moved off the *second*, synchronous pyatlan
    client ``BaseE2ETest`` used to build for itself: the ``$admin`` ACL lookup
    (``admin_identity``, which reports rather than decides, because its two
    callers disagree about whether an absent role is fatal) and the one write in
    this package, ``create_connection`` — which takes the qualified name as an
    *input* rather than adopting one derived from a one-second clock, so two
    matrix legs starting in the same second can no longer share a connection and
    purge each other's assets.
``automation_engine``
    The async AE reader and the non-idempotent submit's retry, from the same
    split, plus the ``native-status`` wire types and the leaves they raise.
``starters``
    Three ways to start a workflow; deliberately no shared signature. Two of the
    three are real, and they are real in different ways. The AE one is also the
    one place the sequence is split: ``publish_seed_version`` is public because
    AE mints the slug on the create and a submit body that has to *carry* that
    slug cannot be built before it — which is exactly ``BaseE2ETest``'s case.
    ``start_on_task_queue``
    (FND-246) dispatches onto a Temporal task queue — the runtime suite's first
    scenario, and unbuilt on both sides: the Slack thread that scoped this
    project recorded it as already existing, and
    ``testing/e2e/workflows.run_workflow`` is an HTTP POST to the app's handler
    Service that never touches a queue. New work, so its tests are claims rather
    than a differential; see the module's own note on provenance below.
    ``start_via_automation_engine`` (child G, FND-243) is the opposite: a plain
    lift of ``_bootstrap_workflow`` plus the submit half of ``run_full_dag``,
    with an original to be pinned against. ``start_via_app_handler`` is still a
    stub, naming child E.
``teardown``
    Purge mechanics (child G, FND-243), including the batching that is a
    correctness bound and the read-everything-before-deleting order that an
    offset-paginated search makes mandatory.

``bridge``, ``waiting``, ``outcome``, ``spec``, ``substrate``, ``budgets``,
``expectations``, ``identity``, ``preconditions``, ``fixtures``, ``atlas``,
``automation_engine``, ``cluster``, ``temporal``, ``evidence`` and ``teardown``
are real, and so are two of ``starters``' three functions; the rest are typed
stubs, each naming the child issue that fills it in.

**Every module here is now called from ``testing/e2e``**, which is what child H
did. ``BaseE2ETest`` holds an ``AEClient`` and calls ``atlas``, ``starters``,
``teardown``, ``expectations``, ``preconditions``, ``identity``, ``budgets``,
``waiting`` and ``evidence`` directly; each of its four public methods is a
one-line :func:`~application_sdk.testing.harness.bridge.run_sync` shim over an
``_async`` twin, so the sync boundary sits at exactly the methods pytest calls
and there is no synchronous implementation underneath. ``AEWorkflowClient``
survives as a deprecated compatibility surface for the one connector suite that
calls it directly; nothing in this package or in ``BaseE2ETest`` routes through
it any more.

``atlas`` and ``automation_engine`` were the first two ``testing/e2e`` called:
since child F, ``AEWorkflowClient`` is a set of one-line ``run_sync`` shims over
them and holds no logic of its own. ``waiting`` joined them in child D
(FND-240) — ``poll_native_status``, ``atlas.poll_for_connection``,
``workflows.wait_for_workflow`` and ``AEClient.wait_for_slug`` are all expressed
over :func:`~application_sdk.testing.harness.waiting.poll_until`, and the loops
whose probe is synchronous read ``_poll``'s deadline arithmetic directly. No
bounded loop in the harness owns a deadline any more.

Being called is not the same as being pinned, and what a pin *is* differs by
provenance — worth knowing before trusting one. A module
lifted from code in this repo can be tested **differentially**, against the
implementation it replaced on identical inputs, while one that was not has to be
pinned against **captured numbers** instead. ``cluster`` is the first kind — it
retired ``testing/e2e/pods.py``, so there was an original to compare against.
``temporal`` is the second, and says so in its own entry above — so "pinned
against the code they were lifted from" is not true of every module here.
``starters``' queue dispatch is neither: there is no original anywhere, in this
repo or the runtime suite, so its tests assert claims about the dispatch it makes
and cannot borrow either kind of authority.

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
    FixtureNotConfiguredError,
    HarnessNotBuiltError,
    MissingTenantEnvError,
    PreconditionsFailedError,
    PreconditionsIndeterminateError,
    SubstrateHasNoClusterError,
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
from application_sdk.testing.harness.preconditions import (
    GateReport,
    HealthReading,
    PollerReading,
    PreconditionCheck,
    assert_gate,
    check_no_stale_pollers,
    check_worker_health,
    run_preconditions,
)
from application_sdk.testing.harness.spec import AppUnderTest
from application_sdk.testing.harness.substrate import Substrate, cluster_reader_for
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
    # App under test, and where the harness is driving it from
    "AppUnderTest",
    "Substrate",
    "SubstrateHasNoClusterError",
    "cluster_reader_for",
    # The precondition gate, its two built-in checks and the readings they take
    "GateReport",
    "HealthReading",
    "PollerReading",
    "PreconditionCheck",
    "PreconditionsFailedError",
    "PreconditionsIndeterminateError",
    "assert_gate",
    "check_no_stale_pollers",
    "check_worker_health",
    "run_preconditions",
    # Composing the fixtures in `harness.fixtures`
    "FixtureNotConfiguredError",
]
