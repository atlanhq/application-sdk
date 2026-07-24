"""Version information for the application_sdk package.

Holds two kinds of versions:

* ``__version__`` — the SDK's own package version.
* ``__dapr_version`` — the pinned Dapr runtime the SDK auto-downloads on
  first local-dev run (see ``application_sdk.dev._dapr``). Leading double
  underscore signals it is **SDK-internal**; consumer apps should rely on
  the runtime behaviour, not import this value.

  This governs **only** the local-dev download path (and the CI daprd cache
  key). It does **not** *control* the container image's daprd: that is baked
  into the ``app-framework-golden`` base image via Chainguard Custom Assembly
  and pinned by the Custom Assembly config. The dependency runs the other
  way — the base image is the source of truth, and this constant is kept in
  sync with the daprd baked into it: ``.github/workflows/check-dapr-version.yaml``
  reads ``daprd --version`` from the golden base on each release-time image
  rebuild and opens a PR bumping this constant when it drifts (BLDX-1467).
  Don't expect editing this value to move the container — it only steers the
  local-dev / CI download so it matches what the container already runs.

Everything that needs the daprd pin derives it from this one line — nothing
re-hardcodes the version:

* ``application_sdk.dev._dapr`` imports ``__dapr_version`` directly for the
  embedded local-dev daprd download.
* The external-Dapr CI paths read it without importing, by grepping this file::

      ver=$(grep '^__dapr_version' application_sdk/version.py | cut -d'"' -f2)

  — namely ``.github/actions/ae-stack-up/ae-stack-up.sh`` and
  ``.github/actions/connector-integration-tests/action.yaml``.

This constant is normally updated by the sync workflow above; bump it by hand
only to reconcile ahead of a release — and deliberately, since older Dapr
releases drop off the CDN.
"""

__version__ = "3.24.1"
__dapr_version: str = "1.18.1"
