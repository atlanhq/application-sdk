"""Deprecated: the workflow-setup route check is SDK CI tooling, not app API.

The check (FND-1667) only ever runs in the SDK's own ``sdr-e2e`` composite, which
calls it against the tenant on every connector's e2e leg. It ships in the wheel
only because that composite imports it from the app's installed SDK. The
implementation now lives in :mod:`application_sdk.testing._setup_routes`, which
the composite reads directly; every name here still resolves to the same
object, with a ``DeprecationWarning`` on access.

.. deprecated:: 3.56
    Rely on the ``sdr-e2e`` composite's "Verify workflow-setup routes resolve"
    step instead of calling this from an app's own tests. Will be removed in
    v4.0.0, when the implementation moves under ``.github/`` (FND-4660).
"""

from __future__ import annotations

import importlib
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type checkers only: binds nothing at runtime, so every access below still
    # goes through ``__getattr__`` and warns. It is also what lets the symbol
    # gate follow each name to its definition, methods included.
    from application_sdk.testing._setup_routes import (  # noqa: F401
        CONFIGMAP_PATH,
        CONFIGMAP_SOURCE_APP_QUERY,
        DEFAULT_CATALOG_WAIT_SECONDS,
        DEFAULT_POD_RECONCILE_WAIT_SECONDS,
        MARKETPLACE_APPS_PATH,
        AppIdentity,
        Card,
        Entrypoint,
        FormStep,
        RouteCheckSkipped,
        RouteReader,
        ServedForm,
        SetupRouteError,
        TenantRoutes,
        TenantUnreachable,
        declared_inputs,
        foreign_form,
        form_shortfall,
        locate_cards,
        read_app_identity,
        read_entrypoint_names,
        read_entrypoints,
        route_mismatch,
        served_form,
        verify,
    )

#: name -> (replacement, why). Served by ``__getattr__`` below (PEP 562), which
#: returns the private implementation's object itself, so a patch or an
#: ``isinstance`` check sees one object, not two.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "MARKETPLACE_APPS_PATH": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "CONFIGMAP_PATH": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "CONFIGMAP_SOURCE_APP_QUERY": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "DEFAULT_CATALOG_WAIT_SECONDS": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "DEFAULT_POD_RECONCILE_WAIT_SECONDS": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "RouteReader": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "SetupRouteError": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "TenantUnreachable": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "RouteCheckSkipped": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "Entrypoint": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "AppIdentity": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "read_app_identity": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "read_entrypoint_names": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "declared_inputs": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "read_entrypoints": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "Card": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "route_mismatch": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "form_shortfall": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "locate_cards": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "FormStep": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "ServedForm": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "served_form": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "TenantRoutes": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "foreign_form": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
    "verify": (
        "the sdr-e2e composite's workflow-setup route check",
        "the check is SDK CI tooling and leaves the package; that step already runs it against your tenant on every e2e leg (FND-4660)",
    ),
}


#: name -> (defining module, attribute): where ``__getattr__`` fetches each name
#: from. The symbol gate reads it too, to follow each name to its definition so
#: a moved class keeps its methods on this module's surface.
_LAZY: dict[str, tuple[str, str]] = {
    "MARKETPLACE_APPS_PATH": (
        "application_sdk.testing._setup_routes",
        "MARKETPLACE_APPS_PATH",
    ),
    "CONFIGMAP_PATH": ("application_sdk.testing._setup_routes", "CONFIGMAP_PATH"),
    "CONFIGMAP_SOURCE_APP_QUERY": (
        "application_sdk.testing._setup_routes",
        "CONFIGMAP_SOURCE_APP_QUERY",
    ),
    "DEFAULT_CATALOG_WAIT_SECONDS": (
        "application_sdk.testing._setup_routes",
        "DEFAULT_CATALOG_WAIT_SECONDS",
    ),
    "DEFAULT_POD_RECONCILE_WAIT_SECONDS": (
        "application_sdk.testing._setup_routes",
        "DEFAULT_POD_RECONCILE_WAIT_SECONDS",
    ),
    "RouteReader": ("application_sdk.testing._setup_routes", "RouteReader"),
    "SetupRouteError": ("application_sdk.testing._setup_routes", "SetupRouteError"),
    "TenantUnreachable": ("application_sdk.testing._setup_routes", "TenantUnreachable"),
    "RouteCheckSkipped": ("application_sdk.testing._setup_routes", "RouteCheckSkipped"),
    "Entrypoint": ("application_sdk.testing._setup_routes", "Entrypoint"),
    "AppIdentity": ("application_sdk.testing._setup_routes", "AppIdentity"),
    "read_app_identity": ("application_sdk.testing._setup_routes", "read_app_identity"),
    "read_entrypoint_names": (
        "application_sdk.testing._setup_routes",
        "read_entrypoint_names",
    ),
    "declared_inputs": ("application_sdk.testing._setup_routes", "declared_inputs"),
    "read_entrypoints": ("application_sdk.testing._setup_routes", "read_entrypoints"),
    "Card": ("application_sdk.testing._setup_routes", "Card"),
    "route_mismatch": ("application_sdk.testing._setup_routes", "route_mismatch"),
    "form_shortfall": ("application_sdk.testing._setup_routes", "form_shortfall"),
    "locate_cards": ("application_sdk.testing._setup_routes", "locate_cards"),
    "FormStep": ("application_sdk.testing._setup_routes", "FormStep"),
    "ServedForm": ("application_sdk.testing._setup_routes", "ServedForm"),
    "served_form": ("application_sdk.testing._setup_routes", "served_form"),
    "TenantRoutes": ("application_sdk.testing._setup_routes", "TenantRoutes"),
    "foreign_form": ("application_sdk.testing._setup_routes", "foreign_form"),
    "verify": ("application_sdk.testing._setup_routes", "verify"),
}


#: Kept so ``from <this module> import *`` still binds every moved name: a star
#: import reads ``__all__`` and then resolves each entry through ``__getattr__``,
#: so each name warns. A literal, because the symbol gate reads it statically.
__all__ = [
    "MARKETPLACE_APPS_PATH",
    "CONFIGMAP_PATH",
    "CONFIGMAP_SOURCE_APP_QUERY",
    "DEFAULT_CATALOG_WAIT_SECONDS",
    "DEFAULT_POD_RECONCILE_WAIT_SECONDS",
    "RouteReader",
    "SetupRouteError",
    "TenantUnreachable",
    "RouteCheckSkipped",
    "Entrypoint",
    "AppIdentity",
    "read_app_identity",
    "read_entrypoint_names",
    "declared_inputs",
    "read_entrypoints",
    "Card",
    "route_mismatch",
    "form_shortfall",
    "locate_cards",
    "FormStep",
    "ServedForm",
    "served_form",
    "TenantRoutes",
    "foreign_form",
    "verify",
]


def __getattr__(name: str) -> object:
    """Serve the moved names once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    module, attr = _LAZY[name]
    return getattr(importlib.import_module(module), attr)
