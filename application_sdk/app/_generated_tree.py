"""Deprecated alias of :mod:`application_sdk.common._generated_tree`.

Moved out of the ``app`` package (FND-3280) so the handler can read the
generated tree without importing ``application_sdk.app``, whose ``__init__``
loads the worker. Every name here still resolves to the same object as its
``common`` counterpart, with a ``DeprecationWarning`` on access.

.. deprecated:: 3.56
    Import from :mod:`application_sdk.common._generated_tree`. Will be removed
    in v4.0.0.
"""

from __future__ import annotations

import warnings

#: name -> (replacement, why). Served by ``__getattr__`` below (PEP 562), which
#: returns the ``common`` object itself so a patch or an ``isinstance`` check
#: sees one object, not two.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "ARTIFACT_SCHEMAS_STEM": (
        "application_sdk.common._generated_tree.ARTIFACT_SCHEMAS_STEM",
        "the implementation moved to common (FND-3280)",
    ),
    "CREDENTIAL_TEMPLATE_PREFIXES": (
        "application_sdk.common._generated_tree.CREDENTIAL_TEMPLATE_PREFIXES",
        "the implementation moved to common (FND-3280)",
    ),
    "GeneratedLayout": (
        "application_sdk.common._generated_tree.GeneratedLayout",
        "the implementation moved to common (FND-3280)",
    ),
    "MANIFEST_STEM": (
        "application_sdk.common._generated_tree.MANIFEST_STEM",
        "the implementation moved to common (FND-3280)",
    ),
    "NON_FORM_STEMS": (
        "application_sdk.common._generated_tree.NON_FORM_STEMS",
        "the implementation moved to common (FND-3280)",
    ),
    "choose_form_configmap": (
        "application_sdk.common._generated_tree.choose_form_configmap",
        "the implementation moved to common (FND-3280)",
    ),
    "eligible_form_configmaps": (
        "application_sdk.common._generated_tree.eligible_form_configmaps",
        "the implementation moved to common (FND-3280)",
    ),
    "form_configmap": (
        "application_sdk.common._generated_tree.form_configmap",
        "the implementation moved to common (FND-3280)",
    ),
    "generated_layout": (
        "application_sdk.common._generated_tree.generated_layout",
        "the implementation moved to common (FND-3280)",
    ),
    "is_form_configmap": (
        "application_sdk.common._generated_tree.is_form_configmap",
        "the implementation moved to common (FND-3280)",
    ),
    "names_entrypoint": (
        "application_sdk.common._generated_tree.names_entrypoint",
        "the implementation moved to common (FND-3280)",
    ),
    "pick_form_configmap": (
        "application_sdk.common._generated_tree.pick_form_configmap",
        "the implementation moved to common (FND-3280)",
    ),
}


#: Kept so ``from <this module> import *`` still binds every moved name: a star
#: import reads ``__all__`` and then resolves each entry through ``__getattr__``,
#: so each name warns. Without it, ``__getattr__`` is never asked.
__all__ = list(_DEPRECATED_CONSTANTS)  # pyright: ignore[reportUnsupportedDunderAll] — names are served by __getattr__


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
    from application_sdk.common import _generated_tree as _target  # noqa: PLC0415

    return getattr(_target, name)
