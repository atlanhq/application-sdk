"""Version information for the application_sdk package.

Holds two kinds of versions:

* ``__version__`` — the SDK's own package version.
* ``__dapr_version`` — the pinned Dapr runtime the SDK auto-downloads on
  first local-dev run (see ``application_sdk.dev._dapr``). Leading double
  underscore signals it is **SDK-internal**; consumer apps should rely on
  the runtime behaviour, not import this value.

CI workflows that need the daprd pin (`.github/workflows/push.yaml`,
`.github/workflows/pull_request.yaml`, `.github/workflows/scale-tests.yaml`,
`.github/actions/e2e-examples/action.yaml`) avoid hard-coding the runtime
version by reading it from this file via a small shell step::

    ver=$(grep '^__dapr_version' application_sdk/version.py | cut -d'"' -f2)

Bump deliberately — older Dapr releases drop off the CDN.
"""

__version__ = "3.15.1"
__dapr_version: str = "1.17.9"
