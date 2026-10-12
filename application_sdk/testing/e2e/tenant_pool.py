"""Which pool of e2e tenants a run is on, and whether the suite may run there.

Connector apps and system apps test on separate tenants. CI records the pool a
leg was placed in as ``E2E_TENANT_POOL``; a local run sets it by hand. Each kind
of suite runs on its own pool only:

* :class:`~application_sdk.testing.e2e.system_app.SystemAppE2ETest` requires
  ``E2E_TENANT_POOL=system``.
* Every other :class:`~application_sdk.testing.e2e.base.BaseE2ETest` suite runs
  anywhere *except* ``system``. Unset counts as the connector pool, so a suite
  that predates pools runs exactly as before.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from application_sdk.testing.e2e._errors import TenantPoolMismatchError

__all__ = ["TENANT_POOL_ENV", "TenantPool", "check_tenant_pool"]

TENANT_POOL_ENV = "E2E_TENANT_POOL"


class TenantPool(str, Enum):
    """A pool of e2e tenants."""

    CONNECTOR = "connector"
    SYSTEM = "system"


def check_tenant_pool(
    required: TenantPool, environ: Mapping[str, str], *, suite: str
) -> TenantPool:
    """Return the pool *environ* names, if *suite* may run on it.

    Args:
        required: The pool the suite is written for.
        environ: The environment to read ``E2E_TENANT_POOL`` from.
        suite: The suite's class name, for the error message.

    Returns:
        The pool this run is on. Unset reads as :attr:`TenantPool.CONNECTOR`.

    Raises:
        TenantPoolMismatchError: The variable names an unknown pool, or a pool
            other than *required*.
    """
    raw = environ.get(TENANT_POOL_ENV, "").strip()
    try:
        actual = TenantPool(raw) if raw else TenantPool.CONNECTOR
    except ValueError:
        known = ", ".join(p.value for p in TenantPool)
        raise TenantPoolMismatchError(
            message=(
                f"{TENANT_POOL_ENV}={raw!r} is not a tenant pool (expected one of: "
                f"{known})."
            ),
            expected_state=f"{TENANT_POOL_ENV} in ({known})",
        ) from None
    if actual is required:
        return actual
    if required is TenantPool.SYSTEM:
        hint = (
            "SystemAppE2ETest suites run only on the system-app tenant pool. In "
            "CI that pool is available only to system-app repos; to run locally "
            f"against a system-app tenant, export {TENANT_POOL_ENV}=system."
        )
    else:
        hint = (
            "The system-app tenant pool is reserved for SystemAppE2ETest suites. "
            "A connector suite runs on the connector pool, which is what an unset "
            f"{TENANT_POOL_ENV} means."
        )
    raise TenantPoolMismatchError(
        message=(
            f"{suite} is a {required.value}-pool suite but this run is on the "
            f"{actual.value} pool ({TENANT_POOL_ENV}={raw or '<unset>'}). {hint}"
        ),
        expected_state=f"{TENANT_POOL_ENV}={required.value}",
    )
