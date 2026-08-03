"""Transport-agnostic preflight / auth check core.

One implementation of "can this app reach and use this source", reached from
every path that needs the answer:

* the config UI (``POST /workflows/v1/{auth,check}``)
* the SDR connectivity test (``checks:*`` Temporal workflows)
* the mandatory pre-extraction gate (``{app}:preflight``)
* scheduled drift detection (the same ``checks:*`` workflows, started by the
  Automation Engine on a cadence)

Before this package the same ``Handler.preflight_check`` was reached three ways
that each assembled the input differently, resolved credentials differently,
enforced a different budget (or none), ran a different set of checks, and
recorded a different amount of nothing. A check could therefore pass in the
config UI and behave differently on the run it was meant to predict. The
divergence was not in the handlers — it was in the four call sites.

The pre-extraction gate had by far the most complete machinery (budget enforced
net of credential resolution, source-unverifiable vs gate-broken classification,
and the only queryable outcome row), so consolidation moved *that* implementation
here and pointed the other paths at it, rather than settling on what the paths
already agreed about.

What stays outside this package, deliberately:

* **Enforcement.** Whether a ``NOT_READY`` verdict aborts the run is the gate's
  business (``App.preflight_gate_mode``); the core only ever *reports* a verdict.
  Verdict and posture are separate concerns — see
  :mod:`application_sdk.execution._temporal.preflight_gate`.
* **Projection.** How a verdict is rendered for a particular consumer (the Sage
  widget's camelCase payload, a Temporal ``ApplicationError``) lives in
  :mod:`application_sdk.checks.projections`, not in the runner.
* **Cadence state.** :mod:`application_sdk.checks.cadence` recommends *when* to
  look again from the verdict alone; the history that recommendation is applied
  to belongs to the caller (the Automation Engine).
"""

from application_sdk.checks.depth import CheckDepth

__all__ = ["CheckDepth"]
