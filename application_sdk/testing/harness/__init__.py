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
    The deadline arithmetic both bounded-wait shapes and ``testing/e2e``'s
    twelve loops run on. Private; moved here from ``testing/e2e`` in child C.
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
    Read-only Kubernetes Protocols and the states they return.
``temporal``
    Read-only Temporal Protocol — pollers and workflow status.
``atlas``
    Atlas reads, split out of ``testing/e2e/client.py``.
``automation_engine``
    AE reads and the non-idempotent submit's retry, from the same split.
``starters``
    Three ways to start a workflow; deliberately no shared signature.
``teardown``
    Purge mechanics, including the batching that is a correctness bound.

``bridge``, ``waiting``, ``outcome``, ``spec``, ``budgets``, ``expectations``
and ``identity`` are real; the rest are typed stubs, each naming the child issue
that fills it in. Nothing outside this package consumes any of it yet —
``testing/e2e`` is re-expressed over it in child H, and until then the extracted
modules are pinned against the code they were lifted from by their unit tests
rather than by being called from it. For ``waiting`` that pin is a differential
test against ``poll_native_status`` itself, on identical scripted readings.

The optional ``harness`` extra carries the typed Kubernetes backend for
``cluster`` (``pip install 'atlan-application-sdk[harness]'``). It is
deliberately *not* folded into ``[tests]``: a connector installing test extras
should not pull a Kubernetes client.
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
