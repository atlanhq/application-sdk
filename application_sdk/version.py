"""Version information for the application_sdk package.

Holds two kinds of versions:

* ``__version__`` — the SDK's own package version, read from the installed
  distribution so ``pyproject.toml`` is the one place it is declared and the
  release job no longer rewrites this file (FND-4672). This file ships in two
  wheels released in lockstep at one version — ``atlan-application-sdk`` and
  the api-only ``atlan-application-sdk-api`` — so whichever is installed
  answers.
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
* The external-Dapr CI path reads it without importing, by grepping this file::

      ver=$(grep '^__dapr_version' application_sdk/version.py | cut -d'"' -f2)

  — namely ``.github/actions/connector-integration-tests/action.yaml``.

This constant is normally updated by the sync workflow above; bump it by hand
only to reconcile ahead of a release — and deliberately, since older Dapr
releases drop off the CDN.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

__all__: list[str] = []


def _installed_version() -> str:
    for distribution in ("atlan-application-sdk", "atlan-application-sdk-api"):
        try:
            return _distribution_version(distribution)
        except PackageNotFoundError:
            continue
    raise PackageNotFoundError(
        "neither atlan-application-sdk nor atlan-application-sdk-api is installed"
    )


__version__: str = _installed_version()
__dapr_version: str = "1.19.2"
