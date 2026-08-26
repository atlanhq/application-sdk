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
``waiting``
    ``poll_until`` and ``hold_stable`` — the only two bounded-wait shapes.
``outcome``
    What a wait returns: settled, never-started, stalled, expired, indeterminate.
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

Most of it is typed stubs at this point: this package is the scaffold from
FND-238, and each module names the child issue that fills it in.

The optional ``harness`` extra carries the typed Kubernetes backend for
``cluster`` (``pip install 'atlan-application-sdk[harness]'``). It is
deliberately *not* folded into ``[tests]``: a connector installing test extras
should not pull a Kubernetes client.
"""

from application_sdk.testing.harness._errors import (
    HarnessNotBuiltError,
    SyncBridgeInAsyncContextError,
)
from application_sdk.testing.harness.bridge import close_loop, run_sync
from application_sdk.testing.harness.budgets import Budget, BudgetProfile
from application_sdk.testing.harness.outcome import (
    Expired,
    Indeterminate,
    NeverStarted,
    Outcome,
    Settled,
    Stalled,
    assert_settled,
)
from application_sdk.testing.harness.spec import AppUnderTest
from application_sdk.testing.harness.waiting import Probe, hold_stable, poll_until

__all__ = [
    # The sync bridge — test-harness only
    "SyncBridgeInAsyncContextError",
    # Raised by every not-yet-implemented scaffold function; also a
    # NotImplementedError, so `except NotImplementedError` still catches it.
    "HarnessNotBuiltError",
    "close_loop",
    "run_sync",
    # Bounded waits
    "Probe",
    "hold_stable",
    "poll_until",
    # Outcomes
    "Expired",
    "Indeterminate",
    "NeverStarted",
    "Outcome",
    "Settled",
    "Stalled",
    "assert_settled",
    # Budgets
    "Budget",
    "BudgetProfile",
    # App under test
    "AppUnderTest",
]
