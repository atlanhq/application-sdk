"""Register this run's e2e tenants as lineage targets of the pinned DataForge source.

Why this exists (FND-5375)
--------------------------
An event-started e2e for a lineage connector (openlineage) uses a real source
that emits OpenLineage events, a DataForge-provisioned Airflow, instead of
posting recorded events. That instance sends every event to the tenants in its
lineage-target list. The e2e tenants come from the tenant-matrix org secrets and
get rotated, so the list cannot be typed in once at provision time: this step
upserts each tenant of the pool this run uses, with the API key that tenant's
matrix entry already carries for the harness (``api_key``, the same
``service-account-apikey`` key the tenant's events gateway accepts), through
DataForge's ``PUT /api/v1/resources/{id}/lineage-targets``.

DataForge merges by tenant under a per-resource lock, so concurrent runs on
other tenants never overwrite each other, and prunes CI entries a run has not
refreshed in a week, so a rotated-out tenant drops off on its own.

Which pool: the same rule the leg resolver applies (``resolve_e2e_tenant``): the
system matrix when it is shared with the repo, else the connector matrix. Every
cloud in it is registered, because the run's legs fan out over its clouds.

Never prints a key, a tenant host or a base URL; the host and URL are masked by
the resolver already, so this step masks them too. Errors name cloud keys and
HTTP status only.

Run as a checked-out ``.github/scripts`` script beside
``dataforge_source_lifecycle.py``: it needs the same VPN tunnel and imports the
same OIDC exchange.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_dataforge_source import (
    _error_class,
    _exchange_for_service_token,
    _github_oidc_token,
)

DEFAULT_BASE_URL = "https://api.dataforge.atlan.dev"
# Upserting a lineage target changes the resource's runtime config, the same
# authority as pause/resume.
SCOPE = "resource:lifecycle"


class LineageTargetError(RuntimeError):
    """A tenant could not be registered. The message never carries a value."""


def tenants_from_matrix(
    matrix_json: str, secret_name: str
) -> list[tuple[str, str, str]]:
    """``[(cloud, base_url, api_key)]`` for every cloud in a tenant-matrix secret.

    Raises:
        LineageTargetError: The payload is not the matrix shape, or an entry has
            no ``tenant`` / ``api_key``. Names cloud keys only.
    """
    try:
        parsed = json.loads(matrix_json)
    except json.JSONDecodeError as exc:
        raise LineageTargetError(f"{secret_name} is not valid JSON") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise LineageTargetError(
            f"{secret_name} must be a non-empty object keyed by cloud"
        )
    matrix = cast("dict[str, object]", parsed)
    out: list[tuple[str, str, str]] = []
    for cloud in sorted(matrix):
        raw_entry = matrix[cloud]
        if not isinstance(raw_entry, dict):
            raise LineageTargetError(f"{secret_name}[{cloud!r}] must be an object")
        entry = cast("dict[str, object]", raw_entry)
        tenant = str(entry.get("tenant", "") or "").strip()
        api_key = str(entry.get("api_key", "") or "").strip()
        missing = [n for n, v in (("tenant", tenant), ("api_key", api_key)) if not v]
        if missing:
            raise LineageTargetError(
                f"{secret_name}[{cloud!r}] is missing or blank for: {', '.join(missing)}"
            )
        # Same derivation as resolve_e2e_tenant.resolve(): ATLAN_BASE_URL is
        # https://<tenant>, so the registered tenant is the one the legs use.
        out.append((cloud, f"https://{tenant}", api_key))
    return out


def pick_matrix(environ: dict[str, str]) -> tuple[str, str]:
    """The matrix this run's legs resolve from, and its secret name.

    Mirrors resolve_e2e_tenant: the system matrix wins when it is set.
    """
    system = environ.get("E2E_SYSTEM_TENANT_MATRIX_JSON", "").strip()
    if system:
        return system, "E2E_SYSTEM_TENANT_MATRIX_JSON"
    connector = environ.get("E2E_TENANT_MATRIX_JSON", "").strip()
    if connector:
        return connector, "E2E_TENANT_MATRIX_JSON"
    raise LineageTargetError(
        "neither E2E_SYSTEM_TENANT_MATRIX_JSON nor E2E_TENANT_MATRIX_JSON is "
        "available to this repository, so there is no tenant to register"
    )


def mask(values: list[str]) -> None:
    """Register *values* with the runner's log masker."""
    for value in values:
        print(f"::add-mask::{value}")


def put_target(
    base_url: str, token: str, resource_id: str, tenant_url: str, api_key: str
) -> None:
    """Upsert one lineage target. Raises LineageTargetError on any failure."""
    url = f"{base_url}/api/v1/resources/{urllib.parse.quote(resource_id, safe='')}/lineage-targets"
    req = urllib.request.Request(
        url,
        method="PUT",
        data=json.dumps({"url": tenant_url, "api_key": api_key}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            return
    except urllib.error.HTTPError as exc:
        raise LineageTargetError(
            f"PUT lineage-targets failed: HTTP {exc.code} ({_error_class(exc)})"
        ) from exc
    except OSError as exc:
        raise LineageTargetError(
            f"PUT lineage-targets failed: {type(exc).__name__}; is the VPN up?"
        ) from exc


def register(
    environ: dict[str, str],
    *,
    base_url: str,
    token_for: Callable[[], str] | None = None,
    put: Callable[[str, str, str, str, str], None] | None = None,
) -> list[str]:
    """Register every tenant of the run's pool. Returns the clouds registered.

    *token_for* and *put* are injection points for tests.
    """
    resource_id = environ.get("DATAFORGE_RESOURCE_ID", "").strip()
    if not resource_id:
        raise LineageTargetError("DATAFORGE_RESOURCE_ID is not set")
    matrix_json, secret_name = pick_matrix(environ)
    tenants = tenants_from_matrix(matrix_json, secret_name)
    mask(
        [v for _, url, key in tenants for v in (url, url.removeprefix("https://"), key)]
    )

    def _default_token() -> str:
        return _exchange_for_service_token(base_url, _github_oidc_token(), scope=SCOPE)

    get_token = token_for or _default_token
    do_put = put or put_target
    token = get_token()
    registered: list[str] = []
    failures: list[str] = []
    for cloud, tenant_url, api_key in tenants:
        try:
            do_put(base_url, token, resource_id, tenant_url, api_key)
            registered.append(cloud)
        except LineageTargetError as exc:
            failures.append(f"{cloud}: {exc}")
    for line in failures:
        print(f"::error::lineage target for cloud {line}")
    if failures:
        raise LineageTargetError(
            f"{len(failures)} of {len(tenants)} tenant(s) could not be registered"
        )
    return registered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register this run's e2e tenants as lineage targets of the pinned DataForge source."
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("DATAFORGE_BASE_URL", DEFAULT_BASE_URL)
    )
    args = parser.parse_args(argv)
    try:
        clouds = register(dict(os.environ), base_url=args.base_url.rstrip("/"))
    except LineageTargetError as exc:
        print(f"::error::{exc}")
        return 1
    print(f"Registered lineage targets for cloud(s): {', '.join(clouds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
