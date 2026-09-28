"""The ``application_sdk_api`` import alias — one spelling for detectors to match.

The handler surface and the error taxonomy moved into the thin
``atlan-application-sdk-api`` distribution (import root ``application_sdk_api``).
``application_sdk.handler*`` is a deprecated shim over it and
``application_sdk.errors*`` a first-class re-export of it: every name resolves
to the *same object* through either path.  A detector that matches the literal
``application_sdk.errors.`` / ``application_sdk.handler.`` prefix therefore goes
blind the moment an app imports the new path.

:func:`canonical_sdk_module` folds the new spelling onto the old one, so a
detector canonicalises the module path it read and keeps matching the prefix it
always matched.  It is deliberately a pure string rewrite of the import root —
``application_sdk_api.X`` → ``application_sdk.X`` — and nothing else.

Rules whose subject *is* the choice of import path (B009, P053) must not route
through this helper: they need the raw spelling to tell the two apart.
"""

from __future__ import annotations

SDK_IMPORT_ROOT = "application_sdk"
"""Import root of the ``atlan-application-sdk`` distribution."""

API_IMPORT_ROOT = "application_sdk_api"
"""Import root of the ``atlan-application-sdk-api`` distribution."""


def canonical_sdk_module(name: str) -> str:
    """Return *name* with an ``application_sdk_api`` root rewritten to ``application_sdk``.

    ``application_sdk_api`` → ``application_sdk``;
    ``application_sdk_api.handler.contracts.PreflightCheck`` →
    ``application_sdk.handler.contracts.PreflightCheck``.  Any other name —
    including ``application_sdk_apiary`` and relative ``.x`` paths — is returned
    unchanged.
    """
    if name == API_IMPORT_ROOT:
        return SDK_IMPORT_ROOT
    if name.startswith(API_IMPORT_ROOT + "."):
        return SDK_IMPORT_ROOT + name[len(API_IMPORT_ROOT) :]
    return name


def is_sdk_module(name: str) -> bool:
    """True if *name* is ``application_sdk[.X]`` or its ``application_sdk_api`` alias."""
    canonical = canonical_sdk_module(name)
    return canonical == SDK_IMPORT_ROOT or canonical.startswith(SDK_IMPORT_ROOT + ".")
