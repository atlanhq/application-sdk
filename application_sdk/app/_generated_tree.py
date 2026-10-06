"""Re-export of :mod:`application_sdk.common._generated_tree`.

Moved out of the ``app`` package (FND-3280) so the handler can read the
generated tree without importing ``application_sdk.app``, whose ``__init__``
loads the worker. Kept so existing imports of this path resolve to the same
objects.
"""

from application_sdk.common._generated_tree import (
    ARTIFACT_SCHEMAS_STEM,
    CREDENTIAL_TEMPLATE_PREFIXES,
    MANIFEST_STEM,
    NON_FORM_STEMS,
    GeneratedLayout,
    choose_form_configmap,
    eligible_form_configmaps,
    form_configmap,
    generated_layout,
    is_form_configmap,
    names_entrypoint,
    pick_form_configmap,
)

__all__ = [
    "ARTIFACT_SCHEMAS_STEM",
    "CREDENTIAL_TEMPLATE_PREFIXES",
    "GeneratedLayout",
    "MANIFEST_STEM",
    "NON_FORM_STEMS",
    "choose_form_configmap",
    "eligible_form_configmaps",
    "form_configmap",
    "generated_layout",
    "is_form_configmap",
    "names_entrypoint",
    "pick_form_configmap",
]
