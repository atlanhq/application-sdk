"""Version information for the application_sdk package.

Holds two kinds of versions:

* ``__version__`` — the SDK's own package version.
* ``__dapr_version`` — the pinned Dapr runtime the SDK auto-downloads on
  first local-dev run (see ``application_sdk.dev._dapr``). Leading double
  underscore signals it is **SDK-internal**; consumer apps should rely on
  the runtime behaviour, not import this value.

CI workflows that need the daprd pin (`.github/workflows/scale-tests.yaml`,
`.github/actions/e2e-examples/action.yaml`) read it via a small shell
step so this file stays the only place the literal ``1.17.x`` appears
in the repo::

    ver=$(grep '^__dapr_version' application_sdk/version.py | cut -d'"' -f2)

Bump deliberately — older Dapr releases drop off the CDN.
"""

__version__ = "3.12.2"
__dapr_version: str = "1.17.7"
