"""Deprecated alias of :mod:`application_sdk.common.build_identity`.

The implementation moved out of the ``app`` package (FND-3280) so the handler
can serve the build identity without importing ``application_sdk.app``, whose
``__init__`` loads the worker. Every name here still resolves to the same object
as its ``common`` counterpart, with a ``DeprecationWarning`` on access.

.. deprecated:: 3.56
    Import from :mod:`application_sdk.common.build_identity`. Will be removed in
    v4.0.0.
"""

from __future__ import annotations

import warnings

#: name -> (replacement, why). Served by ``__getattr__`` below (PEP 562), which
#: returns the ``common`` object itself so a patch or an identity check sees one
#: object, not two.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "BUILD_ID_ENV": (
        "application_sdk.common.build_identity.BUILD_ID_ENV",
        "the implementation moved to common (FND-3280)",
    ),
    "BUILD_IDENTITY_CONFIGMAP_ID": (
        "application_sdk.common.build_identity.BUILD_IDENTITY_CONFIGMAP_ID",
        "the implementation moved to common (FND-3280)",
    ),
    "BUILD_INFO_BUILD_ID_KEY": (
        "application_sdk.common.build_identity.BUILD_INFO_BUILD_ID_KEY",
        "the implementation moved to common (FND-3280)",
    ),
    "build_identity": (
        "application_sdk.common.build_identity.build_identity",
        "the implementation moved to common (FND-3280)",
    ),
}


def __getattr__(name: str) -> object:
    """Serve the moved names once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated here; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    from application_sdk.common import build_identity as _target  # noqa: PLC0415

    return getattr(_target, name)
